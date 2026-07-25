"""Exception types for cogindex."""

from __future__ import annotations

__all__ = [
    "CogindexError",
    "CompatibilityError",
    "LockTimeoutError",
]


class CogindexError(Exception):
    """Base class for all cogindex errors."""


class CompatibilityError(CogindexError):
    """The installed cognee/cocoindex lacks a capability cogindex requires."""


class LockTimeoutError(CogindexError):
    """Timed out acquiring a dataset lock."""
