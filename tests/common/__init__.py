"""Shared test helpers.

The environment helper follows the pattern of cocoindex's own test suite
(python/tests/common/environment.py): each test module gets an isolated
engine database under a per-session temp directory.
"""

from __future__ import annotations

import pathlib
import tempfile

import cocoindex

_tmp_db_path_base = pathlib.Path(tempfile.mkdtemp()) / "cogindex_tests"


def create_test_env(
    test_file_path: str, suffix: str | None = None
) -> cocoindex.Environment:
    """Create an isolated CocoIndex Environment for a test module.

    Args:
        test_file_path: pass ``__file__``; the engine db path derives from it.
        suffix: extra disambiguator when one module needs several
            environments (each Environment needs a unique db path).
    """
    base_name = pathlib.Path(test_file_path).stem
    if suffix is not None:
        base_name = f"{base_name}__{suffix}"
    settings = cocoindex.Settings.from_env(db_path=_tmp_db_path_base / base_name)
    return cocoindex.Environment(settings)
