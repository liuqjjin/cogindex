"""Drift verification: does actual Cognee state match declared expectations?

``verify_dataset`` derives the same stable identities the target would and
compares them against what the runtime actually stores, surfacing missing
documents, foreign/stale documents, incomplete cognify runs, and label
drift. Read-only: it never repairs anything (re-running the flow is the
repair; ADR-0003's convergence property is what makes that safe).

Deliberately NOT compared in 0.1: raw content and external_metadata.
Cognee stores content as its own hash/file formats and wraps metadata in
storage-specific envelopes; comparing them would either replicate upstream
internals or produce false alarms. Presence, identity, completion status
and label already catch the drift classes the connector can cause.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import cocoindex as coco

from ._identity import document_data_id, normalize_external_key
from ._runtime import CogneeRuntime

__all__ = [
    "ExpectedDocument",
    "VerificationIssue",
    "VerificationReport",
    "verify_dataset",
]

IssueKind = Literal["missing", "unexpected", "incomplete", "label_mismatch"]


@dataclass(frozen=True)
class ExpectedDocument:
    """What the caller believes is materialized for one external key."""

    external_key: str
    label: str | None = None


@dataclass(frozen=True)
class VerificationIssue:
    kind: IssueKind
    data_id: uuid.UUID
    external_key: str | None  # None for documents cogindex never declared
    detail: str


@dataclass(frozen=True)
class VerificationReport:
    dataset: str
    tenant: str
    checked: int
    issues: list[VerificationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def render(self) -> str:
        lines = [
            f"verify dataset={self.dataset!r} tenant={self.tenant!r}: "
            f"{self.checked} expected documents, {len(self.issues)} issues"
        ]
        for issue in self.issues:
            key_part = (
                f"key={issue.external_key!r}" if issue.external_key is not None else "(undeclared)"
            )
            lines.append(f"  [{issue.kind}] {key_part} data_id={issue.data_id}: {issue.detail}")
        return "\n".join(lines)


async def verify_dataset(
    runtime: CogneeRuntime,
    runtime_key: coco.ContextKey[CogneeRuntime] | str,
    name: str,
    expected: Iterable[ExpectedDocument],
    *,
    tenant: str = "default",
) -> VerificationReport:
    """Compare declared expectations against the runtime's actual state.

    ``runtime_key`` must be the same ContextKey (or key string) the dataset
    target was declared with, since it participates in every document identity.
    """
    key_string = runtime_key.key if isinstance(runtime_key, coco.ContextKey) else runtime_key
    expected_by_id: dict[uuid.UUID, ExpectedDocument] = {
        document_data_id(key_string, tenant, name, normalize_external_key(item.external_key)): item
        for item in expected
    }

    handle = await runtime.resolve_dataset(name, tenant)
    stored_by_id = {stored.data_id: stored for stored in await runtime.list_documents(handle)}

    issues: list[VerificationIssue] = []
    for data_id in sorted(expected_by_id.keys() | stored_by_id.keys(), key=str):
        expectation = expected_by_id.get(data_id)
        stored = stored_by_id.get(data_id)
        if expectation is not None and stored is None:
            issues.append(
                VerificationIssue(
                    kind="missing",
                    data_id=data_id,
                    external_key=expectation.external_key,
                    detail="expected document not present in Cognee",
                )
            )
            continue
        if expectation is None and stored is not None:
            issues.append(
                VerificationIssue(
                    kind="unexpected",
                    data_id=data_id,
                    external_key=None,
                    detail=(
                        "document not among expectations: foreign data or "
                        "stale state from an interrupted run"
                    ),
                )
            )
            continue
        if expectation is None or stored is None:  # pragma: no cover - exhaustive
            continue
        if not stored.cognify_complete:
            issues.append(
                VerificationIssue(
                    kind="incomplete",
                    data_id=data_id,
                    external_key=expectation.external_key,
                    detail="cognify has not completed for this document",
                )
            )
        if stored.label != expectation.label:
            issues.append(
                VerificationIssue(
                    kind="label_mismatch",
                    data_id=data_id,
                    external_key=expectation.external_key,
                    detail=(
                        f"label drifted: expected {expectation.label!r}, stored {stored.label!r}"
                    ),
                )
            )

    return VerificationReport(
        dataset=name,
        tenant=tenant,
        checked=len(expected_by_id),
        issues=issues,
    )
