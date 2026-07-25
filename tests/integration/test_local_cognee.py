"""Real local Cognee stack integration (marker: integration).

Runs LocalCogneeRuntime against a genuine Cognee deployment — SQLite
relational store, LanceDB vector store, embedded graph database, real
ingestion/chunking/provenance — with deterministic substitutes for the two
nondeterministic components only: the LLM (patched ``LLMGateway``, as
upstream's own pipeline tests do) and embeddings (cognee's
``MOCK_EMBEDDING`` switch). No network. These are integration tests of the
upstream contract; they are NOT end-to-end LLM tests (that opt-in tier
lives in test_local_cognee_llm.py).

The whole module shares one event loop (loop_scope="module"): cognee caches
async engines globally and they must not cross loops.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import cocoindex as coco
import pytest

import cogindex
from cogindex import (
    CognifyProfile,
    DatasetHandle,
    DocumentPayload,
    ExpectedDocument,
    LocalCogneeRuntime,
    verify_dataset,
)
from tests.common import create_test_env

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

RUNTIME_KEY_NAME = "cognee_runtime_integration"
_RUNTIME_KEY = coco.ContextKey[cogindex.CogneeRuntime](RUNTIME_KEY_NAME)
TENANT = "default"

# Deterministic entity vocabulary for the fake LLM: any of these names
# appearing in a document's text becomes a graph node.
ENTITY_TYPES = {
    "AlphaCorp": "Company",
    "BetaCorp": "Company",
    "SharedOrg": "Organization",
    "Bob": "Person",
    "Carol": "Person",
}


def did(dataset: str, key: str) -> uuid.UUID:
    return cogindex.document_data_id(RUNTIME_KEY_NAME, TENANT, dataset, key)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def storage_roots(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    base = tmp_path_factory.mktemp("cognee-integration")
    return str(base / "data"), str(base / "system")


@pytest.fixture
async def runtime(storage_roots: tuple[str, str]) -> AsyncIterator[LocalCogneeRuntime]:
    """A LocalCogneeRuntime over pristine storage (pruned before each test)."""
    import cognee
    from cognee.modules.engine.operations.setup import setup

    data_root, system_root = storage_roots
    local_runtime = LocalCogneeRuntime(data_root=data_root, system_root=system_root)
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()
    yield local_runtime


@pytest.fixture
def llm_mock() -> Iterator[AsyncMock]:
    """Deterministic LLM: entity extraction from a fixed vocabulary."""
    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    def graph_for(text: str) -> Any:
        nodes = [
            Node(id=name, name=name, type=etype, description=f"{name} is a {etype}")
            for name, etype in ENTITY_TYPES.items()
            if name in text
        ]
        people = [n for n in nodes if n.type == "Person"]
        organizations = [n for n in nodes if n.type != "Person"]
        edges = [
            Edge(
                source_node_id=person.id,
                target_node_id=organization.id,
                relationship_name="works_for",
            )
            for person in people
            for organization in organizations
        ]
        return KnowledgeGraph(nodes=nodes, edges=edges)

    def fake_llm(text_input: str, system_prompt: str, response_model: type, **kwargs: Any) -> Any:
        if text_input == "test":  # cognee's LLM connection self-check
            return "test"
        if response_model is SummarizedContent:
            return SummarizedContent(
                summary="deterministic summary", description="deterministic summary"
            )
        if response_model is KnowledgeGraph:
            return graph_for(text_input)
        raise AssertionError(f"unmocked LLM response model: {response_model!r}")

    with patch.object(LLMGateway, "acreate_structured_output", new_callable=AsyncMock) as mock:
        mock.side_effect = fake_llm
        yield mock


async def graph_node_names(dataset_id: uuid.UUID) -> set[str]:
    """Lower-cased names of all nodes in the dataset's graph."""
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    async with set_database_global_context_variables(dataset_id, user.id):
        engine = await get_graph_engine()
        nodes, _ = await engine.get_graph_data()
    names: set[str] = set()
    for _, properties in nodes:
        if isinstance(properties, dict) and properties.get("name"):
            names.add(str(properties["name"]).lower())
    return names


# ---------------------------------------------------------------------------
# runtime-level contract: lifecycle, replace protocol, shared entities
# ---------------------------------------------------------------------------


