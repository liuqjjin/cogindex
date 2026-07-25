"""Emulation of CocoIndex's tracking semantics for handler-level tests.

Drives a :class:`cogindex._target.DocumentHandler` through full
reconcile -> precommit -> apply -> commit cycles with the audited engine
contract (docs/upstream-audit/cocoindex/findings.md):

- tracking keeps *possible* records: committed states plus a precommitted
  pending intent whose external write may or may not have happened;
- a successful apply commits (collapses to the intended record, or removes
  the key for deletes); a crashed apply leaves the multi-state in place;
- ``prev_may_be_missing`` follows the two transitions upstream pins by test
  (``python/tests/core/test_component_target_states.py``):

  * **failed creation** (nothing was ever committed) surfaces *no* possible
    records and ``prev_may_be_missing=True``, the sink may hold anything or
    nothing (``test_proceed_with_failed_creation``);
  * **failed update** (a committed record plus the intent that failed)
    surfaces *both* records with ``prev_may_be_missing=False``, the sink is
    guaranteed to hold one of them
    (``test_prev_may_be_missing_after_failed_update``).

  A retained *deleted* marker and lossy child invalidation both force
  ``prev_may_be_missing=True``.

Used by both the deterministic fault-matrix tests and the Hypothesis
convergence state machine. The real engine is exercised separately in
tests/unit/test_engine_lifecycle.py.
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

    committed: list[DocumentRecord]
    pending: DocumentRecord | coco.NonExistenceType | None
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
            prev: list[DocumentRecord] = []
            # A fresh key, and a key whose creation failed, both surface no
            # possible records at all and force prev_may_be_missing=True.
            missing = True
            if entry is not None and entry.committed:
                prev = list(entry.committed)
                pending = entry.pending
                if isinstance(pending, DocumentRecord):
                    # Failed update: the committed record and the attempted
                    # one are both real prior sink states, so the engine
                    # leaves prev_may_be_missing False and makes the
                    # handler's own record comparison decide.
                    prev.append(pending)
                    missing = entry.may_be_missing
                else:
                    # A retained deleted marker means the sink may already
                    # have removed the document.
                    missing = entry.may_be_missing or pending is not None
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
                entry = TrackEntry(committed=[], pending=None, may_be_missing=False)
                self.tracking[key] = entry
            entry.pending = output.tracking_record

    async def apply(self, actions: list[_DocumentAction]) -> None:
        if actions:
            await self.handler._apply(cast(Any, None), actions)

    def commit(self, outputs: list[tuple[str, Output]]) -> None:
        for key, output in outputs:
            if coco.is_non_existence(output.tracking_record):
                self.tracking.pop(key, None)
            else:
                self.tracking[key] = TrackEntry(
                    committed=[output.tracking_record],
                    pending=None,
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

        Returns True if it crashed, leaving the precommitted multi-state in
        place for :meth:`reconcile_round` to interpret. That interpretation
        follows the two engine transitions pinned upstream (see the module
        docstring); it is not a claim that every engine-internal detail is
        reproduced, the real engine is exercised in
        tests/unit/test_engine_lifecycle.py.

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

    # -- engine-side events --------------------------------------------------

    def invalidate_lossy(self) -> None:
        """Dataset-level lossy child invalidation (config replace)."""
        for entry in self.tracking.values():
            entry.may_be_missing = True

    def assert_fixed_point(self, declared: Mapping[str, CogneeDocumentSpec]) -> None:
        leftover = self.reconcile_round(declared)
        assert leftover == [], f"not a fixed point: {[k for k, _ in leftover]}"
