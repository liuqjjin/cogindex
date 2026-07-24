"""Drift verification (verify_dataset) and environment checks (doctor)."""

from __future__ import annotations

import unicodedata
import uuid

import cogindex
from cogindex import (
    CognifyProfile,
    DatasetHandle,
    DocumentPayload,
    ExpectedDocument,
    doctor,
    verify_dataset,
)
from cogindex.testing import FakeCogneeRuntime

RUNTIME_KEY = "rt-verify"
TENANT = "default"
DATASET = "ds-verify"


def did(key: str) -> uuid.UUID:
    return cogindex.document_data_id(RUNTIME_KEY, TENANT, DATASET, key)


def payload(key: str, content: str, *, label: str | None = None) -> DocumentPayload:
    return DocumentPayload(data_id=did(key), content=content, label=label)


async def seed(
    runtime: FakeCogneeRuntime, payloads: list[DocumentPayload], *, cognify: bool = True
) -> DatasetHandle:
    handle = await runtime.add_documents(DatasetHandle(name=DATASET, tenant=TENANT), payloads)
    if cognify:
        await runtime.cognify_dataset(handle, CognifyProfile())
    return handle


# =============================================================================
# verify_dataset
# =============================================================================


async def test_verify_ok_when_state_matches() -> None:
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload("a.md", "alpha", label="L"), payload("b.md", "beta")])

    report = await verify_dataset(
        runtime,
        RUNTIME_KEY,
        DATASET,
        [ExpectedDocument("a.md", label="L"), ExpectedDocument("b.md")],
        tenant=TENANT,
    )
    assert report.ok
    assert report.checked == 2
    assert report.issues == []


async def test_verify_reports_missing_document() -> None:
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload("a.md", "alpha")])

    report = await verify_dataset(
        runtime,
        RUNTIME_KEY,
        DATASET,
        [ExpectedDocument("a.md"), ExpectedDocument("gone.md")],
        tenant=TENANT,
    )
    assert not report.ok
    (issue,) = report.issues
    assert issue.kind == "missing"
    assert issue.external_key == "gone.md"
    assert issue.data_id == did("gone.md")


async def test_verify_reports_unexpected_document() -> None:
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload("a.md", "alpha"), payload("stale.md", "old")])

    report = await verify_dataset(
        runtime, RUNTIME_KEY, DATASET, [ExpectedDocument("a.md")], tenant=TENANT
    )
    (issue,) = report.issues
    assert issue.kind == "unexpected"
    assert issue.external_key is None
    assert issue.data_id == did("stale.md")


async def test_verify_reports_incomplete_cognify() -> None:
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload("a.md", "alpha")], cognify=False)

    report = await verify_dataset(
        runtime, RUNTIME_KEY, DATASET, [ExpectedDocument("a.md")], tenant=TENANT
    )
    (issue,) = report.issues
    assert issue.kind == "incomplete"
    assert issue.external_key == "a.md"


async def test_verify_reports_label_drift() -> None:
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload("a.md", "alpha", label="stored")])

    report = await verify_dataset(
        runtime,
        RUNTIME_KEY,
        DATASET,
        [ExpectedDocument("a.md", label="declared")],
        tenant=TENANT,
    )
    (issue,) = report.issues
    assert issue.kind == "label_mismatch"
    assert "declared" in issue.detail
    assert "stored" in issue.detail


async def test_verify_missing_dataset_reports_all_expected_missing() -> None:
    runtime = FakeCogneeRuntime()
    report = await verify_dataset(
        runtime,
        RUNTIME_KEY,
        DATASET,
        [ExpectedDocument("a.md"), ExpectedDocument("b.md")],
        tenant=TENANT,
    )
    assert len(report.issues) == 2
    assert {issue.kind for issue in report.issues} == {"missing"}


async def test_verify_key_normalization_matches_target_identity() -> None:
    # NFC vs NFD spellings of the same key verify against one identity.
    nfc = "café.md"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload(nfc, "alpha")])

    report = await verify_dataset(
        runtime, RUNTIME_KEY, DATASET, [ExpectedDocument(nfd)], tenant=TENANT
    )
    assert report.ok


async def test_verify_render_lists_every_issue() -> None:
    runtime = FakeCogneeRuntime()
    await seed(runtime, [payload("stale.md", "old")], cognify=False)

    report = await verify_dataset(
        runtime, RUNTIME_KEY, DATASET, [ExpectedDocument("a.md")], tenant=TENANT
    )
    rendered = report.render()
    assert "missing" in rendered
    assert "unexpected" in rendered
    assert DATASET in rendered


# =============================================================================
# doctor
# =============================================================================


def test_doctor_runs_and_reports_installed_versions() -> None:
    report = doctor()
    assert report.findings
    checks = {finding.check for finding in report.findings}
    assert "cocoindex" in checks
    assert "cognee-compat" in checks
    assert "storage-roots" in checks
    # This dev environment has compatible versions installed.
    by_check = {finding.check: finding for finding in report.findings}
    assert by_check["cocoindex"].severity == "ok"
    assert by_check["cognee-compat"].severity == "ok"
    for finding in report.findings:
        assert finding.severity in ("ok", "warning", "critical")
    rendered = report.render()
    assert "cogindex doctor" in rendered
    assert "cognee-compat" in rendered
