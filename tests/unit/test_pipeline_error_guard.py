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

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from cogindex import CogindexError
from cogindex._runtime_local import CogneePipelineError, _raise_on_errored_runs


# Type NAMES matter: the guard matches on cognee's class names without
# importing cognee.
@dataclass
class PipelineRunErrored:
    payload: str


@dataclass
class PipelineRunCompleted:
    dataset_id: str
    dataset_name: str
    data_ingestion_info: list[dict[str, Any]] | None = None


@dataclass
class PipelineRunAlreadyCompleted:
    dataset_id: str
    dataset_name: str


@dataclass
class PipelineRunStarted:
    dataset_id: str
    dataset_name: str


def test_add_result_with_errored_run_raises() -> None:
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    result = PipelineRunCompleted(
        dataset_id="d",
        dataset_name="ds",
        data_ingestion_info=[
            {
                "run_info": PipelineRunCompleted(dataset_id="d", dataset_name="ds"),
                "data_id": first_id,
            },
            {
                "run_info": PipelineRunErrored(payload="ValueError: boom"),
                "data_id": second_id,
            },
        ],
    )
    with pytest.raises(CogneePipelineError, match="errored pipeline run"):
        _raise_on_errored_runs(
            result,
            op="add",
            dataset="ds",
            expected_item_count=2,
        )


def test_pipeline_error_is_catchable_as_cogindex_error() -> None:
    result = PipelineRunErrored(payload="pipeline failed")

    with pytest.raises(CogindexError) as exc_info:
        _raise_on_errored_runs(result, op="add", dataset="ds")

    assert isinstance(exc_info.value, CogneePipelineError)
    assert isinstance(exc_info.value, RuntimeError)


def test_add_result_error_message_does_not_copy_upstream_payload() -> None:
    secret = "document-or-provider-secret"
    data_id = uuid.uuid4()
    result = PipelineRunCompleted(
        dataset_id="d",
        dataset_name="ds",
        data_ingestion_info=[{"run_info": PipelineRunErrored(payload=secret), "data_id": data_id}],
    )
    with pytest.raises(CogneePipelineError) as exc_info:
        _raise_on_errored_runs(
            result,
            op="add",
            dataset="ds",
            expected_item_count=1,
        )
    assert secret not in str(exc_info.value)
    assert "Cognee's own logs" in str(exc_info.value)


def test_cognify_dict_result_with_errored_run_raises() -> None:
    dataset_id = uuid.UUID(int=1)
    results = [
        {dataset_id: PipelineRunErrored(payload="cognify failed")},
        {
            dataset_id: PipelineRunCompleted(
                dataset_id=str(dataset_id),
                dataset_name="ds",
                data_ingestion_info=[{"run_info": PipelineRunErrored(payload="nested failure")}],
            )
        },
    ]

    for result in results:
        with pytest.raises(CogneePipelineError, match="cognify"):
            _raise_on_errored_runs(
                result,
                op="cognify",
                dataset="ds",
                expected_dataset_id=dataset_id,
            )


def test_top_level_errored_result_without_item_details_raises() -> None:
    result = PipelineRunErrored(payload="pipeline failed before item results existed")
    with pytest.raises(CogneePipelineError, match="add"):
        _raise_on_errored_runs(result, op="add", dataset="ds")


def test_only_explicit_complete_results_are_accepted() -> None:
    dataset_id = uuid.uuid4()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    _raise_on_errored_runs(
        PipelineRunCompleted(
            dataset_id=str(dataset_id),
            dataset_name="ds",
            data_ingestion_info=[
                {
                    "run_info": PipelineRunCompleted(dataset_id=str(dataset_id), dataset_name="ds"),
                    "data_id": first_id,
                },
                {
                    "run_info": PipelineRunAlreadyCompleted(
                        dataset_id=str(dataset_id), dataset_name="ds"
                    ),
                    "data_id": second_id,
                },
            ],
        ),
        op="add",
        dataset="ds",
        expected_item_count=2,
    )
    _raise_on_errored_runs(
        {
            dataset_id: PipelineRunCompleted(
                dataset_id=str(dataset_id),
                dataset_name="ds",
                data_ingestion_info=[
                    {
                        "run_info": PipelineRunAlreadyCompleted(
                            dataset_id=str(dataset_id), dataset_name="ds"
                        )
                    }
                ],
            )
        },
        op="cognify",
        dataset="ds",
        expected_dataset_id=dataset_id,
    )

    invalid_results: list[tuple[Any, dict[str, Any]]] = [
        (None, {"expected_item_count": 1}),
        (object(), {"expected_item_count": 1}),
        (
            PipelineRunStarted(dataset_id=str(dataset_id), dataset_name="ds"),
            {"expected_item_count": 1},
        ),
        (
            PipelineRunCompleted(
                dataset_id=str(dataset_id),
                dataset_name="ds",
                data_ingestion_info=[],
            ),
            {"expected_item_count": 1},
        ),
        (
            PipelineRunCompleted(
                dataset_id=str(dataset_id),
                dataset_name="ds",
                data_ingestion_info=[{"run_info": object(), "data_id": first_id}],
            ),
            {"expected_item_count": 1},
        ),
        (
            PipelineRunCompleted(
                dataset_id=str(dataset_id),
                dataset_name="ds",
                data_ingestion_info=[
                    {
                        "run_info": PipelineRunCompleted(
                            dataset_id=str(dataset_id), dataset_name="ds"
                        ),
                        "data_id": first_id,
                    },
                    {
                        "run_info": PipelineRunCompleted(
                            dataset_id=str(dataset_id), dataset_name="ds"
                        ),
                        "data_id": first_id,
                    },
                ],
            ),
            {"expected_item_count": 1},
        ),
        (
            PipelineRunCompleted(
                dataset_id="wrong",
                dataset_name="other",
                data_ingestion_info=[
                    {
                        "run_info": PipelineRunCompleted(dataset_id="wrong", dataset_name="other"),
                        "data_id": first_id,
                    }
                ],
            ),
            {
                "expected_item_count": 1,
                "expected_dataset_id": dataset_id,
            },
        ),
        ({}, {"expected_dataset_id": dataset_id}),
        (
            {uuid.uuid4(): PipelineRunCompleted(dataset_id="other", dataset_name="ds")},
            {"expected_dataset_id": dataset_id},
        ),
    ]
    for invalid, expected in invalid_results:
        with pytest.raises(CogneePipelineError):
            _raise_on_errored_runs(invalid, op="add", dataset="ds", **expected)
