"""
Unit tests for the per-case fan-out decision logic (app.workers.tasks).

Scope: the PURE helpers that gate retries, backoff, and the kill-switch. The DB-driven
orchestration (idempotent skip, resume re-enqueue, finalize status, crash recovery) is
exercised by the integration suite against a real Postgres+Redis stack — see
docs/excel-batch-fanout/TESTING_PLAN.md §2-3 (IT-2..IT-4, recovery simulations).

Like the rest of backend/tests, this imports the app and runs in the Docker/CI test env.
"""

import importlib

import pytest

tasks = importlib.import_module("app.workers.tasks")


# --- retry classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "error_msg,expected",
    [
        ("Connection refused to vLLM", True),     # LLM_ERROR -> retry
        ("Request timed out after 900s", True),   # TIMEOUT  -> retry
        ("vllm model failed to generate", True),  # LLM_ERROR -> retry
        ("Failed to parse JSON response", False), # PARSE_ERROR -> no retry
        ("Schema validation failed: required field", False),  # SCHEMA_ERROR -> no retry
        ("Some totally unknown weirdness", False),  # UNKNOWN -> no retry
        (None, False),
        ("", False),
    ],
)
def test_is_retryable(error_msg, expected):
    assert tasks._is_retryable(error_msg) is expected


def test_retry_backoff_is_exponential_and_capped():
    assert tasks._retry_backoff_seconds(0) == 30
    assert tasks._retry_backoff_seconds(1) == 60
    assert tasks._retry_backoff_seconds(2) == 120
    assert tasks._retry_backoff_seconds(5) == 120  # capped


# --- kill-switch ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),     # unset -> default ON
        ("1", True),
        ("true", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
    ],
)
def test_fanout_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("BATCH_FANOUT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BATCH_FANOUT_ENABLED", value)
    assert tasks._fanout_enabled() is expected


# --- task registration / queue routing ------------------------------------------------


def test_fanout_tasks_registered():
    """The new coordinator/worker/finalizer tasks exist and are wired into Celery."""
    registered = tasks.celery_app.tasks
    for name in ("start_job", "process_one_case", "finalize_job", "process_batch"):
        assert name in registered, f"{name} not registered"


def test_process_one_case_ignores_result():
    """Per-case results must not accumulate in the Redis backend (R-4)."""
    assert tasks.process_one_case.ignore_result is True


def test_queue_routes_present():
    from app.workers.celery_app import TASK_ROUTES
    assert TASK_ROUTES["process_one_case"]["queue"] == "cases"
    assert TASK_ROUTES["process_case"]["queue"] == "interactive"
    assert TASK_ROUTES["finalize_job"]["queue"] == "celery"
