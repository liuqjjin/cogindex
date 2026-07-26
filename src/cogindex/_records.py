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
# differ from the desired state and force a conservative rebuild.
RECORD_SCHEMA_VERSION = 2


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
      ``processing_fingerprint`` / ``schema_version``: derivative-affecting;
      a difference forces purge + re-add + cognify.
    - ``importance_weight_fingerprint``: a difference forces hard deletion
      and recreation because Cognee 1.4 does not update the weight on an
      existing raw data row. The empty default keeps older records decodable
      and deliberately makes their unknown weight differ from every real
      fingerprint.
    - ``metadata_fingerprint``: label-only metadata; a difference re-adds the
      document without purging graph/vector derivatives.
    """

    data_id: uuid.UUID
    content_fingerprint: str
    annotations_fingerprint: str
    metadata_fingerprint: str
    processing_fingerprint: str
    importance_weight_fingerprint: str = ""
    schema_version: int = RECORD_SCHEMA_VERSION
