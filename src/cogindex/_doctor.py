"""Environment health checks for a cogindex deployment.

``doctor()`` runs read-only checks over the installed packages and cognee's
effective configuration and reports findings with actionable fix hints. It
never mutates anything.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Literal

from . import _compat
from ._errors import CompatibilityError

__all__ = ["DoctorFinding", "DoctorReport", "doctor"]

Severity = Literal["ok", "warning", "critical"]


@dataclass(frozen=True)
class DoctorFinding:
    severity: Severity
    check: str
    detail: str
    fix_hint: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    findings: tuple[DoctorFinding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))

    @property
    def ok(self) -> bool:
        return all(finding.severity != "critical" for finding in self.findings)

    def render(self) -> str:
        lines = ["cogindex doctor:"]
        for finding in self.findings:
            lines.append(f"  [{finding.severity:8s}] {finding.check}: {finding.detail}")
            if finding.fix_hint is not None:
                lines.append(f"             fix: {finding.fix_hint}")
        return "\n".join(lines)


def doctor() -> DoctorReport:
    """Run all environment checks and return the report."""
    findings: list[DoctorFinding] = []
    findings.append(_check_cocoindex())
    findings.append(_check_cognee_compat())
    findings.extend(_check_storage_roots())
    findings.extend(_check_credentials())
    return DoctorReport(findings=tuple(findings))


def _check_cocoindex() -> DoctorFinding:
    try:
        version = importlib.metadata.version("cocoindex")
    except importlib.metadata.PackageNotFoundError:
        return DoctorFinding(
            severity="critical",
            check="cocoindex",
            detail="cocoindex is not installed",
            fix_hint="reinstall cogindex; cocoindex is a required dependency",
        )
    return DoctorFinding(severity="ok", check="cocoindex", detail=f"version {version}")


def _check_cognee_compat() -> DoctorFinding:
    try:
        compat_info = _compat.load()
    except CompatibilityError as exc:
        return DoctorFinding(
            severity="critical",
            check="cognee-compat",
            detail=str(exc),
            fix_hint="install a cognee version in the supported range (>=1.4,<1.5)",
        )
    return DoctorFinding(
        severity="ok",
        check="cognee-compat",
        detail=(
            f"version {compat_info.version}; DataItem(data_id), forget() and "
            "datasets APIs all present"
        ),
    )


def _check_storage_roots() -> list[DoctorFinding]:
    data_root, system_root = _compat.storage_roots()
    if data_root is None or system_root is None:
        return [
            DoctorFinding(
                severity="warning",
                check="storage-roots",
                detail="could not read cognee's storage configuration",
                fix_hint="cognee's config layout may have moved; check versions",
            )
        ]
    findings: list[DoctorFinding] = []
    for label, path in (("data root", data_root), ("system root", system_root)):
        if "site-packages" in path:
            findings.append(
                DoctorFinding(
                    severity="critical",
                    check="storage-roots",
                    detail=f"{label} points inside the installed package: {path}",
                    fix_hint=(
                        "pass data_root=/system_root= to LocalCogneeRuntime or "
                        "set DATA_ROOT_DIRECTORY / SYSTEM_ROOT_DIRECTORY"
                    ),
                )
            )
        else:
            findings.append(
                DoctorFinding(severity="ok", check="storage-roots", detail=f"{label}: {path}")
            )
    return findings


def _check_credentials() -> list[DoctorFinding]:
    llm_ok, embedding_ok = _compat.credentials_present()
    findings: list[DoctorFinding] = []
    for label, state in (("llm", llm_ok), ("embedding", embedding_ok)):
        if state is True:
            findings.append(
                DoctorFinding(severity="ok", check=f"{label}-credentials", detail="configured")
            )
        elif state is False:
            findings.append(
                DoctorFinding(
                    severity="critical",
                    check=f"{label}-credentials",
                    detail=f"no {label} credentials configured; cognify will fail",
                    fix_hint="set LLM_API_KEY (and embedding config) in the environment",
                )
            )
        else:
            findings.append(
                DoctorFinding(
                    severity="warning",
                    check=f"{label}-credentials",
                    detail="could not determine credential state",
                )
            )
    return findings
