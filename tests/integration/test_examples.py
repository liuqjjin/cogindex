"""The examples, executed the way their README tells users to run them.

`examples/` is otherwise ungated: ruff lints it, mypy skips it, and nothing
imports it. Three real defects lived in `quickstart_live.py` behind that gap,
including one where documents were declared under keys that the verification
step then looked for under different keys, so a clean run reported every
document as simultaneously missing and unexpected.

Two details of the invocation below are load-bearing rather than incidental:

- the folder is passed as a **relative** path, because absolute paths took a
  different branch that happened to work;
- the corpus contains a **subfolder**, because the directory walk defaulted to
  non-recursive while the file patterns and the expectation list were both
  recursive.

Each example runs in a subprocess. They configure cognee's storage roots and
patch its LLM gateway at import time, which would leak into the rest of this
tier if run in-process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
TIMEOUT_SECONDS = 600

CORPUS = {
    "bob.md": "Bob works for AlphaCorp.\n",
    "carol.md": "Carol works for SharedOrg.\n",
    "nested/dave.md": "Dave works for BetaCorp.\n",
    "notes.txt": "A plain text note mentioning AlphaCorp.\n",
}


def write_corpus(folder: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def run_quickstart(workdir: Path, folder_arg: str, *extra_args: str) -> str:
    """Run the quickstart from ``workdir`` so relative paths stay relative."""
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / "quickstart_live.py"),
            folder_arg,
            "--deterministic",
            "--storage",
            "./storage",
            *extra_args,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "MOCK_EMBEDDING": "false"},
    )
    assert result.returncode == 0, f"quickstart failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def issue_count(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("verify dataset="):
            # "... N expected documents, M issues"
            return int(line.rsplit(",", 1)[1].strip().split()[0])
    raise AssertionError(f"no verify line in quickstart output:\n{output}")


def test_quickstart_converges_and_tracks_edits_and_deletes(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    write_corpus(docs, CORPUS)

    # 1. First run, folder spelled relatively.
    assert issue_count(run_quickstart(tmp_path, "./docs")) == 0

    # 2. The same folder spelled absolutely must resolve to the same document
    #    identities. If it did not, every document would come back as both
    #    missing (under the other spelling) and unexpected.
    assert issue_count(run_quickstart(tmp_path, str(docs))) == 0

    # 3. Re-running an unchanged corpus stays converged.
    assert issue_count(run_quickstart(tmp_path, "./docs")) == 0

    # 4. An edit is a replacement in place, not a second document.
    (docs / "bob.md").write_text("Bob works for BetaCorp now.\n")
    assert issue_count(run_quickstart(tmp_path, "./docs")) == 0

    # 5. A removed file is deleted, and its absence is not reported as drift.
    (docs / "carol.md").unlink()
    output = run_quickstart(tmp_path, "./docs")
    assert issue_count(output) == 0
    assert "3 expected documents" in output

    invalid_env = {
        **os.environ,
        "EMBEDDING_MODEL": "cogindex/unregistered-example-model",
        "MOCK_EMBEDDING": "false",
    }
    invalid_env.pop("EMBEDDING_DIMENSIONS", None)
    invalid = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / "quickstart_live.py"),
            "./docs",
            "--deterministic",
            "--storage",
            "./invalid-storage",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=invalid_env,
    )
    assert invalid.returncode == 2
    assert "environment is not ready" in invalid.stdout

    empty = tmp_path / "empty"
    empty.mkdir()
    empty_output = run_quickstart(
        tmp_path,
        "./empty",
        "--dataset",
        "empty_dataset",
        "--search",
        "anything",
    )
    assert issue_count(empty_output) == 0
    assert "search skipped: dataset 'empty_dataset' has no materialized documents" in empty_output


def test_shared_entity_demo_matches_its_documented_output(tmp_path: Path) -> None:
    # examples/README.md quotes this output verbatim, so it is a claim that can
    # rot. The graph assertions are the interesting part: an entity supported
    # by two documents has to survive the replacement of one of them.
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "shared_entity_demo.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "MOCK_EMBEDDING": "false", "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, f"demo failed:\n{result.stdout}\n{result.stderr}"
    assert not list(tmp_path.glob("cogindex-shared-entity-*"))
    entity_lines = [
        line.split("graph entities:")[1].strip()
        for line in result.stdout.splitlines()
        if "graph entities:" in line
    ]
    assert entity_lines == [
        "['AlphaCorp', 'Bob', 'Carol', 'SharedOrg']",
        "['BetaCorp', 'Bob', 'Carol', 'SharedOrg']",
        "['BetaCorp', 'Bob']",
    ]


def test_agent_memory_demo_reads_the_replaced_fact(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "agent_memory_demo.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "MOCK_EMBEDDING": "false", "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, f"demo failed:\n{result.stdout}\n{result.stderr}"
    assert not list(tmp_path.glob("cogindex-agent-memory-*"))

    answer_lines = [line.strip() for line in result.stdout.splitlines() if "Agent answer:" in line]
    assert answer_lines == [
        "Agent answer: ProjectAtlas routes alerts to BlueQueue.",
        "Agent answer: ProjectAtlas routes alerts to GreenQueue.",
    ]
    assert "Graph check: GreenQueue present=True" in result.stdout
    assert "Graph check: BlueQueue absent=True" in result.stdout
    assert "Passed: the agent read the new fact; the old graph memory is gone." in result.stdout
