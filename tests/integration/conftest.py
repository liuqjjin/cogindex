"""Integration-test environment setup.

These assignments run before cognee is imported anywhere in the process:

- ``MOCK_EMBEDDING`` is enabled for the deterministic integration tier and
  removed when ``COGINDEX_RUN_LLM_TESTS=1`` selects the opt-in real-provider
  tier. The two modes must not silently share one setting.
- ``TELEMETRY_DISABLED`` keeps the integration tier network-free.

The LLM itself is patched per-test via
``patch.object(LLMGateway, "acreate_structured_output", ...)``, mirroring
upstream's own full-pipeline tests (e.g. test_delete_default_graph.py).
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEMETRY_DISABLED", "1")
if os.environ.get("COGINDEX_RUN_LLM_TESTS") == "1":
    os.environ.pop("MOCK_EMBEDDING", None)
else:
    os.environ["MOCK_EMBEDDING"] = "true"
