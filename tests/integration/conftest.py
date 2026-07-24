"""Integration-test environment setup.

These assignments must run before cognee is imported anywhere in the
process:

- ``MOCK_EMBEDDING`` is cognee's own deterministic-embedding switch (every
  embedding engine checks it at call time) — the officially supported way
  its test suite runs the full pipeline without an embedding provider.
- ``TELEMETRY_DISABLED`` keeps the integration tier network-free.

The LLM itself is patched per-test via
``patch.object(LLMGateway, "acreate_structured_output", ...)``, mirroring
upstream's own full-pipeline tests (e.g. test_delete_default_graph.py).
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEMETRY_DISABLED", "1")
os.environ.setdefault("MOCK_EMBEDDING", "true")
