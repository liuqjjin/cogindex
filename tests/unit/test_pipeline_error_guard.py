"""The false-success guard on cognee pipeline results (ADR-0003).

cognee's non-incremental pipeline path collects in-task failures as
``PipelineRunErrored`` entries in its RETURN VALUE and does not raise
(observed live: a relative data root breaks ingestion inside the task and
``add()`` returns normally: recorded in
docs/upstream-audit/cognee/findings.md). Treating that as success would
commit tracking records for writes that never happened, so
``LocalCogneeRuntime`` scans every add/cognify result and raises. These
tests pin that scan against synthesized upstream result shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from cogindex._runtime_local import CogneePipelineError, _raise_on_errored_runs


# Type NAMES matter: the guard matches on cognee's class names without
# importing cognee.
@dataclass
class PipelineRunErrored:
    payload: str


@dataclass
class PipelineRunCompleted:
    dataset_id: str


@dataclass
class _AddResult:
    data_ingestion_info: list[dict[str, Any]]


def test_add_result_with_errored_run_raises() -> None:
    result = _AddResult(
        data_ingestion_info=[
            {"run_info": PipelineRunCompleted(dataset_id="d"), "data_id": "a"},
            {"run_info": PipelineRunErrored(payload="ValueError: boom"), "data_id": "b"},
        ]
    )
    with pytest.raises(CogneePipelineError, match="errored pipeline run"):
        _raise_on_errored_runs(result, op="add", dataset="ds")


def test_add_result_error_message_carries_payload() -> None:
    result = _AddResult(
        data_ingestion_info=[{"run_info": PipelineRunErrored(payload="root cause here")}]
    )
    with pytest.raises(CogneePipelineError, match="root cause here"):
        _raise_on_errored_runs(result, op="add", dataset="ds")


def test_cognify_dict_result_with_errored_run_raises() -> None:
    result = {"dataset-id": PipelineRunErrored(payload="cognify failed")}
    with pytest.raises(CogneePipelineError, match="cognify"):
        _raise_on_errored_runs(result, op="cognify", dataset="ds")


def test_clean_results_do_not_raise() -> None:
    _raise_on_errored_runs(
        _AddResult(data_ingestion_info=[{"run_info": PipelineRunCompleted(dataset_id="d")}]),
        op="add",
        dataset="ds",
    )
    _raise_on_errored_runs(
        {"dataset-id": PipelineRunCompleted(dataset_id="d")}, op="cognify", dataset="ds"
    )
    _raise_on_errored_runs(None, op="add", dataset="ds")
    _raise_on_errored_runs(object(), op="cognify", dataset="ds")
