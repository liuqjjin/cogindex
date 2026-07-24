"""Tracking records persisted in CocoIndex's tracking store (ADR-0003/0005).

These are the only state cogindex persists. They must contain no secrets and
no raw content, and they must stay msgspec-serializable: the engine
round-trips them through its own storage and hands them back on the next
reconcile.
"""

from __future__ import annotations

import uuid

import msgspec

__all__ = ["RECORD_SCHEMA_VERSION", "DatasetConfigRecord", "DocumentRecord"]

# Bump when the record layout below changes incompatibly. Old records then
# always diff as "replace", forcing a conservative rebuild.
RECORD_SCHEMA_VERSION = 1


class DatasetConfigRecord(msgspec.Struct, frozen=True):
    """Tracking record for a dataset container target."""

    processing_fingerprint: str
    schema_version: int = RECORD_SCHEMA_VERSION


class DocumentRecord(msgspec.Struct, frozen=True):
    """Tracking record for one managed document.

    Field groups (ADR-0004/0005):

    - ``data_id``: the derived stable identity, recorded for observability
      and drift verification.
    - ``content_fingerprint`` / ``annotations_fingerprint`` /
      ``processing_fingerprint`` / ``schema_version``: derivative-affecting —
      any difference forces purge + re-add + cognify.
    - ``metadata_fingerprint``: benign — a difference re-adds (metadata
      upsert) without purging graph/vector derivatives.
    """

    data_id: uuid.UUID
    content_fingerprint: str
    annotations_fingerprint: str
    metadata_fingerprint: str
    processing_fingerprint: str
    schema_version: int = RECORD_SCHEMA_VERSION
