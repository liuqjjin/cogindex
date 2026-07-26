"""Benchmark environment metadata, metrics, and report writing.

Reports are written to ``benchmarks/reports/``. Wall-clock results are only
comparable when the code, dependencies, machine, and scenario match.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


@dataclasses.dataclass
class BenchResult:
    category: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    notes: str = ""


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def _git_commit() -> str:
    try:
        git = shutil.which("git")
        if git is None:
            return "unknown"
        out = subprocess.run(  # noqa: S603 - fixed argv, resolved binary
            [git, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _git_dirty() -> bool | None:
    try:
        git = shutil.which("git")
        if git is None:
            return None
        out = subprocess.run(  # noqa: S603 - fixed argv, resolved binary
            [git, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        return bool(out.stdout.strip())
    except Exception:
        return None


def environment_fingerprint(mode: str, profile: str) -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "profile": profile,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "cogindex": _package_version("cogindex"),
        "cocoindex": _package_version("cocoindex"),
        "cognee": _package_version("cognee"),
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
    }


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    k = (len(ordered) - 1) * p
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return ordered[int(k)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


def write_report(
    env: dict[str, Any], results: list[BenchResult], *, stem: str
) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{stem}.json"
    md_path = REPORTS_DIR / f"{stem}.md"

    payload = {
        "environment": env,
        "results": [dataclasses.asdict(result) for result in results],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")

    lines = [
        f"# cogindex benchmark report: {env['timestamp_utc']}",
        "",
        "> Machine-specific numbers. Compare only reports produced from the same",
        "> code, dependency set, machine, and scenario. Mode "
        f"`{env['mode']}`: "
        + (
            "connector layer over an in-memory fake Cognee: measures cogindex "
            "+ engine overhead, not Cognee."
            if env["mode"] == "fake"
            else "real local Cognee stack with deterministic LLM/embedding "
            "substitutes: measures the full local pipeline without model "
            "latency."
        ),
        "",
        "## Environment",
        "",
        "| key | value |",
        "|---|---|",
    ]
    lines.extend(f"| {key} | {value} |" for key, value in env.items())
    for result in results:
        lines.extend(
            [
                "",
                f"## {result.category}",
                "",
                f"params: `{json.dumps(result.params, default=str)}`",
                "",
                "| metric | value |",
                "|---|---|",
            ]
        )
        lines.extend(f"| {name} | {value} |" for name, value in result.metrics.items())
        if result.notes:
            lines.extend(["", result.notes])
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path
