"""Identity and fingerprint invariants for ``cogindex._identity`` (ADR-0002).

Golden values pinned here are identity-stability contracts: changing any of
them renames every managed document. They must never change.
"""

from __future__ import annotations

import uuid

import pytest

from cogindex._identity import (
    COGINDEX_NAMESPACE,
    canonical_join,
    document_data_id,
    fingerprint_content,
    fingerprint_json,
    normalize_external_key,
)

# NFC and NFD spellings of "café": distinct code-point sequences on purpose.
_CAFE_NFC = "café"
_CAFE_NFD = "café"

_HEX_DIGITS = set("0123456789abcdef")


class TestCanonicalJoin:
    def test_length_prefix_encoding(self) -> None:
        assert canonical_join("a", "b:c") == "1:a3:b:c"

    def test_empty_call_is_empty_string(self) -> None:
        assert canonical_join() == ""

    def test_single_empty_segment_encoding(self) -> None:
        assert canonical_join("") == "0:"

    def test_injective_colon_shift(self) -> None:
        assert canonical_join("a", "b:c") != canonical_join("a:b", "c")

    def test_injective_empty_segment_vs_split(self) -> None:
        assert canonical_join("ab", "") != canonical_join("a", "b")

    def test_injective_empty_segment_vs_no_segments(self) -> None:
        assert canonical_join("") != canonical_join()

    def test_length_is_character_count_not_bytes(self) -> None:
        # "é" is one character but two UTF-8 bytes; the prefix counts chars.
        assert canonical_join("é") == "1:é"


class TestNormalizeExternalKey:
    def test_nfc_and_nfd_normalize_identically(self) -> None:
        assert _CAFE_NFC != _CAFE_NFD  # genuinely different spellings
        assert normalize_external_key(_CAFE_NFC) == normalize_external_key(_CAFE_NFD)

    def test_nfc_and_nfd_yield_same_document_data_id(self) -> None:
        nfc_id = document_data_id("rt", "tenant", "ds", _CAFE_NFC)
        nfd_id = document_data_id("rt", "tenant", "ds", _CAFE_NFD)
        assert nfc_id == nfd_id

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            normalize_external_key("")

    def test_nul_character_raises(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            normalize_external_key("a\x00b")

    def test_idempotent(self) -> None:
        for key in (_CAFE_NFC, _CAFE_NFD, "plain/key.md"):
            once = normalize_external_key(key)
            assert normalize_external_key(once) == once


class TestCogindexNamespace:
    def test_derived_from_dns_namespace(self) -> None:
        assert uuid.uuid5(uuid.NAMESPACE_DNS, "cogindex") == COGINDEX_NAMESPACE

    def test_golden_literal(self) -> None:
        # Identity-stability golden. This value must NEVER change; changing
        # it renames every managed document.
        assert uuid.UUID("427dd96a-c6b7-5e1b-b82f-a0fbc923770f") == COGINDEX_NAMESPACE


class TestDocumentDataId:
    def test_golden_literals(self) -> None:
        # Identity-stability goldens: computed once and hardcoded; these
        # values must NEVER change (a change renames every managed document).
        assert document_data_id("rt-primary", "tenant-a", "docs", "guide/intro.md") == uuid.UUID(
            "f52de54d-d400-5dea-b331-e6cee711c324"
        )
        assert document_data_id("rt-primary", "tenant-a", "docs", _CAFE_NFC + ".md") == uuid.UUID(
            "eee82025-a8ac-54fa-a5dc-75175b5a8cf1"
        )

    def test_each_coordinate_participates(self) -> None:
        base = document_data_id("rt", "tenant", "ds", "key")
        assert document_data_id("rt2", "tenant", "ds", "key") != base
        assert document_data_id("rt", "tenant2", "ds", "key") != base
        assert document_data_id("rt", "tenant", "ds2", "key") != base
        assert document_data_id("rt", "tenant", "ds", "key2") != base

    def test_deterministic(self) -> None:
        assert document_data_id("rt", "tenant", "ds", "key") == document_data_id(
            "rt", "tenant", "ds", "key"
        )


class TestFingerprintContent:
    def test_str_and_bytes_never_collide(self) -> None:
        assert fingerprint_content("a") != fingerprint_content(b"a")

    def test_deterministic(self) -> None:
        assert fingerprint_content("a") == fingerprint_content("a")
        assert fingerprint_content(b"\x01\x02") == fingerprint_content(b"\x01\x02")

    def test_different_content_differs(self) -> None:
        assert fingerprint_content("a") != fingerprint_content("b")
        assert fingerprint_content(b"a") != fingerprint_content(b"b")

    def test_returns_32_char_hex(self) -> None:
        for fp in (fingerprint_content("a"), fingerprint_content(b"a")):
            assert len(fp) == 32
            assert set(fp) <= _HEX_DIGITS


class TestFingerprintJson:
    def test_dict_key_order_insensitive(self) -> None:
        assert fingerprint_json({"a": 1, "b": 2}) == fingerprint_json({"b": 2, "a": 1})

    def test_nan_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            fingerprint_json(float("nan"))

    def test_set_value_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            fingerprint_json({"a": {1, 2}})

    def test_value_change_changes_fingerprint(self) -> None:
        assert fingerprint_json({"a": 1, "b": 2}) != fingerprint_json({"a": 1, "b": 3})

    def test_returns_32_char_hex(self) -> None:
        fp = fingerprint_json({"a": 1})
        assert len(fp) == 32
        assert set(fp) <= _HEX_DIGITS