async def test_lifecycle_replace_and_shared_entity_provenance(
    runtime: LocalCogneeRuntime, llm_mock: AsyncMock
) -> None:
    dataset = "int_lifecycle"
    id_bob = did(dataset, "bob.md")
    id_carol = did(dataset, "carol.md")
    handle = await runtime.add_documents(
        DatasetHandle(name=dataset, tenant=TENANT),
        [
            DocumentPayload(data_id=id_bob, content="Bob works for SharedOrg and AlphaCorp."),
            DocumentPayload(data_id=id_carol, content="Carol works for SharedOrg."),
        ],
    )
    assert handle.dataset_id is not None
    await runtime.cognify_dataset(handle, CognifyProfile())

    stored = {d.data_id: d for d in await runtime.list_documents(handle)}
    assert set(stored) == {id_bob, id_carol}
    assert all(document.cognify_complete for document in stored.values())
    names = await graph_node_names(handle.dataset_id)
    assert {"bob", "carol", "sharedorg", "alphacorp"} <= names

    # -- replace bob.md (ADR-0004: purge -> re-add same data_id -> cognify) --
    await runtime.purge_document_memory(handle, [id_bob])
    await runtime.add_documents(
        handle, [DocumentPayload(data_id=id_bob, content="Bob works for BetaCorp.")]
    )
    await runtime.cognify_dataset(handle, CognifyProfile())

    names = await graph_node_names(handle.dataset_id)
    assert "betacorp" in names
    # AlphaCorp was referenced only by bob.md's old content: gone.
    assert "alphacorp" not in names
    # SharedOrg is still referenced by carol.md: preserved (provenance).
    assert "sharedorg" in names

    # -- delete carol.md: SharedOrg loses its last reference ------------------
    await runtime.delete_documents(handle, [id_carol])
    stored = {d.data_id: d for d in await runtime.list_documents(handle)}
    assert set(stored) == {id_bob}
    names = await graph_node_names(handle.dataset_id)
    assert "sharedorg" not in names
    assert "betacorp" in names

    # -- idempotent deletes: repeating and deleting missing ids succeed -------
    await runtime.delete_documents(handle, [id_carol])
    await runtime.purge_document_memory(handle, [id_carol])


async def test_batch_purge_opens_one_database_context_for_the_whole_batch(
    runtime: LocalCogneeRuntime, llm_mock: AsyncMock
) -> None:
    """Purge cost stays flat in the size of the change set.

    Cognee scopes its graph engine per dataset and shuts the graph worker down
    when that scope closes, on a blocking thread join measured at roughly 2.7 s
    against about 0.07 s of real deletion work. A naive loop pays that per
    document, which is how replacing two documents came to cost more than
    ingesting six. Holding one context around the batch collapses it to a
    single teardown.

    Asserted structurally rather than as a wall-clock threshold: a timing bound
    would be flaky on shared CI hardware, while the call counts are exactly the
    property that matters and fail loudly if the loop is ever restructured.
    """
    import cogindex._compat as compat_module

    dataset = "int_batch_purge"
    ids = [did(dataset, f"doc-{i}.md") for i in range(5)]
    handle = await runtime.add_documents(
        DatasetHandle(name=dataset, tenant=TENANT),
        [
            DocumentPayload(data_id=data_id, content=f"Bob works for AlphaCorp, note {i}.")
            for i, data_id in enumerate(ids)
        ],
    )
    await runtime.cognify_dataset(handle, CognifyProfile())

    contexts_opened = 0
    forgets_issued = 0
    real_context = compat_module.dataset_database_context
    real_forget = compat_module.load().cognee.forget

    def counting_context(dataset_id: uuid.UUID, user_id: uuid.UUID | None) -> Any:
        nonlocal contexts_opened
        contexts_opened += 1
        return real_context(dataset_id, user_id)

    async def counting_forget(**kwargs: Any) -> Any:
        nonlocal forgets_issued
        forgets_issued += 1
        return await real_forget(**kwargs)

    with (
        patch.object(compat_module, "dataset_database_context", counting_context),
        patch.object(compat_module.load().cognee, "forget", counting_forget),
    ):
        await runtime.purge_document_memory(handle, ids)

    assert forgets_issued == len(ids), "one forget per document is the unit of deletion"
    assert contexts_opened == 1, (
        f"opened {contexts_opened} database contexts for {len(ids)} documents; "
        "the batch must share one or the graph-worker teardown is paid per document"
    )
    # The purge itself still worked: everything is back to un-cognified.
    assert all(not stored.cognify_complete for stored in await runtime.list_documents(handle))


