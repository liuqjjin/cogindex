"""Model-based convergence property (ADR-0003).

A Hypothesis state machine drives arbitrary interleavings of document
declarations, removals, processing-config changes, successful syncs, and
syncs that crash at every phase of the write protocol (lock acquisition,
deletes, purges, partial adds, cognify), against an explicit emulation of
CocoIndex's tracking semantics: precommit -> sink apply -> commit,
multi-state possible records, and ``prev_may_be_missing`` derivation
(pending writes, deleted markers, fresh keys, lossy invalidation).

The property: **any successful sync converges** — the fake Cognee state
equals the reference model (exactly the declared documents, fresh
derivatives, current config), reconciliation reaches a fixed point, and no
sequence of prior crashes can break this. Safety holds even after crashed
syncs: nothing exists that was never declared, and locks stay balanced.

The engine itself is not under test (that is upstream's suite plus our
engine-lifecycle tests); its tracking contract is emulated per the audited
semantics recorded in docs/upstream-audit/cocoindex/findings.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Any, cast

import cocoindex as coco
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

import cogindex
from cogindex import CognifyProfile, DatasetHandle
from cogindex._identity import fingerprint_content
from cogindex._records import DocumentRecord
from cogindex._spec import CogneeDocumentSpec
from cogindex._target import DocumentHandler, _DocumentAction
from cogindex.testing import FakeCogneeRuntime, InjectedFault

pytestmark = pytest.mark.property

RUNTIME_KEY = "rt-prop"
TENANT = "default"
DATASET = "ds-prop"

# Two processing configurations; switching between them is the
# config-invalidation transition (ADR-0005).
PROFILES: dict[str, CognifyProfile] = {
    "pfp-A": CognifyProfile(chunk_size=100),
    "pfp-B": CognifyProfile(chunk_size=200),
}

KEYS = ["a.md", "b.md", "dir/c.md", "café.md", "d.txt"]
CONTENTS = ["alpha", "beta", "gamma", ""]
LABELS = [None, "L1", "L2"]
NODE_SETS: list[tuple[str, ...] | None] = [None, ("n1",), ("n1", "n2")]
WEIGHTS = [None, 0.5]

FAULT_OPS = [
    "dataset_lock",
    "delete_documents",
    "purge_document_memory",
    "add_documents",
    "cognify_dataset",
]


def _data_id(key: str) -> uuid.UUID:
    return cogindex.document_data_id(RUNTIME_KEY, TENANT, DATASET, key)


@dataclasses.dataclass
class _TrackEntry:
    """Emulated engine tracking state for one target key.

    ``committed`` are the possible records from completed syncs; ``pending``
    is a precommitted intended record (or NON_EXISTENCE for an intended
    delete) whose external write may or may not have happened.
    """

    committed: list[DocumentRecord]
    pending: DocumentRecord | coco.NonExistenceType | None
    may_be_missing: bool


_Output = coco.TargetReconcileOutput[_DocumentAction, DocumentRecord, None]


class ConvergenceMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        # One persistent loop: asyncio primitives in the fake's lock provider
        # bind to the loop they first run on.
        self.loop = asyncio.new_event_loop()
        self.fake = FakeCogneeRuntime()
        self.declared: dict[str, CogneeDocumentSpec] = {}
        self.tracking: dict[str, _TrackEntry] = {}
        self.config_fp = "pfp-A"
        handle = DatasetHandle(name=DATASET, tenant=TENANT)
        self.handlers = {
            fp: DocumentHandler(
                runtime=self.fake,
                runtime_key=RUNTIME_KEY,
                handle=handle,
                profile=profile,
                processing_fingerprint=fp,
            )
            for fp, profile in PROFILES.items()
        }
        self.ever_declared_ids: set[uuid.UUID] = set()

    def teardown(self) -> None:
        self.loop.close()

    # -- transitions on desired state ---------------------------------------

    @rule(
        key=st.sampled_from(KEYS),
        content=st.sampled_from(CONTENTS),
        label=st.sampled_from(LABELS),
        node_set=st.sampled_from(NODE_SETS),
        weight=st.sampled_from(WEIGHTS),
    )
    def declare(
        self,
        key: str,
        content: str,
        label: str | None,
        node_set: tuple[str, ...] | None,
        weight: float | None,
    ) -> None:
        self.declared[key] = CogneeDocumentSpec(
            content=content,
            label=label,
            node_set=node_set,
            importance_weight=weight,
        )
        self.ever_declared_ids.add(_data_id(key))

    @rule(key=st.sampled_from(KEYS))
    def undeclare(self, key: str) -> None:
        self.declared.pop(key, None)

    @rule(fp=st.sampled_from(sorted(PROFILES)))
    def change_config(self, fp: str) -> None:
        if fp == self.config_fp:
            return
        self.config_fp = fp
        # Emulate the engine's "lossy" child invalidation on dataset config
        # replace: every child's records become possibly-missing.
        for entry in self.tracking.values():
            entry.may_be_missing = True

    # -- sync transitions ----------------------------------------------------

    @rule()
    def sync_ok(self) -> None:
        outputs = self._reconcile_round()
        self._precommit(outputs)
        self._apply([output.action for _, output in outputs])
        self._commit(outputs)
        self._assert_converged()

    @rule(fault=st.sampled_from(FAULT_OPS), after=st.integers(min_value=0, max_value=2))
    def sync_crash_mid_batch(self, fault: str, after: int) -> None:
        outputs = self._reconcile_round()
        if not outputs:
            return
        self._precommit(outputs)
        if fault == "add_documents":
            self.fake.inject_fault(fault, after_items=after)
        else:
            self.fake.inject_fault(fault)
        try:
            self._apply([output.action for _, output in outputs])
        except InjectedFault:
            # Crashed mid-batch: nothing commits; the precommitted multi-state
            # is exactly what the engine would hand the next reconcile.
            pass
        else:
            # The faulted op never ran in this batch (e.g. nothing to purge):
            # the batch genuinely succeeded, so the honest engine step is to
            # commit it.
            self._commit(outputs)
        finally:
            self.fake.clear_faults()

    @rule()
    def sync_crash_before_apply(self) -> None:
        """Process dies between precommit and the first external write."""
        outputs = self._reconcile_round()
        self._precommit(outputs)

    # -- safety, checked after every step ------------------------------------

    @invariant()
    def nothing_undeclared_exists(self) -> None:
        dataset = self.fake.dataset(TENANT, DATASET)
        if dataset is not None:
            phantom = set(dataset.documents) - self.ever_declared_ids
            assert not phantom, f"phantom documents: {sorted(map(str, phantom))}"

    @invariant()
    def locks_balanced(self) -> None:
        acquires = sum(1 for call in self.fake.calls if call[0] == "lock_acquire")
        releases = sum(1 for call in self.fake.calls if call[0] == "lock_release")
        assert acquires == releases

    # -- engine-tracking emulation -------------------------------------------

    def _reconcile_round(self) -> list[tuple[str, _Output]]:
        handler = self.handlers[self.config_fp]
        outputs: list[tuple[str, _Output]] = []
        for key in sorted(set(self.declared) | set(self.tracking)):
            entry = self.tracking.get(key)
            prev: list[DocumentRecord] = []
            # A key with no tracking at all is a fresh item: the engine
            # forces prev_may_be_missing=True for those.
            missing = True
            if entry is not None:
                prev = list(entry.committed)
                if isinstance(entry.pending, DocumentRecord):
                    prev.append(entry.pending)
                # Pending write (record or deleted marker) => possibly
                # missing; lossy invalidation keeps the flag sticky.
                missing = entry.may_be_missing or entry.pending is not None
            desired: CogneeDocumentSpec | coco.NonExistenceType = self.declared.get(
                key, coco.NON_EXISTENCE
            )
            output = handler.reconcile(key, desired, prev, missing)
            if output is not None:
                outputs.append((key, output))
        return outputs

    def _precommit(self, outputs: list[tuple[str, _Output]]) -> None:
        for key, output in outputs:
            entry = self.tracking.get(key)
            if entry is None:
                entry = _TrackEntry(committed=[], pending=None, may_be_missing=False)
                self.tracking[key] = entry
            entry.pending = output.tracking_record

    def _apply(self, actions: list[_DocumentAction]) -> None:
        if not actions:
            return
        handler = self.handlers[self.config_fp]
        self.loop.run_until_complete(handler._apply(cast(Any, None), actions))

    def _commit(self, outputs: list[tuple[str, _Output]]) -> None:
        for key, output in outputs:
            if coco.is_non_existence(output.tracking_record):
                self.tracking.pop(key, None)
            else:
                self.tracking[key] = _TrackEntry(
                    committed=[output.tracking_record],
                    pending=None,
                    may_be_missing=False,
                )

    # -- reference model -----------------------------------------------------

    def _assert_converged(self) -> None:
        profile = PROFILES[self.config_fp]
        dataset = self.fake.dataset(TENANT, DATASET)
        expected_ids = {_data_id(key) for key in self.declared}
        actual_ids = set(dataset.documents) if dataset is not None else set()
        assert actual_ids == expected_ids

        for key, spec in self.declared.items():
            document = self.fake.document(TENANT, DATASET, _data_id(key))
            assert document is not None
            assert document.payload.content == spec.content
            assert document.payload.label == spec.label
            assert document.payload.node_set == spec.node_set
            assert document.cognify_complete
            # Exactly the current content's derivatives — no orphans from
            # earlier versions (the replace protocol's whole point).
            assert document.derived_fragments == {fingerprint_content(spec.content)}
            assert document.derived_profile == profile

        assert self.fake.unconverged_documents(TENANT, DATASET, profile=profile) == []
        # Tracking mirrors declarations exactly after a successful sync.
        assert set(self.tracking) == set(self.declared)
        # Fixed point: an immediate re-reconciliation has nothing to do.
        assert self._reconcile_round() == []


ConvergenceMachine.TestCase.settings = settings(
    max_examples=60,
    stateful_step_count=40,
    deadline=None,
)

TestConvergence = ConvergenceMachine.TestCase
