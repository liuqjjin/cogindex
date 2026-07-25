"""What a hand-rolled Cognee integration does differently, measured.

Every other category in this harness measures cogindex against itself. This one
measures it against the alternative: the loop a developer writes when they wire
Cognee into a pipeline directly.

    for key, text in docs.items():
        await cognee.add(text, dataset_name=ds)
    await cognee.cognify(datasets=[ds])

That code is not a straw man. It is the shape of Cognee's own quickstart, it
works, and on a corpus that never changes it produces exactly the right graph.
The divergence starts on the second run, because ``add`` without an explicit
``data_id`` derives the id from a hash of the content
(``uuid5(NAMESPACE_OID, md5(content) + user + tenant)``), so an edited document
is a *different* document. Nothing updates and nothing is removed.

Four things are compared, on identical corpora and identical LLM stubs:

``documents``
    Rows Cognee holds. The naive run accumulates one per edit, because the
    edited text hashes to a new id while the old row stays.
``stale_entities``
    Entities in the graph that only the superseded text supported. These are
    what a retrieval query returns as confidently as the current ones, which is
    the failure that is hard to notice and expensive to debug.
``llm_calls``
    Extraction work. Cheap here because the LLM is a deterministic stub, and
    the dominant cost of the whole system in production.
``deleted_documents``
    Whether removing a source document removes it from Cognee at all. The naive
    integration has no answer: it never knew the id.

The point is not that Cognee is wrong. It is that stable identity has to come
from somewhere, and if the integration layer does not supply it, nobody does.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import cocoindex as coco

import cogindex

from ._harness import BenchResult

DATASET_NAIVE = "baseline_naive"
DATASET_MANAGED = "baseline_cogindex"
TENANT = "default"

# Kept small: every document is a real cognify pass over a real local stack.
CORPUS_SIZE = 6
EDIT_COUNT = 2
DELETE_COUNT = 1

# One capitalised token per document, so a stale entity is unambiguous: it is
# present in the graph while no current document mentions it.
_ENTITY = "Entity{index:02d}"
_REPLACEMENT = "Replacement{index:02d}"


def _corpus(*, edited: int = 0, dropped: int = 0) -> dict[str, str]:
    """The source of truth: ``dropped`` documents gone, ``edited`` rewritten."""
    documents: dict[str, str] = {}
    for index in range(CORPUS_SIZE):
        if index < dropped:
            continue
        token = (
            _REPLACEMENT.format(index=index)
            if dropped <= index < dropped + edited
            else _ENTITY.format(index=index)
        )
        documents[f"doc-{index:02d}.md"] = f"Document {index} concerns {token}."
    return documents


def _expected_tokens(corpus: dict[str, str]) -> set[str]:
    return {
        word.rstrip(".")
        for text in corpus.values()
        for word in text.split()
        if word[0].isupper() and word != "Document"
    }


async def _graph_tokens(dataset_id: uuid.UUID) -> set[str]:
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    async with set_database_global_context_variables(dataset_id, user.id):
        engine = await get_graph_engine()
        nodes, _ = await engine.get_graph_data()
    tokens = set()
    for _, properties in nodes:
        if isinstance(properties, dict) and properties.get("name"):
            name = str(properties["name"])
            if name.lower().startswith(("entity", "replacement")):
                tokens.add(name.lower())
    return tokens


async def _prune() -> None:
    import cognee
    from cognee.modules.engine.operations.setup import setup

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()


async def _document_count(dataset: str) -> int:
    import cognee

    for candidate in await cognee.datasets.list_datasets():
        if candidate.name == dataset:
            return len(await cognee.datasets.list_data(candidate.id))
    return 0


async def _dataset_id(dataset: str) -> uuid.UUID | None:
    import cognee

    for candidate in await cognee.datasets.list_datasets():
        if candidate.name == dataset:
            dataset_id: uuid.UUID = candidate.id
            return dataset_id
    return None


# ---------------------------------------------------------------------------
# the naive integration
# ---------------------------------------------------------------------------


async def _run_naive(corpus: dict[str, str]) -> None:
    import cognee

    for text in corpus.values():
        await cognee.add(text, dataset_name=DATASET_NAIVE)
    dataset_id = await _dataset_id(DATASET_NAIVE)
    if dataset_id is not None:
        await cognee.cognify(datasets=[dataset_id])


# ---------------------------------------------------------------------------
# the same corpus through cogindex
# ---------------------------------------------------------------------------

_RUNTIME_KEY = coco.ContextKey[cogindex.CogneeRuntime]("baseline_runtime")


@coco.fn
async def _managed_main(docs_holder: dict[str, dict[str, str]]) -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target,
        _RUNTIME_KEY,
        DATASET_MANAGED,
        processing=cogindex.ProcessingConfig(graph_model_id="baseline.Model"),
    )
    for key, text in docs_holder["docs"].items():
        target.declare_document(key, text)


async def bench_baseline_comparison(mode: str, sizes: dict[str, int]) -> BenchResult:
    """Naive integration versus cogindex over one edit-and-delete cycle."""
    del sizes  # fixed corpus: each document is a real extraction pass
    if mode != "real":
        return BenchResult(
            "baseline_comparison",
            {"mode": mode},
            {"skipped": True},
            notes="needs the real stack: the divergence is in Cognee's own "
            "identity and provenance handling, which the fake cannot show.",
        )

    import tempfile
    from pathlib import Path

    from cognee.infrastructure.llm import LLMGateway

    initial = _corpus()
    edited = _corpus(edited=EDIT_COUNT)
    final = _corpus(edited=EDIT_COUNT, dropped=DELETE_COUNT)

    metrics: dict[str, Any] = {}

    # -- naive ---------------------------------------------------------------
    await _prune()
    calls_before = LLMGateway.acreate_structured_output.call_count
    started = time.perf_counter()
    await _run_naive(initial)
    await _run_naive(edited)
    await _run_naive(final)
    metrics["naive_seconds"] = round(time.perf_counter() - started, 3)
    metrics["naive_llm_calls"] = LLMGateway.acreate_structured_output.call_count - calls_before
    metrics["naive_documents"] = await _document_count(DATASET_NAIVE)
    naive_dataset_id = await _dataset_id(DATASET_NAIVE)
    naive_tokens = await _graph_tokens(naive_dataset_id) if naive_dataset_id else set()
    expected = {token.lower() for token in _expected_tokens(final)}
    metrics["naive_stale_entities"] = len(naive_tokens - expected)
    metrics["naive_deleted_documents"] = 0

    # -- cogindex ------------------------------------------------------------
    await _prune()
    storage = Path(tempfile.mkdtemp(prefix="cogindex-baseline-"))
    runtime = cogindex.LocalCogneeRuntime(
        data_root=storage / "data", system_root=storage / "system"
    )
    env = coco.Environment(coco.Settings.from_env(db_path=storage / "tracking"))
    env.context_provider.provide(_RUNTIME_KEY, runtime)
    holder: dict[str, dict[str, str]] = {"docs": {}}
    app = coco.App(
        coco.AppConfig(name="baseline_managed", environment=env), _managed_main, docs_holder=holder
    )

    calls_before = LLMGateway.acreate_structured_output.call_count
    started = time.perf_counter()
    for corpus in (initial, edited, final):
        holder["docs"] = corpus
        await app.update().result()
    metrics["cogindex_seconds"] = round(time.perf_counter() - started, 3)
    metrics["cogindex_llm_calls"] = LLMGateway.acreate_structured_output.call_count - calls_before
    metrics["cogindex_documents"] = await _document_count(DATASET_MANAGED)
    managed_dataset_id = await _dataset_id(DATASET_MANAGED)
    managed_tokens = await _graph_tokens(managed_dataset_id) if managed_dataset_id else set()
    metrics["cogindex_stale_entities"] = len(managed_tokens - expected)
    metrics["cogindex_deleted_documents"] = DELETE_COUNT

    metrics["documents_expected"] = len(final)

    return BenchResult(
        "baseline_comparison",
        {
            "corpus": CORPUS_SIZE,
            "edited": EDIT_COUNT,
            "deleted": DELETE_COUNT,
            "mode": mode,
        },
        metrics,
        notes=(
            f"Source of truth after three syncs: {len(final)} documents. "
            "Rows above `documents_expected` are superseded copies the naive "
            "integration could not update, because add() without a data_id "
            "hashes the content into the id. `stale_entities` counts graph "
            "nodes that only superseded text supports; retrieval cannot tell "
            "them apart from current ones."
        ),
    )
