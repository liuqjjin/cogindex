"""Minimal agent-memory demo: a changed fact replaces the old graph memory.

The "agent" here is deliberately small. It has one tool that reads a routing
relationship from Cognee's graph and formats the tool result as an answer. It
does not use an agent framework or a second mocked LLM call, so the answer can
only change when the stored graph changes.

The demo runs locally with deterministic LLM and embedding substitutes:

1. sync ``ProjectAtlas routes alerts to BlueQueue``;
2. ask the agent which queue ProjectAtlas uses;
3. edit the same document to say ``GreenQueue`` and sync again;
4. ask the same question and verify that GreenQueue exists while BlueQueue no
   longer exists in the graph.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ENTITY_TYPES = {
    "ProjectAtlas": "Service",
    "BlueQueue": "Queue",
    "GreenQueue": "Queue",
}
RELATIONSHIP = "routes_alerts_to"
QUESTION = "Which queue does ProjectAtlas route alerts to?"


@contextlib.contextmanager
def deterministic_llm() -> Iterator[None]:
    """Extract only the entities and relationship used by this example."""
    from unittest.mock import AsyncMock, patch

    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import Edge, KnowledgeGraph, Node, SummarizedContent

    def fake_llm(text_input: str, system_prompt: str, response_model: type, **kwargs: Any) -> Any:
        if text_input == "test":
            return "test"
        if response_model is SummarizedContent:
            return SummarizedContent(summary=text_input, description=text_input)
        if response_model is KnowledgeGraph:
            nodes = [
                Node(id=name, name=name, type=entity_type, description=name)
                for name, entity_type in ENTITY_TYPES.items()
                if name in text_input
            ]
            queues = [node for node in nodes if node.type == "Queue"]
            projects = [node for node in nodes if node.type == "Service"]
            edges = [
                Edge(
                    source_node_id=project.id,
                    target_node_id=queue.id,
                    relationship_name=RELATIONSHIP,
                )
                for project in projects
                for queue in queues
            ]
            return KnowledgeGraph(nodes=nodes, edges=edges)
        raise AssertionError(f"demo cannot mock {response_model!r}")

    with patch.object(LLMGateway, "acreate_structured_output", new_callable=AsyncMock) as mock:
        mock.side_effect = fake_llm
        yield


async def graph_snapshot(
    dataset_id: uuid.UUID,
) -> tuple[list[tuple[Any, Any]], list[tuple[Any, ...]]]:
    """Read one dataset's graph using Cognee's dataset-scoped graph context."""
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    async with set_database_global_context_variables(dataset_id, user.id):
        engine = await get_graph_engine()
        return await engine.get_graph_data()


def entity_name(properties: Any) -> str:
    if not isinstance(properties, dict):
        return ""
    return str(properties.get("name", ""))


class MemoryAgent:
    """Answer one routing question using a graph lookup as the memory tool."""

    def __init__(self, dataset_id: uuid.UUID) -> None:
        self._dataset_id = dataset_id

    async def answer(self, question: str) -> str:
        if "ProjectAtlas" not in question:
            return "I can only look up ProjectAtlas routing in this demo."

        nodes, edges = await graph_snapshot(self._dataset_id)
        nodes_by_id = {str(node_id): entity_name(properties) for node_id, properties in nodes}
        for source_id, target_id, relationship, *_ in edges:
            source = nodes_by_id.get(str(source_id), "")
            target = nodes_by_id.get(str(target_id), "")
            if (
                source.lower() == "projectatlas"
                and str(relationship).lower() == RELATIONSHIP
                and target.lower() in {"bluequeue", "greenqueue"}
            ):
                canonical_target = next(
                    name for name in ENTITY_TYPES if name.lower() == target.lower()
                )
                return f"ProjectAtlas routes alerts to {canonical_target}."
        return "No current routing fact was found for ProjectAtlas."


async def entity_names(dataset_id: uuid.UUID) -> set[str]:
    nodes, _ = await graph_snapshot(dataset_id)
    canonical = {name.lower(): name for name in ENTITY_TYPES}
    return {
        canonical[name.lower()]
        for _, properties in nodes
        if (name := entity_name(properties)).lower() in canonical
    }


async def run_demo(storage: Path) -> None:
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

    document = {"content": "ProjectAtlas routes alerts to BlueQueue."}
    dataset = "agent_memory_demo"

    @coco.fn
    async def app_main() -> None:
        target = await coco.use_mount(cogindex.declare_dataset_target, cognee_key, dataset)
        # The key stays fixed across the edit: this is a replacement, not a new
        # source document alongside the old one.
        target.declare_document("routing.md", document["content"])

    app = coco.App(coco.AppConfig(name="agent_memory_demo", environment=env), app_main)

    with deterministic_llm():
        print("1. Initial sync: ProjectAtlas routes alerts to BlueQueue.")
        await app.update().result()
        handle = await runtime.resolve_dataset(dataset, "default")
        assert handle.dataset_id is not None
        agent = MemoryAgent(handle.dataset_id)

        initial_answer = await agent.answer(QUESTION)
        print(f"   Agent question: {QUESTION}")
        print(f"   Agent answer: {initial_answer}")
        assert initial_answer == "ProjectAtlas routes alerts to BlueQueue."

        print("2. Edit routing.md: BlueQueue -> GreenQueue; run incremental sync.")
        document["content"] = "ProjectAtlas routes alerts to GreenQueue."
        await app.update().result()

        updated_answer = await agent.answer(QUESTION)
        names = await entity_names(handle.dataset_id)
        print(f"   Agent question: {QUESTION}")
        print(f"   Agent answer: {updated_answer}")
        print(f"   Graph check: GreenQueue present={('GreenQueue' in names)!s}")
        print(f"   Graph check: BlueQueue absent={('BlueQueue' not in names)!s}")

        assert updated_answer == "ProjectAtlas routes alerts to GreenQueue."
        assert "GreenQueue" in names
        assert "BlueQueue" not in names

    print("3. Passed: the agent read the new fact; the old graph memory is gone.")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cogindex-agent-memory-") as directory:
        await run_demo(Path(directory))


if __name__ == "__main__":
    asyncio.run(main())
