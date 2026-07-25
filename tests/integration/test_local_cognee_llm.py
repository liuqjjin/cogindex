"""Opt-in real-LLM end-to-end test (marker: integration_llm).

Runs the full pipeline: real LLM, real embeddings, real local stack: with
no mocks at all. Costs money and is nondeterministic by nature, so it is
opt-in: set ``COGINDEX_RUN_LLM_TESTS=1`` and configure an LLM provider
(``LLM_API_KEY`` etc., see .env.example). Assertions are structural
(documents ingested, cognify completed, search returns results), never
about specific model output.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

import cogindex
from cogindex import CognifyProfile, DatasetHandle, DocumentPayload, LocalCogneeRuntime

pytestmark = [
    pytest.mark.integration_llm,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        os.environ.get("COGINDEX_RUN_LLM_TESTS") != "1",
        reason="real-LLM tests are opt-in: set COGINDEX_RUN_LLM_TESTS=1",
    ),
]

RUNTIME_KEY_NAME = "cognee_runtime_llm"
TENANT = "default"
DATASET = "llm_e2e"


def did(key: str) -> uuid.UUID:
    return cogindex.document_data_id(RUNTIME_KEY_NAME, TENANT, DATASET, key)


@pytest.fixture
async def runtime(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[LocalCogneeRuntime]:
    import cognee
    from cognee.modules.engine.operations.setup import setup

    base = tmp_path_factory.mktemp("cognee-llm")
    local_runtime = LocalCogneeRuntime(
        data_root=str(base / "data"), system_root=str(base / "system")
    )
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await setup()
    yield local_runtime


async def test_full_pipeline_with_real_llm(runtime: LocalCogneeRuntime) -> None:
    import cognee

    handle = await runtime.add_documents(
        DatasetHandle(name=DATASET, tenant=TENANT),
        [
            DocumentPayload(
                data_id=did("solar.md"),
                content=(
                    "The Sun is the star at the center of the Solar System. "
                    "Earth orbits the Sun once per year."
                ),
            ),
        ],
    )
    assert handle.dataset_id is not None
    await runtime.cognify_dataset(handle, CognifyProfile())

    stored = await runtime.list_documents(handle)
    assert len(stored) == 1
    assert stored[0].cognify_complete

    results = await cognee.search("What does Earth orbit?", datasets=[handle.dataset_id])
    assert results  # structural only: the hybrid index answers something
