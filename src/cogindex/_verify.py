"""Drift verification: does actual Cognee state match declared expectations?

``verify_dataset`` derives the same stable identities the target would and
compares them against what the runtime actually stores, surfacing missing
documents, foreign/stale documents, incomplete cognify runs, and label
drift. It is read-only and does not claim that an ordinary flow rerun repairs
external drift: when CocoIndex's tracking record is already converged, no
document action may be scheduled.

Deliberately NOT compared in 0.1: raw content and external_metadata.
Cognee stores content as its own hash/file formats and wraps metadata in
storage-specific envelopes; comparing them would either replicate upstream
internals or produce false alarms. The current checks cover relational-row
presence, identity, completion status and label; stale derivatives can remain
invisible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import cocoindex as coco

from ._identity import (
    _validate_coordinate,
    _validate_runtime_key,
    document_data_id,
    normalize_external_key,
)
from ._runtime import CogneeRuntime, StoredDocument

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
    issues: tuple[VerificationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))

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
    runtime_key: coco.ContextKey[CogneeRuntime],
    name: str,
    expected: Iterable[ExpectedDocument],
    *,
    tenant: str = "default",
) -> VerificationReport:
    """Compare declared expectations against the runtime's actual state.

    ``runtime_key`` must be the same non-secret ContextKey the dataset target
    was declared with, since its key is persisted and participates in every
    document identity.
    """
    if not isinstance(runtime_key, coco.ContextKey):
        raise TypeError("runtime_key must be a ContextKey; its non-secret key is persisted")
    key_string = runtime_key.key
    _validate_runtime_key(key_string)
    _validate_coordinate(tenant, "tenant")
    _validate_coordinate(name, "dataset_name")
    handle = await runtime.resolve_dataset(name, tenant)
    expected_by_id: dict[uuid.UUID, ExpectedDocument] = {}
    for item in expected:
        data_id = document_data_id(
            key_string,
            handle.identity_scope,
            tenant,
            name,
            normalize_external_key(item.external_key),
        )
        previous = expected_by_id.get(data_id)
        if previous is not None:
            raise ValueError(
                "expected documents contain duplicate logical identity: "
                f"{previous.external_key!r} and {item.external_key!r}"
            )
        expected_by_id[data_id] = item

    stored_by_id: dict[uuid.UUID, StoredDocument] = {}
    for stored_document in await runtime.list_documents(handle):
        if stored_document.data_id in stored_by_id:
            raise RuntimeError(
                f"runtime returned duplicate stored data_id {stored_document.data_id} "
                f"for dataset {name!r}"
            )
        stored_by_id[stored_document.data_id] = stored_document

    issues: list[VerificationIssue] = []
    for data_id in sorted(expected_by_id.keys() | stored_by_id.keys(), key=str):
        expectation = expected_by_id.get(data_id)
        actual = stored_by_id.get(data_id)
        if expectation is not None and actual is None:
            issues.append(
                VerificationIssue(
                    kind="missing",
                    data_id=data_id,
                    external_key=expectation.external_key,
                    detail="expected document not present in Cognee",
                )
            )
            continue
        if expectation is None and actual is not None:
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
        if expectation is None or actual is None:  # pragma: no cover - union keys forbid this
            raise RuntimeError("verification identity union produced an empty entry")
        if not actual.cognify_complete:
            issues.append(
                VerificationIssue(
                    kind="incomplete",
                    data_id=data_id,
                    external_key=expectation.external_key,
                    detail="cognify has not completed for this document",
                )
            )
        if actual.label != expectation.label:
            issues.append(
                VerificationIssue(
                    kind="label_mismatch",
                    data_id=data_id,
                    external_key=expectation.external_key,
                    detail=(
                        f"label drifted: expected {expectation.label!r}, stored {actual.label!r}"
                    ),
                )
            )

    return VerificationReport(
        dataset=name,
        tenant=tenant,
        checked=len(expected_by_id),
        issues=tuple(issues),
    )
