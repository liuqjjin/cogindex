"""Model-based convergence property (ADR-0003).

A Hypothesis state machine samples bounded interleavings of document
declarations, removals, processing-config changes, successful syncs, sink
failures (lock acquisition, deletes, purges, partial adds, cognify), and an
exit after successful apply but before tracking commit. Tracking behavior is
the explicit model in tests/common/engine_model.py.

For every generated sequence, a completed ``sync_ok`` must leave Fake Cognee
equal to the reference state (exactly the declared documents, fresh
derivatives, current config) and reconciliation at a fixed point. Safety
checks also run after failed steps. This is randomized evidence over the
modeled transitions, not an exhaustive proof of every CocoIndex or Cognee
failure mode; named deterministic regressions pin the known critical
sequences.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

import cogindex
from cogindex import CognifyProfile, DatasetHandle
from cogindex._identity import fingerprint_content
from cogindex._spec import CogneeDocumentSpec
from cogindex._target import DocumentHandler
from cogindex.testing import FakeCogneeRuntime, InjectedFault
from tests.common.engine_model import EmulatedEngine

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


class ConvergenceMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        # One persistent loop: asyncio primitives in the fake's lock provider
        # bind to the loop they first run on.
        self.loop = asyncio.new_event_loop()
        self.fake = FakeCogneeRuntime()
        self.declared: dict[str, CogneeDocumentSpec] = {}
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
        self.engine = EmulatedEngine(self.handlers[self.config_fp])
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
        self.engine.handler = self.handlers[fp]
        # Engine behavior on dataset config replace: lossy child invalidation.
        self.engine.invalidate_lossy()

    # -- sync transitions ----------------------------------------------------

    @rule()
    def sync_ok(self) -> None:
        self.loop.run_until_complete(self.engine.sync(self.declared))
        self._assert_converged()

    @rule(fault=st.sampled_from(FAULT_OPS), after=st.integers(min_value=0, max_value=2))
    def sync_crash_mid_batch(self, fault: str, after: int) -> None:
        if not self.engine.reconcile_round(self.declared):
            return
        if fault == "add_documents":
            self.fake.inject_fault(fault, after_items=after)
        else:
            self.fake.inject_fault(fault)
        try:
            self.loop.run_until_complete(
                self.engine.sync_expect_crash(self.declared, InjectedFault)
            )
        finally:
            self.fake.clear_faults()

    @rule()
    def sync_crash_before_apply(self) -> None:
        """Process dies between precommit and the first external write."""
        outputs = self.engine.reconcile_round(self.declared)
        self.engine.precommit(outputs)

    @rule()
    def sync_exit_after_apply(self) -> None:
        """External apply succeeds, then the process exits before commit."""
        self.loop.run_until_complete(self.engine.sync_exit_after_apply(self.declared))

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
            # Exactly the current content's derivatives, no orphans from
            # earlier versions (the replace protocol's whole point).
            assert document.derived_fragments == {fingerprint_content(spec.content)}
            assert document.derived_profile == profile

        assert self.fake.unconverged_documents(TENANT, DATASET, profile=profile) == []
        # Tracking mirrors declarations exactly after a successful sync.
        assert set(self.engine.tracking) == set(self.declared)
        # Fixed point: an immediate re-reconciliation has nothing to do.
        self.engine.assert_fixed_point(self.declared)


ConvergenceMachine.TestCase.settings = settings(
    max_examples=60,
    stateful_step_count=40,
    deadline=None,
)

TestConvergence = ConvergenceMachine.TestCase
