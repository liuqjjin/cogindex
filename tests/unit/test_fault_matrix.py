"""Deterministic fault-injection matrix (ADR-0003/0004).

Each test crashes one specific phase of the write protocol, asserts the
exact mid-crash external state (what landed, what didn't), then proves the
next sync converges. Complements the randomized Hypothesis machine in
tests/property/ with named, reviewable scenarios:

 1. crash before any external write (lock acquisition)
 2. crash after hard deletes, before purges
 3. crash after purge, before the add
 4. partial add (1 of 3 payloads landed)
 5. crash after adds, before cognify
 6. repeated faults, then success
 7. crash in a delete-only batch
 8. processing-config change crashing mid-invalidation
 9. stale-identity cleanup crashing mid-delete

Plus: concurrent batches on one dataset are serialized by the dataset lock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping

import cogindex
from cogindex import CognifyProfile, DatasetHandle, DocumentPayload
from cogindex._identity import fingerprint_content
from cogindex._spec import CogneeDocumentSpec, document_record_for
from cogindex._target import DocumentHandler
from cogindex.testing import FakeCogneeRuntime, InjectedFault
from tests.common.engine_model import EmulatedEngine, TrackEntry

RUNTIME_KEY = "rt-fault"
TENANT = "default"
DATASET = "ds-fault"

PROFILE_A = CognifyProfile(chunk_size=100)
PROFILE_B = CognifyProfile(chunk_size=200)


def make_handler(fake: FakeCogneeRuntime, fp: str, profile: CognifyProfile) -> DocumentHandler:
    return DocumentHandler(
        runtime=fake,
        runtime_key=RUNTIME_KEY,
        handle=DatasetHandle(name=DATASET, tenant=TENANT),
        profile=profile,
        processing_fingerprint=fp,
    )


def make_stack() -> tuple[FakeCogneeRuntime, EmulatedEngine]:
    fake = FakeCogneeRuntime()
    return fake, EmulatedEngine(make_handler(fake, "pfp-A", PROFILE_A))


def spec(content: str) -> CogneeDocumentSpec:
    return CogneeDocumentSpec(content=content)


def did(key: str) -> uuid.UUID:
    return cogindex.document_data_id(RUNTIME_KEY, TENANT, DATASET, key)


def ops(fake: FakeCogneeRuntime, op: str) -> list[tuple[str, str, tuple[str, ...]]]:
    return [call for call in fake.calls if call[0] == op]


def assert_converged(
    fake: FakeCogneeRuntime,
    engine: EmulatedEngine,
    declared: Mapping[str, CogneeDocumentSpec],
    profile: CognifyProfile,
) -> None:
    dataset = fake.dataset(TENANT, DATASET)
    expected_ids = {did(key) for key in declared}
    actual_ids = set(dataset.documents) if dataset is not None else set()
    assert actual_ids == expected_ids
    for key, document_spec in declared.items():
        document = fake.document(TENANT, DATASET, did(key))
        assert document is not None
        assert document.cognify_complete
        assert document.payload.content == document_spec.content
        assert document.derived_fragments == {fingerprint_content(document_spec.content)}
        assert document.derived_profile == profile
    assert fake.unconverged_documents(TENANT, DATASET, profile=profile) == []
    engine.assert_fixed_point(declared)


# =============================================================================
# 1. Crash before any external write
# =============================================================================


async def test_crash_before_any_write_then_recover() -> None:
    fake, engine = make_stack()
    declared = {"a.md": spec("alpha"), "b.md": spec("beta")}

    fake.inject_fault("dataset_lock")
    assert await engine.sync_expect_crash(declared, InjectedFault)
    # Nothing external happened — not even a lock acquisition was recorded.
    assert fake.dataset(TENANT, DATASET) is None
    assert fake.calls == []

    await engine.sync(declared)
    assert_converged(fake, engine, declared, PROFILE_A)
    # Recovery needed exactly one batch: no duplicated work from the crash.
    assert len(ops(fake, "add_documents")) == 1
    assert len(ops(fake, "cognify_dataset")) == 1


# =============================================================================
# 2. Crash after hard deletes, before purges
# =============================================================================


async def test_crash_after_deletes_before_purges() -> None:
    fake, engine = make_stack()
    await engine.sync({"a.md": spec("alpha"), "b.md": spec("beta v1")})

    replaced = {"b.md": spec("beta v2")}  # a.md removed, b.md replaced
    fake.inject_fault("purge_document_memory")
    assert await engine.sync_expect_crash(replaced, InjectedFault)

    # The hard delete of a.md landed (deletes run first, ADR-0004)...
    dataset = fake.dataset(TENANT, DATASET)
    assert dataset is not None
    assert set(dataset.documents) == {did("b.md")}
    # ...but b.md is untouched: old content, old derivatives still intact.
    document_b = fake.document(TENANT, DATASET, did("b.md"))
    assert document_b is not None
    assert document_b.payload.content == "beta v1"
    assert document_b.derived_fragments == {fingerprint_content("beta v1")}

    await engine.sync(replaced)
    assert_converged(fake, engine, replaced, PROFILE_A)


# =============================================================================
# 3. Crash after purge, before the add
# =============================================================================


async def test_crash_after_purge_before_add() -> None:
    fake, engine = make_stack()
    await engine.sync({"a.md": spec("v1")})

    fake.inject_fault("add_documents", after_items=0)
    assert await engine.sync_expect_crash({"a.md": spec("v2")}, InjectedFault)

    document = fake.document(TENANT, DATASET, did("a.md"))
    assert document is not None
    assert document.payload.content == "v1"  # the add never landed
    assert document.derived_fragments == set()  # the purge did
    assert document.cognify_complete is False

    await engine.sync({"a.md": spec("v2")})
    assert_converged(fake, engine, {"a.md": spec("v2")}, PROFILE_A)


# =============================================================================
# 4. Partial add: 1 of 3 payloads landed
# =============================================================================


async def test_partial_add_crash() -> None:
    fake, engine = make_stack()
    declared = {"a.md": spec("A"), "b.md": spec("B"), "c.md": spec("C")}

    fake.inject_fault("add_documents", after_items=1)
    assert await engine.sync_expect_crash(declared, InjectedFault)

    dataset = fake.dataset(TENANT, DATASET)
    assert dataset is not None
    assert len(dataset.documents) == 1
    (landed,) = dataset.documents.values()
    assert landed.cognify_complete is False  # cognify never ran

    await engine.sync(declared)
    assert_converged(fake, engine, declared, PROFILE_A)


# =============================================================================
# 5. Crash after adds, before cognify
# =============================================================================


async def test_crash_after_adds_before_cognify() -> None:
    fake, engine = make_stack()
    declared = {"a.md": spec("alpha")}

    fake.inject_fault("cognify_dataset")
    assert await engine.sync_expect_crash(declared, InjectedFault)

    document = fake.document(TENANT, DATASET, did("a.md"))
    assert document is not None
    assert document.payload.content == "alpha"
    assert document.cognify_complete is False

    await engine.sync(declared)
    assert_converged(fake, engine, declared, PROFILE_A)
    assert len(ops(fake, "cognify_dataset")) == 2  # crashed + recovery


# =============================================================================
# 6. Repeated faults, then success
# =============================================================================


async def test_repeated_faults_then_success() -> None:
    fake, engine = make_stack()
    await engine.sync({"a.md": spec("v1")})

    replaced = {"a.md": spec("v2")}
    fake.inject_fault("cognify_dataset", times=2)
    assert await engine.sync_expect_crash(replaced, InjectedFault)
    assert await engine.sync_expect_crash(replaced, InjectedFault)

    await engine.sync(replaced)
    # Convergence includes: exactly {v2} derivatives — the repeated
    # purge/add cycles left no orphans behind.
    assert_converged(fake, engine, replaced, PROFILE_A)


# =============================================================================
# 7. Crash in a delete-only batch
# =============================================================================


async def test_delete_only_batch_fault() -> None:
    fake, engine = make_stack()
    await engine.sync({"a.md": spec("alpha")})

    fake.inject_fault("delete_documents")
    assert await engine.sync_expect_crash({}, InjectedFault)

    await engine.sync({})
    dataset = fake.dataset(TENANT, DATASET)
    assert dataset is not None
    assert dataset.documents == {}
    assert engine.tracking == {}
    engine.assert_fixed_point({})


# =============================================================================
# 8. Processing-config change crashing mid-invalidation
# =============================================================================


async def test_config_change_crash_mid_invalidation() -> None:
    fake, engine = make_stack()
    declared = {"a.md": spec("alpha"), "b.md": spec("beta")}
    await engine.sync(declared)

    # Dataset-level config replace: new handler + lossy child invalidation.
    engine.handler = make_handler(fake, "pfp-B", PROFILE_B)
    engine.invalidate_lossy()

    fake.inject_fault("purge_document_memory")
    assert await engine.sync_expect_crash(declared, InjectedFault)

    await engine.sync(declared)
    # Everything rebuilt under the new configuration, nothing stale.
    assert_converged(fake, engine, declared, PROFILE_B)


# =============================================================================
# 9. Stale-identity cleanup crashing mid-delete
# =============================================================================


async def test_stale_identity_crash_mid_cleanup() -> None:
    fake, engine = make_stack()
    spec_a = spec("alpha")

    # Seed an externally-existing document + tracking under a drifted
    # (old-identity-schema) data_id.
    old_id = uuid.uuid5(uuid.NAMESPACE_URL, "cogindex-old-schema-id")
    handle = await fake.add_documents(
        DatasetHandle(name=DATASET, tenant=TENANT),
        [DocumentPayload(data_id=old_id, content="alpha")],
    )
    await fake.cognify_dataset(handle, PROFILE_A)
    engine.tracking["a.md"] = TrackEntry(
        committed=[document_record_for(spec_a, data_id=old_id, processing_fingerprint="pfp-A")],
        pending=None,
        may_be_missing=False,
    )

    fake.inject_fault("delete_documents")
    assert await engine.sync_expect_crash({"a.md": spec_a}, InjectedFault)

    await engine.sync({"a.md": spec_a})
    # The drifted identity is gone, the current one exists and is converged.
    dataset = fake.dataset(TENANT, DATASET)
    assert dataset is not None
    assert old_id not in dataset.documents
    assert_converged(fake, engine, {"a.md": spec_a}, PROFILE_A)


# =============================================================================
# Concurrency: batches on one dataset serialize under the dataset lock
# =============================================================================


async def test_concurrent_batches_serialize_under_dataset_lock() -> None:
    fake = FakeCogneeRuntime()
    handler = make_handler(fake, "pfp-A", PROFILE_A)
    engine_one = EmulatedEngine(handler)
    engine_two = EmulatedEngine(handler)
    batch_one = {f"one-{i}.md": spec(f"o{i}") for i in range(3)}
    batch_two = {f"two-{i}.md": spec(f"t{i}") for i in range(3)}

    await asyncio.gather(engine_one.sync(batch_one), engine_two.sync(batch_two))

    # Partition the call log into lock-delimited segments; overlapping
    # batches would either nest acquires or mix their adds in one segment.
    segments: list[list[tuple[str, str, tuple[str, ...]]]] = []
    current: list[tuple[str, str, tuple[str, ...]]] | None = None
    for call in fake.calls:
        if call[0] == "lock_acquire":
            assert current is None, "nested lock acquisition observed"
            current = []
        elif call[0] == "lock_release":
            assert current is not None
            segments.append(current)
            current = None
        elif current is not None:
            current.append(call)
    assert current is None
    assert len(segments) == 2

    ids_one = {str(did(key)) for key in batch_one}
    ids_two = {str(did(key)) for key in batch_two}
    for segment in segments:
        segment_ids = {item for call in segment if call[0] == "add_documents" for item in call[2]}
        assert segment_ids in (ids_one, ids_two)

    # Both batches fully converged.
    dataset = fake.dataset(TENANT, DATASET)
    assert dataset is not None
    assert {str(document_id) for document_id in dataset.documents} == ids_one | ids_two
    assert fake.unconverged_documents(TENANT, DATASET, profile=PROFILE_A) == []
