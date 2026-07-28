"""Full lifecycle of the cogindex two-level target through the real CocoIndex
engine, against :class:`cogindex.testing.FakeCogneeRuntime`.

Covers create, no-op convergence, in-place replacement, metadata-only
updates, deletion, processing-config invalidation (ADR-0005), and unmount
ownership semantics (system-managed teardown vs. user-managed hands-off).

These are fake-runtime tests, not integration tests (AGENTS.md testing rules).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TypeAlias

import cocoindex as coco

import cogindex
from cogindex.testing import FakeCogneeRuntime
from tests.common import create_test_env

# ContextKey names are globally unique per process: exactly one for this module.
_RUNTIME_KEY_NAME = "cognee_runtime_engine_tests"
_RUNTIME_KEY = coco.ContextKey[cogindex.CogneeRuntime](_RUNTIME_KEY_NAME)
_IDENTITY_SCOPE = "fake-default"

_PROFILE = cogindex.CognifyProfile()
# Explicit ProcessingConfig so processing_config_from_profile (which imports
# cognee via the compat layer) is never triggered in unit tests.
_PROCESSING = cogindex.ProcessingConfig(graph_model_id="t.Model")
_PROCESSING_V2 = cogindex.ProcessingConfig(graph_model_id="t.ModelV2")

_MUTATING_OPS = frozenset(
    {
        "add_documents",
        "cognify_dataset",
        "delete_documents",
        "purge_document_memory",
        "teardown_dataset",
    }
)

_Call: TypeAlias = tuple[str, str, tuple[str, ...]]


@coco.fn
async def _declare_docs_main(
    dataset: str,
    docs: dict[str, str],
    labels: dict[str, str],
    processing: cogindex.ProcessingConfig,
    managed_by: str,
) -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target,
        _RUNTIME_KEY,
        dataset,
        profile=_PROFILE,
        processing=processing,
        managed_by=managed_by,
    )
    for key, content in docs.items():
        target.declare_document(key, content, label=labels.get(key))


@coco.fn
async def _no_mount_main() -> None:
    """Mounts nothing: running this unmounts everything previously declared."""


def _setup(suffix: str) -> tuple[coco.Environment, FakeCogneeRuntime]:
    env = create_test_env(__file__, suffix=suffix)
    fake = FakeCogneeRuntime()
    env.context_provider.provide(_RUNTIME_KEY, fake)
    return env, fake


def _run_app(
    env: coco.Environment,
    app_name: str,
    *,
    dataset: str,
    docs: dict[str, str],
    labels: dict[str, str] | None = None,
    processing: cogindex.ProcessingConfig = _PROCESSING,
    managed_by: str = "system",
) -> None:
    # Created in function scope on purpose: the environment's app registry is
    # a WeakValueDictionary, so the App must be released before the same name
    # can be registered again (re-runs and the unmount tests rely on this).
    app = coco.App(
        coco.AppConfig(name=app_name, environment=env),
        _declare_docs_main,
        dataset=dataset,
        docs=docs,
        labels=labels if labels is not None else {},
        processing=processing,
        managed_by=managed_by,
    )
    app.update_blocking()


def _run_empty_app(env: coco.Environment, app_name: str) -> None:
    app = coco.App(coco.AppConfig(name=app_name, environment=env), _no_mount_main)
    app.update_blocking()


def _data_id(dataset: str, external_key: str) -> uuid.UUID:
    return cogindex.document_data_id(
        _RUNTIME_KEY_NAME,
        _IDENTITY_SCOPE,
        "default",
        dataset,
        external_key,
    )


def _ops(fake: FakeCogneeRuntime, op: str) -> list[_Call]:
    return [call for call in fake.calls if call[0] == op]


def _mutating(fake: FakeCogneeRuntime) -> list[_Call]:
    return [call for call in fake.calls if call[0] in _MUTATING_OPS]


# =============================================================================
# Tests
# =============================================================================


def test_create_two_documents() -> None:
    env, fake = _setup("create")
    dataset = "docs_create"
    _run_app(env, "engine_lc_create", dataset=dataset, docs={"a.md": "alpha", "b.md": "beta"})

    id_a = _data_id(dataset, "a.md")
    id_b = _data_id(dataset, "b.md")
    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert sorted(ds.documents, key=str) == sorted([id_a, id_b], key=str)
    assert fake.unconverged_documents("default", dataset) == []

    adds = _ops(fake, "add_documents")
    cognifies = _ops(fake, "cognify_dataset")
    assert len(adds) == 1
    assert set(adds[0][2]) == {str(id_a), str(id_b)}
    assert len(cognifies) == 1
    # Documents are added before the (single) cognify (ADR-0004 phase order).
    assert fake.calls.index(adds[0]) < fake.calls.index(cognifies[0])


def test_noop_rerun_makes_no_mutating_calls() -> None:
    env, fake = _setup("noop")
    dataset = "docs_noop"
    docs = {"a.md": "alpha", "b.md": "beta"}
    _run_app(env, "engine_lc_noop", dataset=dataset, docs=docs)
    mutating_before = _mutating(fake)

    _run_app(env, "engine_lc_noop", dataset=dataset, docs=docs)

    assert _mutating(fake) == mutating_before
    assert fake.unconverged_documents("default", dataset) == []


def test_replace_changed_content_purges_only_that_document() -> None:
    env, fake = _setup("replace")
    dataset = "docs_replace"
    _run_app(env, "engine_lc_replace", dataset=dataset, docs={"a.md": "alpha", "b.md": "beta"})

    _run_app(env, "engine_lc_replace", dataset=dataset, docs={"a.md": "alpha v2", "b.md": "beta"})

    id_a = _data_id(dataset, "a.md")
    purges = _ops(fake, "purge_document_memory")
    assert len(purges) == 1
    assert purges[0][2] == (str(id_a),)

    adds = _ops(fake, "add_documents")
    assert adds[-1][2] == (str(id_a),)

    doc_a = fake.document("default", dataset, id_a)
    assert doc_a is not None
    assert doc_a.payload.content == "alpha v2"
    assert fake.unconverged_documents("default", dataset) == []


def test_metadata_only_change_readds_without_purge_or_cognify() -> None:
    env, fake = _setup("metadata")
    dataset = "docs_metadata"
    docs = {"a.md": "alpha", "b.md": "beta"}
    _run_app(env, "engine_lc_metadata", dataset=dataset, docs=docs, labels={"a.md": "first"})

    _run_app(env, "engine_lc_metadata", dataset=dataset, docs=docs, labels={"a.md": "second"})

    id_a = _data_id(dataset, "a.md")
    adds = _ops(fake, "add_documents")
    assert len(adds) == 2  # a new add happened for the metadata upsert
    assert adds[-1][2] == (str(id_a),)
    # Benign metadata change: no derivative purge, no re-cognify (ADR-0005).
    assert len(_ops(fake, "cognify_dataset")) == 1
    assert _ops(fake, "purge_document_memory") == []

    doc_a = fake.document("default", dataset, id_a)
    assert doc_a is not None
    assert doc_a.payload.label == "second"
    assert fake.unconverged_documents("default", dataset) == []


def test_delete_undeclared_document() -> None:
    env, fake = _setup("delete")
    dataset = "docs_delete"
    _run_app(env, "engine_lc_delete", dataset=dataset, docs={"a.md": "alpha", "b.md": "beta"})

    _run_app(env, "engine_lc_delete", dataset=dataset, docs={"a.md": "alpha"})

    id_a = _data_id(dataset, "a.md")
    id_b = _data_id(dataset, "b.md")
    deletes = _ops(fake, "delete_documents")
    assert len(deletes) == 1
    assert deletes[0][2] == (str(id_b),)

    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert sorted(ds.documents, key=str) == [id_a]
    assert fake.unconverged_documents("default", dataset) == []


def test_user_managed_target_still_deletes_undeclared_document() -> None:
    # managed_by controls whole-dataset teardown on unmount. It does not
    # change the desired document set while the target remains mounted.
    env, fake = _setup("delete_user_managed")
    dataset = "docs_delete_user_managed"
    app_name = "engine_lc_delete_user_managed"
    _run_app(
        env,
        app_name,
        dataset=dataset,
        docs={"a.md": "alpha", "b.md": "beta"},
        managed_by="user",
    )
    fake.calls.clear()

    _run_app(
        env,
        app_name,
        dataset=dataset,
        docs={"a.md": "alpha"},
        managed_by="user",
    )

    id_a = _data_id(dataset, "a.md")
    id_b = _data_id(dataset, "b.md")
    assert _ops(fake, "delete_documents") == [("delete_documents", dataset, (str(id_b),))]
    assert _ops(fake, "teardown_dataset") == []
    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert sorted(ds.documents, key=str) == [id_a]
    assert fake.unconverged_documents("default", dataset) == []


def test_processing_config_change_purges_and_recognifies_all() -> None:
    env, fake = _setup("config_change")
    dataset = "docs_config"
    docs = {"a.md": "alpha", "b.md": "beta"}
    _run_app(env, "engine_lc_config", dataset=dataset, docs=docs, processing=_PROCESSING)
    assert _ops(fake, "purge_document_memory") == []
    cognifies_before = len(_ops(fake, "cognify_dataset"))

    _run_app(env, "engine_lc_config", dataset=dataset, docs=docs, processing=_PROCESSING_V2)

    id_a = _data_id(dataset, "a.md")
    id_b = _data_id(dataset, "b.md")
    purges = _ops(fake, "purge_document_memory")
    purged_ids = {data_id for call in purges for data_id in call[2]}
    assert purged_ids == {str(id_a), str(id_b)}
    assert len(_ops(fake, "cognify_dataset")) == cognifies_before + 1
    assert fake.unconverged_documents("default", dataset) == []
    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert sorted(ds.documents, key=str) == sorted([id_a, id_b], key=str)


def test_unrelated_config_change_reprocesses_nothing() -> None:
    # The other half of ADR-0005: a knob that cannot change derivatives must
    # not be able to trigger a rebuild. Between the two runs the runtime object
    # and its lock provider are replaced, which is what a deployment change
    # touches, while the ProcessingConfig stays byte-identical.
    env, fake = _setup("unrelated_config")
    dataset = "docs_unrelated"
    docs = {"a.md": "alpha", "b.md": "beta"}
    _run_app(env, "engine_lc_unrelated", dataset=dataset, docs=docs)
    mutating_before = _mutating(fake)

    swapped = FakeCogneeRuntime(lock_provider=cogindex.InProcessLockProvider(timeout=30))
    swapped.datasets = fake.datasets
    # resolve_dataset is not part of the recorded call log, so count it here:
    # without this the assertions below would also pass if the engine had
    # quietly kept using the old runtime.
    resolves = 0
    original_resolve = swapped.resolve_dataset

    async def counting_resolve(name: str, tenant: str) -> cogindex.DatasetHandle:
        nonlocal resolves
        resolves += 1
        return await original_resolve(name, tenant)

    swapped.resolve_dataset = counting_resolve  # type: ignore[method-assign]
    env.context_provider.provide(_RUNTIME_KEY, swapped)
    _run_app(env, "engine_lc_unrelated", dataset=dataset, docs=docs)

    assert resolves > 0, "the second run never reached the replacement runtime"
    assert _mutating(swapped) == []
    assert _mutating(fake) == mutating_before
    assert swapped.unconverged_documents("default", dataset) == []


def test_unmount_system_managed_removes_all_documents() -> None:
    env, fake = _setup("unmount_system")
    dataset = "docs_unmount_sys"
    _run_app(env, "engine_lc_unmount_sys", dataset=dataset, docs={"a.md": "alpha", "b.md": "beta"})
    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert len(ds.documents) == 2

    # Same app name, same environment, but the main function mounts nothing:
    # every previously declared target state is unmounted.
    _run_empty_app(env, "engine_lc_unmount_sys")

    # End-state contract only: cleanup may arrive as per-document deletes,
    # teardown_dataset, or both. Hard teardown removes the dataset itself.
    assert fake.dataset("default", dataset) is None


def test_unmount_lock_failure_leaves_intent_uncommitted_and_retries() -> None:
    env, fake = _setup("unmount_lock_failure")
    dataset = "docs_unmount_lock_failure"
    app_name = "engine_lc_unmount_lock_failure"
    # Track the system-managed dataset without child records, then materialize
    # one external row. The unmount therefore has only the container teardown
    # action, so the injected lock failure cannot be consumed by a child batch.
    _run_app(env, app_name, dataset=dataset, docs={})
    data_id = _data_id(dataset, "external.md")
    asyncio.run(
        fake.add_documents(
            cogindex.DatasetHandle(
                name=dataset,
                tenant="default",
                identity_scope=_IDENTITY_SCOPE,
            ),
            [cogindex.DocumentPayload(data_id=data_id, content="external row")],
        )
    )
    fake.calls.clear()
    fake.inject_fault("dataset_lock")

    # CocoIndex reports component cleanup failures on the update result/log
    # rather than re-raising them from update_blocking. The sink itself is
    # contract-tested separately to propagate the exception.
    _run_empty_app(env, app_name)

    assert fake.document("default", dataset, data_id) is not None
    assert _ops(fake, "teardown_dataset") == []

    # The failed sink did not commit NON_EXISTENCE: retry still receives the
    # teardown intent, acquires the lock and removes the row.
    _run_empty_app(env, app_name)
    assert fake.document("default", dataset, data_id) is None
    assert len(_ops(fake, "teardown_dataset")) == 1


def test_unmount_user_managed_leaves_documents_untouched() -> None:
    env, fake = _setup("unmount_user")
    dataset = "docs_unmount_user"
    _run_app(
        env,
        "engine_lc_unmount_user",
        dataset=dataset,
        docs={"a.md": "alpha", "b.md": "beta"},
        managed_by="user",
    )
    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert len(ds.documents) == 2
    mutating_before = _mutating(fake)

    _run_empty_app(env, "engine_lc_unmount_user")

    # Ownership contract: user-managed datasets are never torn down. The
    # documents survive the unmount with their content untouched.
    assert _ops(fake, "teardown_dataset") == []
    assert _mutating(fake) == mutating_before
    id_a = _data_id(dataset, "a.md")
    id_b = _data_id(dataset, "b.md")
    ds = fake.dataset("default", dataset)
    assert ds is not None
    assert sorted(ds.documents, key=str) == sorted([id_a, id_b], key=str)
    doc_a = fake.document("default", dataset, id_a)
    doc_b = fake.document("default", dataset, id_b)
    assert doc_a is not None and doc_a.payload.content == "alpha"
    assert doc_b is not None and doc_b.payload.content == "beta"
