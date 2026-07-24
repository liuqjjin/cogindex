"""Stable identity and fingerprinting (ADR-0002).

Document identity is a pure function of *logical coordinates* — never of
content — so that updated content maps onto the same Cognee ``data_id`` and
replacement works in place. Fingerprints capture content/config and drive
change detection; they never participate in identity.

Fingerprints here deliberately do not reuse cocoindex's memo fingerprints:
these values are persisted in tracking records and must stay stable across
cogindex and cocoindex versions (ADR-0005).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from typing import Any

__all__ = [
    "COGINDEX_NAMESPACE",
    "IDENTITY_SCHEMA_VERSION",
    "canonical_join",
    "document_data_id",
    "fingerprint_content",
    "fingerprint_json",
    "normalize_external_key",
]

# uuid5(NAMESPACE_DNS, "cogindex") — fixed forever; changing it would rename
# every managed document.
COGINDEX_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "cogindex")

# Bump only when the identity derivation itself changes incompatibly. A bump
# gives every managed document a new data_id, which reconciles as delete of
# the old identity + create of the new one.
IDENTITY_SCHEMA_VERSION = 1

_FINGERPRINT_DIGEST_SIZE = 16


def canonical_join(*segments: str) -> str:
    """Join segments into a single string, injectively.

    Uses netstring-style length prefixes (character count), so no choice of
    characters inside segments can create a collision: ``("a", "b:c")`` and
    ``("a:b", "c")`` encode differently.
    """
    return "".join(f"{len(segment)}:{segment}" for segment in segments)


def normalize_external_key(key: str) -> str:
    """Normalize an external document key for identity derivation.

    Applies NFC so visually identical Unicode spellings map to one identity.
    Empty keys and NUL characters are rejected: they have no meaningful
    identity and NUL breaks downstream storage layers.
    """
    if not key:
        raise ValueError("external key must be a non-empty string")
    if "\x00" in key:
        raise ValueError("external key must not contain NUL characters")
    return unicodedata.normalize("NFC", key)


def document_data_id(
    runtime_key: str, tenant: str, dataset_name: str, external_key: str
) -> uuid.UUID:
    """Derive the stable Cognee ``data_id`` for a document (ADR-0002)."""
    return uuid.uuid5(
        COGINDEX_NAMESPACE,
        canonical_join(
            str(IDENTITY_SCHEMA_VERSION),
            runtime_key,
            tenant,
            dataset_name,
            normalize_external_key(external_key),
        ),
    )


def fingerprint_json(obj: Any) -> str:
    """Fingerprint a JSON-serializable object via canonical JSON + BLAKE2b.

    Raises TypeError if ``obj`` is not JSON-serializable; callers surface
    that at declaration time, before anything reaches the engine.
    """
    canonical = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.blake2b(
        canonical.encode("utf-8"), digest_size=_FINGERPRINT_DIGEST_SIZE
    ).hexdigest()


def fingerprint_content(content: str | bytes) -> str:
    """Fingerprint document content. ``str`` and ``bytes`` never collide."""
    if isinstance(content, str):
        payload = b"s\x00" + content.encode("utf-8")
    else:
        payload = b"b\x00" + bytes(content)
    return hashlib.blake2b(payload, digest_size=_FINGERPRINT_DIGEST_SIZE).hexdigest()
