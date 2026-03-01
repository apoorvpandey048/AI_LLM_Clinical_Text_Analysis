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

# Configure Celery
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    
    # Task execution
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    
    # Result settings
    result_expires=86400,  # 24 hours
    
    # Worker settings
    worker_prefetch_multiplier=1,  # Process one task at a time (LLM is heavy)
    worker_concurrency=1,  # Sequential processing for deterministic inference
    
    # Task time limits – batch processes cases sequentially;
    # CPU inference can take ~15 min/case × 3 layers, so allow 1 hour.
    task_soft_time_limit=3600,  # 60 minutes soft limit
    task_time_limit=3660,  # 61 minutes hard limit
)
