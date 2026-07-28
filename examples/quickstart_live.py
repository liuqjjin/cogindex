"""Quickstart: materialize a folder of text files into a Cognee knowledge graph.

CocoIndex watches the folder and owns change detection; cogindex maps every
file onto a stable Cognee document; Cognee builds the knowledge graph.
Add, edit, or delete files and re-run (or use --live): the graph follows,
edits replace in place, deletions clean up derivatives.

Usage:
    # with a configured LLM (see .env.example):
    uv run python examples/quickstart_live.py ./my-docs --search "what is X?"

    # without any LLM key (deterministic demo substitutes):
    uv run python examples/quickstart_live.py ./my-docs --deterministic

    # watch continuously (Ctrl+C to stop):
    uv run python examples/quickstart_live.py ./my-docs --live
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import os
import pathlib
from collections.abc import Iterator
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder", type=pathlib.Path, help="folder of .md/.txt files, searched recursively"
    )
    parser.add_argument("--dataset", default="quickstart", help="Cognee dataset name")
    parser.add_argument("--live", action="store_true", help="watch and sync continuously")
    parser.add_argument("--search", default=None, help="run a search after syncing")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="no-LLM demo mode: deterministic LLM/embedding substitutes "
        "(same mechanism as the test suite; NOT representative output)",
    )
    parser.add_argument(
        "--storage",
        type=pathlib.Path,
        default=pathlib.Path(".cogindex-quickstart-storage"),
        help="where Cognee stores its data/databases",
    )
    return parser.parse_args()


def document_key(path: pathlib.Path, folder_root: pathlib.Path) -> str:
    """Stable identity for one file: its path relative to the watched folder.

    Deriving it in one place is the point. Identity must not depend on how the
    folder was spelled on the command line, a relative `./my-docs` and an
    absolute `/home/me/my-docs` are the same folder and must produce the same
    document keys, and the declaration side and the verification side must
    never derive it differently, or every document reads as both missing and
    unexpected. ``resolve()`` both sides: /tmp vs /private/tmp style symlinks
    would otherwise make ``relative_to()`` fail.

    ``resolve()`` stats the filesystem, so callers inside async code block
    briefly: one stat per file, which is fine for an example.
    """
    return path.resolve().relative_to(folder_root).as_posix()


@contextlib.contextmanager
def deterministic_llm() -> Iterator[None]:
    """Deterministic LLM substitute: extracts Capitalized words as entities."""
    from unittest.mock import AsyncMock, patch

    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import KnowledgeGraph, Node, SummarizedContent

    def fake_llm(text_input: str, system_prompt: str, response_model: type, **kwargs: Any) -> Any:
        if text_input == "test":
            return "test"
        if response_model is str:
            # search answer generation
            return (
                "deterministic demo answer (LLM output is mocked; use a real key for real answers)"
            )
        if response_model is SummarizedContent:
            summary = text_input.strip().split("\n")[0][:80] or "empty document"
            return SummarizedContent(summary=summary, description=summary)
        if response_model is KnowledgeGraph:
            words = {
                word.strip(".,;:!?()[]\"'")
                for word in text_input.split()
                if word[:1].isupper() and len(word) > 3
            }
            nodes = [
                Node(id=word, name=word, type="Concept", description=word)
                for word in sorted(words)[:12]
            ]
            return KnowledgeGraph(nodes=nodes, edges=[])
        raise AssertionError(f"deterministic demo cannot mock {response_model!r}")

    with patch.object(LLMGateway, "acreate_structured_output", new_callable=AsyncMock) as mock:
        mock.side_effect = fake_llm
        yield


async def main() -> None:
    args = parse_args()
    if args.deterministic:
        os.environ["MOCK_EMBEDDING"] = "true"
        os.environ["TELEMETRY_DISABLED"] = "1"
        os.environ.setdefault("LOG_LEVEL", "ERROR")

    import cocoindex as coco
    from cocoindex.connectors import localfs
    from cocoindex.resources.file import PatternFilePathMatcher

    import cogindex

    cognee_key = coco.ContextKey[cogindex.CogneeRuntime]("cognee")
    runtime = cogindex.LocalCogneeRuntime(
        data_root=args.storage / "data", system_root=args.storage / "system"
    )

    health = cogindex.doctor(check_credentials=not args.deterministic)
    print(health.render())
    if not health.ok:
        print("\nenvironment is not ready; fix the findings above")
        if not args.deterministic:
            print("if only model credentials are missing, use --deterministic")
        raise SystemExit(2)

    env = coco.Environment(coco.Settings.from_env(db_path=args.storage / "cocoindex-tracking"))
    env.context_provider.provide(cognee_key, runtime)

    folder_root = args.folder.resolve()
    profile = cogindex.CognifyProfile()
    processing = cogindex.processing_config_from_profile(profile)
    if args.deterministic:
        # The substitute produces different derivatives from a configured
        # model even when both expose the same model identifiers. Keep its
        # persistent tracking state distinct so switching modes reprocesses.
        processing = dataclasses.replace(
            processing,
            extras=(*processing.extras, ("example_adapter", "deterministic-v1")),
        )

    @coco.fn
    async def process_file(file: localfs.File, target: cogindex.DatasetTarget) -> None:
        content = await file.read_text()
        path = pathlib.Path(file.file_path.path)
        key = document_key(path, folder_root)
        target.declare_document(key, content, label=path.name)

    @coco.fn
    async def app_main() -> None:
        target = await coco.use_mount(
            cogindex.declare_dataset_target,
            cognee_key,
            args.dataset,
            profile=profile,
            processing=processing,
        )
        files = localfs.walk_dir(
            args.folder,
            live=args.live,
            # recursive defaults to False upstream, which would silently
            # ignore subfolders while the `**/` patterns below (and the
            # expectations built for verify_dataset) both mean "any depth".
            recursive=True,
            path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md", "**/*.txt"]),
        )
        await coco.mount_each(process_file, files.items(), target)

    app = coco.App(coco.AppConfig(name="cogindex_quickstart", environment=env), app_main)

    mock_context = deterministic_llm() if args.deterministic else contextlib.nullcontext()
    with mock_context:
        if args.live:
            print(f"\nwatching {args.folder}. Edit files, Ctrl+C to stop")
            await app.update(live=True).result()
            return
        await app.update().result()

        expected = [
            cogindex.ExpectedDocument(document_key(path, folder_root), label=path.name)
            for pattern in ("**/*.md", "**/*.txt")
            for path in sorted(args.folder.glob(pattern))
        ]
        report = await cogindex.verify_dataset(runtime, cognee_key, args.dataset, expected)
        print()
        print(report.render())

        if args.search:
            import cognee

            handle = await runtime.resolve_dataset(args.dataset, "default")
            if handle.dataset_id is None:
                print(f"\nsearch skipped: dataset {args.dataset!r} has no materialized documents")
                return
            results = await cognee.search(args.search, datasets=[handle.dataset_id])
            print(f"\nsearch: {args.search!r}")
            for result in results if isinstance(results, list) else [results]:
                print(f"  - {result}")


if __name__ == "__main__":
    asyncio.run(main())
