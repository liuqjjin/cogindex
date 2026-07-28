"""Shared-entity provenance demo.

Two documents mention the same organization. Watch what happens to the
knowledge graph as documents are replaced and deleted:

1. bob.md ("Bob works for SharedOrg and AlphaCorp") and carol.md ("Carol
   works for SharedOrg") are synced, the graph contains all entities.
2. bob.md is edited to mention BetaCorp instead: AlphaCorp disappears
   (its only supporting document changed), BetaCorp appears, and SharedOrg
   survives because carol.md still references it.
3. carol.md is removed: SharedOrg loses its last reference and disappears.

Nothing here is cogindex deleting graph nodes by hand: every removal flows
through Cognee's provenance-aware deletion planner. cogindex's contribution
is stable identity + the replace protocol that keeps derivatives in sync.

Runs entirely locally with deterministic LLM/embedding substitutes by
default (add --real to use your configured LLM instead).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ENTITY_VOCAB = {
    "AlphaCorp": "Company",
    "BetaCorp": "Company",
    "SharedOrg": "Organization",
    "Bob": "Person",
    "Carol": "Person",
}


@contextlib.contextmanager
def deterministic_llm() -> Iterator[None]:
    from unittest.mock import AsyncMock, patch

    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    def fake_llm(text_input: str, system_prompt: str, response_model: type, **kwargs: Any) -> Any:
        if text_input == "test":
            return "test"
        if response_model is SummarizedContent:
            return SummarizedContent(summary="demo summary", description="demo summary")
        if response_model is KnowledgeGraph:
            nodes = [
                Node(id=name, name=name, type=etype, description=f"{name} is a {etype}")
                for name, etype in ENTITY_VOCAB.items()
                if name in text_input
            ]
            edges = [
                Edge(
                    source_node_id=person.id,
                    target_node_id=org.id,
                    relationship_name="works_for",
                )
                for person in nodes
                if person.type == "Person"
                for org in nodes
                if org.type != "Person"
            ]
            return KnowledgeGraph(nodes=nodes, edges=edges)
        raise AssertionError(f"demo cannot mock {response_model!r}")

    with patch.object(LLMGateway, "acreate_structured_output", new_callable=AsyncMock) as mock:
        mock.side_effect = fake_llm
        yield


async def graph_entity_names(dataset_id: uuid.UUID) -> list[str]:
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    async with set_database_global_context_variables(dataset_id, user.id):
        engine = await get_graph_engine()
        nodes, _ = await engine.get_graph_data()
    # Cognee normalizes entity names (lower-cases them); match case-insensitively.
    vocabulary = {name.lower(): name for name in ENTITY_VOCAB}
    names = {
        vocabulary[str(properties["name"]).lower()]
        for _, properties in nodes
        if isinstance(properties, dict) and str(properties.get("name", "")).lower() in vocabulary
    }
    return sorted(names)


async def run_demo(storage: Path, *, real: bool) -> None:
    if not real:
        os.environ["MOCK_EMBEDDING"] = "true"
        os.environ["TELEMETRY_DISABLED"] = "1"

    import cocoindex as coco

    import cogindex

    runtime = cogindex.LocalCogneeRuntime(
        data_root=storage / "data", system_root=storage / "system"
    )
    cognee_key = coco.ContextKey[cogindex.CogneeRuntime]("cognee")
    env = coco.Environment(coco.Settings.from_env(db_path=storage / "tracking"))
    env.context_provider.provide(cognee_key, runtime)

    docs_holder: dict[str, dict[str, str]] = {"docs": {}}
    dataset = "shared_entity_demo"

    @coco.fn
    async def app_main() -> None:
        target = await coco.use_mount(cogindex.declare_dataset_target, cognee_key, dataset)
        for key, content in docs_holder["docs"].items():
            target.declare_document(key, content)

    app = coco.App(coco.AppConfig(name="shared_entity_demo", environment=env), app_main)

    async def sync_and_show(step: str, docs: dict[str, str]) -> None:
        docs_holder["docs"] = docs
        await app.update().result()
        handle = await runtime.resolve_dataset(dataset, "default")
        assert handle.dataset_id is not None
        names = await graph_entity_names(handle.dataset_id)
        print(f"\n== {step}")
        for key in sorted(docs):
            print(f"   document: {key}: {docs[key]!r}")
        if not docs:
            print("   (no documents declared)")
        print(f"   graph entities: {names}")

    mock_context = contextlib.nullcontext() if real else deterministic_llm()
    with mock_context:
        await sync_and_show(
            "step 1: both documents synced",
            {
                "bob.md": "Bob works for SharedOrg and AlphaCorp.",
                "carol.md": "Carol works for SharedOrg.",
            },
        )
        await sync_and_show(
            "step 2: bob.md edited (AlphaCorp -> BetaCorp); SharedOrg must survive",
            {
                "bob.md": "Bob works for BetaCorp.",
                "carol.md": "Carol works for SharedOrg.",
            },
        )
        await sync_and_show(
            "step 3: carol.md removed; SharedOrg loses its last reference",
            {"bob.md": "Bob works for BetaCorp."},
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help="use the configured real LLM instead of the deterministic substitute",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="cogindex-shared-entity-") as directory:
        await run_demo(Path(directory), real=args.real)
    print("\ndone: temporary storage removed")


if __name__ == "__main__":
    asyncio.run(main())