async def test_metadata_only_readd_upserts_in_place_and_keeps_derivatives(
    runtime: LocalCogneeRuntime, llm_mock: AsyncMock
) -> None:
    """The upstream assumption behind the update_metadata write op.

    ADR-0005 routes a label or external-metadata change to a bare re-add: no
    derivative purge, no cognify. That is only safe if re-adding identical
    content leaves the item's pipeline status COMPLETED, so the subsequent
    cognify (whenever one next runs for another reason) still skips it, and if
    the metadata actually lands. Both halves were previously asserted only
    against the in-memory fake, which is circular: the fake was written from
    the same reading of upstream that the claim rests on.
    """
    dataset = "int_metadata"
    data_id = did(dataset, "doc.md")
    content = "Bob works for AlphaCorp."
    handle = await runtime.add_documents(
        DatasetHandle(name=dataset, tenant=TENANT),
        [DocumentPayload(data_id=data_id, content=content, label="first")],
    )
    await runtime.cognify_dataset(handle, CognifyProfile())
    assert handle.dataset_id is not None
    entities_before = await graph_node_names(handle.dataset_id)
    calls_after_cognify = llm_mock.call_count
    assert calls_after_cognify > 0

    await runtime.add_documents(
        handle,
        [
            DocumentPayload(
                data_id=data_id,
                content=content,
                label="second",
                external_metadata={"revision": 2},
            )
        ],
    )

    (stored,) = await runtime.list_documents(handle)
    assert stored.data_id == data_id
    assert stored.label == "second"
    # Status survived, so cognify's gate will keep skipping this item.
    assert stored.cognify_complete is True
    # Nothing was re-extracted: same content means no LLM work, which is the
    # entire reason metadata changes take the cheap path.
    assert llm_mock.call_count == calls_after_cognify
    assert await graph_node_names(handle.dataset_id) == entities_before

    # A cognify triggered by unrelated work must still skip this document.
    await runtime.cognify_dataset(handle, CognifyProfile())
    assert llm_mock.call_count == calls_after_cognify


async def test_incremental_cognify_skips_completed_items(
    runtime: LocalCogneeRuntime, llm_mock: AsyncMock
) -> None:
    dataset = "int_incremental"
    handle = await runtime.add_documents(
        DatasetHandle(name=dataset, tenant=TENANT),
        [
            DocumentPayload(data_id=did(dataset, "a.md"), content="Bob works for AlphaCorp."),
            DocumentPayload(data_id=did(dataset, "b.md"), content="Carol works for SharedOrg."),
        ],
    )
    await runtime.cognify_dataset(handle, CognifyProfile())
    calls_after_first = llm_mock.call_count
    assert calls_after_first > 0

    # Re-running cognify with nothing new must not reprocess anything.
    await runtime.cognify_dataset(handle, CognifyProfile())
    assert llm_mock.call_count == calls_after_first

    # A third document triggers processing again, and the first two stay done.
    await runtime.add_documents(
        handle,
        [DocumentPayload(data_id=did(dataset, "c.md"), content="BetaCorp exists.")],
    )
    await runtime.cognify_dataset(handle, CognifyProfile())
    assert llm_mock.call_count > calls_after_first
    stored = {d.data_id: d for d in await runtime.list_documents(handle)}
    assert len(stored) == 3
    assert all(document.cognify_complete for document in stored.values())


# ---------------------------------------------------------------------------
# engine end-to-end: cocoindex App -> cogindex target -> real Cognee
# ---------------------------------------------------------------------------


@coco.fn
async def _app_main(dataset: str, docs: dict[str, str]) -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target,
        _RUNTIME_KEY,
        dataset,
        processing=cogindex.ProcessingConfig(graph_model_id="integration.Model"),
    )
    for key, content in docs.items():
        target.declare_document(key, content, label=f"label-{key}")


async def test_engine_end_to_end_with_real_cognee(
    runtime: LocalCogneeRuntime, llm_mock: AsyncMock
) -> None:
    dataset = "int_engine"
    env = create_test_env(__file__, suffix="engine_e2e")
    env.context_provider.provide(_RUNTIME_KEY, runtime)

    async def run(docs: dict[str, str]) -> None:
        app = coco.App(
            coco.AppConfig(name="int_engine_e2e", environment=env),
            _app_main,
            dataset=dataset,
            docs=docs,
        )
        await app.update().result()

    # Create.
    await run({"bob.md": "Bob works for AlphaCorp.", "carol.md": "Carol works for SharedOrg."})
    report = await verify_dataset(
        runtime,
        _RUNTIME_KEY,
        dataset,
        [
            ExpectedDocument("bob.md", label="label-bob.md"),
            ExpectedDocument("carol.md", label="label-carol.md"),
        ],
        tenant=TENANT,
    )
    assert report.ok, report.render()

    # Replace through the engine: content change purges + re-cognifies.
    await run({"bob.md": "Bob works for BetaCorp.", "carol.md": "Carol works for SharedOrg."})
    handle = await runtime.resolve_dataset(dataset, TENANT)
    assert handle.dataset_id is not None
    names = await graph_node_names(handle.dataset_id)
    assert "betacorp" in names
    assert "alphacorp" not in names

    # Delete through the engine.
    await run({"bob.md": "Bob works for BetaCorp."})
    report = await verify_dataset(
        runtime,
        _RUNTIME_KEY,
        dataset,
        [ExpectedDocument("bob.md", label="label-bob.md")],
        tenant=TENANT,
    )
    assert report.ok, report.render()
    stored = await runtime.list_documents(handle)
    assert [document.data_id for document in stored] == [did(dataset, "bob.md")]
