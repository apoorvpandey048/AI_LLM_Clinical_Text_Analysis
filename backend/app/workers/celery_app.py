"""
Celery application configuration.

Configures Celery with Redis as broker and backend.
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

# Create Celery app
celery_app = Celery(
    "snapai",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

# Queue topology (Excel batch fan-out):
#   - "cases"       : per-case fan-out tasks (process_one_case) — the heavy batch work.
#   - "interactive" : single ad-hoc case jobs (process_case) — kept off the batch queue so a
#                     156-case run cannot starve a clinician's one-off submission.
#   - "celery"      : default queue — lightweight coordination (start_job / finalize_job /
#                     the process_batch shim).
# The canonical worker consumes ALL THREE: `celery ... worker -Q cases,interactive,celery`.
TASK_ROUTES = {
    "process_one_case": {"queue": "cases"},
    "process_case": {"queue": "interactive"},
    "start_job": {"queue": "celery"},
    "finalize_job": {"queue": "celery"},
    "process_batch": {"queue": "celery"},
}

# Configure Celery
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Task execution
    task_acks_late=True,  # with per-case fan-out this re-runs only the in-flight case
    task_reject_on_worker_lost=True,

    # Queue routing + default queue
    task_default_queue="celery",
    task_routes=TASK_ROUTES,

    # Result settings. Per-case results live in PostgreSQL, NOT here: process_one_case is
    # ignore_result and the coordinator/shim return only tiny status dicts. This keeps the
    # Redis result backend from accumulating multi-MB per-job payloads (RISK_REGISTER R-4).
    result_expires=86400,  # 24 hours (tiny payloads only)

    # Worker settings
    worker_prefetch_multiplier=1,  # one task reserved at a time (LLM is heavy)
    # Start value; tune against measured single-GPU vLLM headroom (TESTING_PLAN §4 / R-7).
    # Overridable per-deployment via the worker CLI --concurrency flag.
    worker_concurrency=2,

    # Task time limits. With per-case fan-out each task is short (~3 min), so these are now
    # only a safety net, NOT load-bearing (the old monolithic 156-case task blew past 61 min
    # and got SIGKILLed -> redelivery -> restart-from-case-1 livelock; RISK_REGISTER R-1).
    task_soft_time_limit=3600,  # 60 minutes soft limit
    task_time_limit=3660,  # 61 minutes hard limit
)
