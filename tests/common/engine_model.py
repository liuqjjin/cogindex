"""Emulation of CocoIndex's tracking semantics for handler-level tests.

Drives a :class:`cogindex._target.DocumentHandler` through full
reconcile -> precommit -> apply -> commit cycles with the audited engine
contract (docs/upstream-audit/cocoindex/findings.md):

- precommit appends the intended tracking state to the set of possible states;
  successive failed attempts accumulate rather than overwrite one another;
- possible states include both document records and a retained deletion marker;
  only commit collapses them to the intended record (or removes a deleted key);
- a failed child creation surfaces its intended record with
  ``prev_may_be_missing=True``: the external write may have landed or not;
- failed updates over a confirmed live record retain every attempted record
  with ``prev_may_be_missing=False`` until a delete marker or lossy
  invalidation introduces possible absence.

Used by both the deterministic fault-matrix tests and the Hypothesis
convergence state machine. It is a bounded model of the transitions the tests
exercise, not a reproduction of every engine-internal failure mode.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, cast

import cocoindex as coco

from cogindex._records import DocumentRecord
from cogindex._spec import CogneeDocumentSpec
from cogindex._target import DocumentHandler, _DocumentAction

Output = coco.TargetReconcileOutput[_DocumentAction, DocumentRecord, None]


@dataclasses.dataclass
class TrackEntry:
    """Emulated engine tracking state for one target key."""

    possible_states: list[DocumentRecord | coco.NonExistenceType]
    may_be_missing: bool


class EmulatedEngine:
    """One dataset's tracking store plus the sync cycle around a handler.

    ``handler`` is swappable to model processing-config changes (a config
    change also calls :meth:`invalidate_lossy`, mirroring the engine's lossy
    child invalidation).
    """

    def __init__(self, handler: DocumentHandler) -> None:
        self.handler = handler
        self.tracking: dict[str, TrackEntry] = {}

    # -- cycle phases --------------------------------------------------------

    def reconcile_round(
        self, declared: Mapping[str, CogneeDocumentSpec]
    ) -> list[tuple[str, Output]]:
        outputs: list[tuple[str, Output]] = []
        for key in sorted(set(declared) | set(self.tracking)):
            entry = self.tracking.get(key)
            if entry is None:
                prev: list[DocumentRecord] = []
                # A fresh key has no tracked external state at all.
                missing = True
            else:
                prev = [
                    state for state in entry.possible_states if isinstance(state, DocumentRecord)
                ]
                missing = entry.may_be_missing or any(
                    coco.is_non_existence(state) for state in entry.possible_states
                )
            desired: CogneeDocumentSpec | coco.NonExistenceType = declared.get(
                key, coco.NON_EXISTENCE
            )
            output = self.handler.reconcile(key, desired, prev, missing)
            if output is not None:
                outputs.append((key, output))
        return outputs

    def precommit(self, outputs: list[tuple[str, Output]]) -> None:
        for key, output in outputs:
            entry = self.tracking.get(key)
            if entry is None:
                # With no confirmed baseline, a first attempted creation may
                # or may not have reached the external sink.
                entry = TrackEntry(possible_states=[], may_be_missing=True)
                self.tracking[key] = entry
            intended = output.tracking_record
            if not any(_same_tracking_state(state, intended) for state in entry.possible_states):
                entry.possible_states.append(intended)

    async def apply(self, actions: list[_DocumentAction]) -> None:
        if actions:
            await self.handler._apply(cast(Any, None), actions)

    def commit(self, outputs: list[tuple[str, Output]]) -> None:
        for key, output in outputs:
            if coco.is_non_existence(output.tracking_record):
                self.tracking.pop(key, None)
            else:
                self.tracking[key] = TrackEntry(
                    possible_states=[output.tracking_record],
                    may_be_missing=False,
                )

    # -- composed cycles -----------------------------------------------------

    async def sync(self, declared: Mapping[str, CogneeDocumentSpec]) -> list[tuple[str, Output]]:
        """One successful sync: reconcile, precommit, apply, commit."""
        outputs = self.reconcile_round(declared)
        self.precommit(outputs)
        await self.apply([output.action for _, output in outputs])
        self.commit(outputs)
        return outputs

    async def sync_expect_crash(
        self,
        declared: Mapping[str, CogneeDocumentSpec],
        exc_type: type[BaseException],
    ) -> bool:
        """A sync whose apply is expected to crash with ``exc_type``.

        Returns True if it crashed, leaving every precommitted possible state
        in place for :meth:`reconcile_round` to interpret. This covers sink
        failures only; :meth:`sync_exit_after_apply` models the separate window
        after a successful external write but before tracking commit.

        If the apply happened to succeed (the faulted op never ran in this
        batch), the honest engine step is to commit: done here, and False
        is returned.
        """
        outputs = self.reconcile_round(declared)
        self.precommit(outputs)
        try:
            await self.apply([output.action for _, output in outputs])
        except exc_type:
            return True
        self.commit(outputs)
        return False

    async def sync_exit_after_apply(
        self, declared: Mapping[str, CogneeDocumentSpec]
    ) -> list[tuple[str, Output]]:
        """Precommit and apply successfully, then exit before tracking commit."""
        outputs = self.reconcile_round(declared)
        self.precommit(outputs)
        await self.apply([output.action for _, output in outputs])
        return outputs

    # -- engine-side events --------------------------------------------------

    def invalidate_lossy(self) -> None:
        """Dataset-level lossy child invalidation (config replace)."""
        for entry in self.tracking.values():
            entry.may_be_missing = True

    def assert_fixed_point(self, declared: Mapping[str, CogneeDocumentSpec]) -> None:
        leftover = self.reconcile_round(declared)
        assert leftover == [], f"not a fixed point: {[k for k, _ in leftover]}"


def _same_tracking_state(
    left: DocumentRecord | coco.NonExistenceType,
    right: DocumentRecord | coco.NonExistenceType,
) -> bool:
    if coco.is_non_existence(left) or coco.is_non_existence(right):
        return coco.is_non_existence(left) and coco.is_non_existence(right)
    return left == right
