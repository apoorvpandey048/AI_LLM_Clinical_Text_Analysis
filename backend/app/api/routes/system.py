"""
System Info API routes.

Provides system status, LLM backend info, worker count, and health info.
Also includes Stage 5 operations dashboard endpoints (admin-only).
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, case as sql_case, text
from sqlalchemy.orm import Session

from app.api.routes.auth import require_auth, require_admin
from app.db import get_db
from app.db.models import User, Job, JobStatus, LayerMetric
from app.config import get_settings
from app.llm import OllamaClient, VLLMClient
from app.utils import get_logger

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1/system", tags=["System"])


def create_response(success: bool, data=None, error=None, request_id=None):
    """Create standardized response."""
    return {
        "success": success,
        "data": data,
        "error": error,
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id,
        },
    }


def _get_redis():
    """Get a Redis client."""
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


@router.get("/info")
async def get_system_info(request: Request, user: User = Depends(require_auth)):
    """
    Get comprehensive system status.

    Returns:
    - LLM backend type (Ollama or vLLM)
    - Active model
    - Worker count
    - Connection status
    - Backend URL
    """
    request_id = getattr(request.state, "request_id", None)

    # Get active model and runtime temperature from Redis
    runtime_temperature = settings.llm_temperature
    try:
        r = _get_redis()
        active_model = r.get("snapai:active_model") or settings.llm_model
        temp = r.get("snapai:temperature")
        if temp is not None:
            runtime_temperature = float(temp)
        r.close()
    except Exception:
        active_model = settings.llm_model

    # Check LLM backend connection
    llm_connected = False
    llm_error = None
    available_models = []

    try:
        if settings.llm_backend == "ollama":
            client = OllamaClient()
            available_models = await client.list_models()
            llm_connected = True
        else:
            client = VLLMClient()
            llm_connected = await client.health_check()
            if llm_connected:
                model_names = await client.list_models()
                available_models = [{"name": m} for m in model_names]
    except Exception as e:
        llm_error = str(e)
        llm_connected = False

    # Get worker info from Celery if available
    worker_count = settings.api_workers
    try:
        from app.workers.celery_app import celery_app
        inspect = celery_app.control.inspect()
        active_workers = inspect.active()
        if active_workers:
            worker_count = len(active_workers)
    except Exception:
        pass  # Use default worker count

    return create_response(
        success=True,
        data={
            "llm_backend": settings.llm_backend,
            "llm_host": settings.llm_host,
            "llm_connected": llm_connected,
            "llm_error": llm_error,
            "active_model": active_model,
            "default_model": settings.llm_model,
            "available_models_count": len(available_models),
            "worker_count": worker_count,
            "environment": settings.environment,
            "version": "2.0.0",
            # Runtime values for system info bar
            "temperature": runtime_temperature,
            "prompt_version": settings.prompt_version,
            "model_version": getattr(settings, 'model_version', settings.llm_model),
        },
        request_id=request_id,
    )


@router.get("/health")
async def health_check(request: Request):
    """Quick health check endpoint."""
    request_id = getattr(request.state, "request_id", None)

    # Check Redis
    redis_ok = False
    try:
        r = _get_redis()
        r.ping()
        r.close()
        redis_ok = True
    except Exception:
        pass

    # Check LLM
    llm_ok = False
    try:
        if settings.llm_backend == "ollama":
            client = OllamaClient()
            models = await client.list_models()
            llm_ok = len(models) > 0
        else:
            client = VLLMClient()
            llm_ok = await client.health_check()
    except Exception:
        pass

    all_ok = redis_ok and llm_ok

    return create_response(
        success=all_ok,
        data={
            "status": "healthy" if all_ok else "degraded",
            "services": {
                "redis": "ok" if redis_ok else "error",
                "llm": "ok" if llm_ok else "error",
            },
        },
        request_id=request_id,
    )


@router.get("/workers")
async def get_workers(request: Request):
    """
    Get current worker pool information.
    """
    request_id = getattr(request.state, "request_id", None)

    worker_info = {
        "configured_count": settings.api_workers,
        "active_count": 0,
        "active_workers": [],
        "pool_type": "unknown",
    }

    try:
        from app.workers.celery_app import celery_app
        inspect = celery_app.control.inspect()

        # Get active workers
        active = inspect.active()
        if active:
            worker_info["active_count"] = len(active)
            worker_info["active_workers"] = list(active.keys())

        # Get pool info
        stats = inspect.stats()
        if stats:
            for worker_name, worker_stats in stats.items():
                worker_info["pool_type"] = worker_stats.get("pool", {}).get("implementation", "unknown")
                break

    except Exception as e:
        logger.warning("worker_inspect_failed", error=str(e))

    return create_response(
        success=True,
        data=worker_info,
        request_id=request_id,
    )


@router.put("/workers")
async def set_worker_concurrency(request: Request, admin: User = Depends(require_admin)):
    """
    Adjust worker pool concurrency at runtime.

    Note: This only works with prefork pool. For gevent/eventlet pools,
    a restart is required to change concurrency.
    """
    from pydantic import BaseModel

    class WorkerConfig(BaseModel):
        concurrency: int

    request_id = getattr(request.state, "request_id", None)

    try:
        body = await request.json()
        config = WorkerConfig(**body)
    except Exception as e:
        return create_response(
            success=False,
            error=f"Invalid request: {str(e)}",
            request_id=request_id,
        )

    if config.concurrency < 1 or config.concurrency > 16:
        return create_response(
            success=False,
            error="Concurrency must be between 1 and 16",
            request_id=request_id,
        )

    try:
        from app.workers.celery_app import celery_app

        # Get current concurrency
        inspect = celery_app.control.inspect()
        stats = inspect.stats()

        if not stats:
            return create_response(
                success=False,
                error="No workers available to configure",
                request_id=request_id,
            )

        # Adjust concurrency for all workers
        # Note: pool_grow/pool_shrink only works with prefork
        current_count = 0
        for worker_name, worker_stats in stats.items():
            pool_info = worker_stats.get("pool", {})
            if pool_info.get("implementation") == "prefork":
                current = pool_info.get("max-concurrency", 1)
                current_count = current

        if current_count == 0:
            # Try to get from active workers
            active = inspect.active()
            if active:
                current_count = len(list(active.values())[0]) if active else 1

        target = config.concurrency
        diff = target - current_count

        if diff > 0:
            celery_app.control.pool_grow(diff)
            action = f"grew pool by {diff}"
        elif diff < 0:
            celery_app.control.pool_shrink(abs(diff))
            action = f"shrunk pool by {abs(diff)}"
        else:
            action = "no change needed"

        logger.info("worker_concurrency_adjusted", target=target, action=action)

        # Audit log
        from sqlalchemy.orm import Session
        from app.db import get_db, AuditLog
        db = next(get_db())
        audit = AuditLog(
            action="worker_concurrency_changed",
            details={"target": target, "previous": current_count, "admin": admin.username},
        )
        db.add(audit)
        db.commit()
        db.close()

        return create_response(
            success=True,
            data={
                "message": f"Worker concurrency adjusted: {action}",
                "target_concurrency": target,
                "previous_concurrency": current_count,
            },
            request_id=request_id,
        )

    except Exception as e:
        logger.error("worker_adjustment_failed", error=str(e))
        return create_response(
            success=False,
            error=f"Failed to adjust workers: {str(e)}. May require restart for non-prefork pools.",
            request_id=request_id,
        )


@router.put("/temperature")
async def set_temperature(request: Request, admin: User = Depends(require_admin)):
    """
    Update runtime LLM temperature.

    Stored in Redis so workers pick it up per case
    (not per job — ensures dynamic updates take effect immediately).
    """
    from pydantic import BaseModel

    class TemperatureConfig(BaseModel):
        temperature: float

    request_id = getattr(request.state, "request_id", None)

    try:
        body = await request.json()
        config = TemperatureConfig(**body)
    except Exception as e:
        return create_response(
            success=False,
            error=f"Invalid request: {str(e)}",
            request_id=request_id,
        )

    if config.temperature < 0.0 or config.temperature > 2.0:
        return create_response(
            success=False,
            error="Temperature must be between 0.0 and 2.0",
            request_id=request_id,
        )

    try:
        r = _get_redis()
        r.set("snapai:temperature", str(config.temperature))
        r.close()

        logger.info("temperature_updated", temperature=config.temperature)

        # Audit log
        from sqlalchemy.orm import Session
        from app.db import get_db, AuditLog
        db = next(get_db())
        audit = AuditLog(
            action="temperature_changed",
            details={"temperature": config.temperature, "admin": admin.username},
        )
        db.add(audit)
        db.commit()
        db.close()

        return create_response(
            success=True,
            data={
                "temperature": config.temperature,
                "message": f"Temperature set to {config.temperature}",
            },
            request_id=request_id,
        )

    except Exception as e:
        logger.error("temperature_update_failed", error=str(e))
        return create_response(
            success=False,
            error=f"Failed to update temperature: {str(e)}",
            request_id=request_id,
        )


# ============================================
# Stage 5 — Operations Dashboard Endpoints
# ============================================


@router.get("/operations-summary")
async def operations_summary(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Aggregated operations summary for the dashboard.
    Uses SQL aggregation — no Python loops over large datasets.
    """
    request_id = getattr(request.state, "request_id", None)
    logger.info("SYSTEM_SUMMARY_FETCHED", admin=admin.username, request_id=request_id)

    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Single query: counts + averages from jobs table
    row = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE status IN ('queued','processing') AND deleted_at IS NULL) AS active_jobs,
            COUNT(*) FILTER (WHERE created_at >= :today AND deleted_at IS NULL)               AS jobs_today,
            COUNT(*) FILTER (WHERE status = 'completed' AND deleted_at IS NULL)               AS completed_jobs,
            COUNT(*) FILTER (WHERE status = 'failed'    AND deleted_at IS NULL)               AS failed_jobs,
            COUNT(*) FILTER (WHERE deleted_at IS NULL)                                        AS total_jobs,
            ROUND(AVG(
                EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
            ) FILTER (WHERE status = 'completed' AND started_at IS NOT NULL AND completed_at IS NOT NULL AND deleted_at IS NULL))
                AS avg_runtime_ms
        FROM jobs
    """), {"today": today_start}).fetchone()

    total = row[4] or 0
    completed = row[2] or 0
    success_rate = round((completed / total) * 100, 1) if total > 0 else 0.0

    return create_response(
        success=True,
        data={
            "active_jobs": row[0] or 0,
            "jobs_today": row[1] or 0,
            "completed_jobs": completed,
            "failed_jobs": row[3] or 0,
            "total_jobs": total,
            "success_rate": success_rate,
            "avg_runtime_ms": int(row[5]) if row[5] else None,
        },
        request_id=request_id,
    )


@router.get("/failure-analytics")
async def failure_analytics(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Error category breakdown from layer_metrics.
    """
    request_id = getattr(request.state, "request_id", None)

    rows = db.execute(text("""
        SELECT
            COALESCE(error_type, 'UNKNOWN') AS category,
            COUNT(*)                        AS cnt
        FROM layer_metrics
        WHERE success = false
        GROUP BY category
        ORDER BY cnt DESC
    """)).fetchall()

    total_failures = sum(r[1] for r in rows)
    categories = []
    for r in rows:
        categories.append({
            "category": r[0],
            "count": r[1],
            "percentage": round((r[1] / total_failures) * 100, 1) if total_failures > 0 else 0,
        })

    return create_response(
        success=True,
        data={
            "total_failures": total_failures,
            "categories": categories,
        },
        request_id=request_id,
    )


@router.get("/determinism")
async def determinism_check(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Lightweight determinism health check.
    Compares replayed jobs vs their source jobs structurally:
    - same layer count in snapshot
    - same layer execution order
    - same failure pattern (no mismatch in which layers failed)
    """
    request_id = getattr(request.state, "request_id", None)
    logger.info("DETERMINISM_CHECK_RUN", admin=admin.username, request_id=request_id)

    # Get all completed replay jobs with their source
    replay_rows = db.execute(text("""
        SELECT
            r.id          AS replay_id,
            r.pipeline_snapshot AS replay_snap,
            r.failed_count      AS replay_fails,
            s.pipeline_snapshot AS source_snap,
            s.failed_count      AS source_fails
        FROM jobs r
        JOIN jobs s ON r.replay_source_id = s.id
        WHERE r.replay_source_id IS NOT NULL
          AND r.status = 'completed'
          AND r.deleted_at IS NULL
          AND s.deleted_at IS NULL
    """)).fetchall()

    total_replays = len(replay_rows)
    matching = 0
    mismatch = 0

    for row in replay_rows:
        replay_snap = row[1] or []
        source_snap = row[3] or []
        replay_fails = row[2] or 0
        source_fails = row[4] or 0

        # Structural comparison: layer names and order
        replay_layers = [l.get("layer_name", "") for l in replay_snap] if isinstance(replay_snap, list) else []
        source_layers = [l.get("layer_name", "") for l in source_snap] if isinstance(source_snap, list) else []

        if replay_layers == source_layers and replay_fails == source_fails:
            matching += 1
        else:
            mismatch += 1

    score = round((matching / total_replays) * 100, 1) if total_replays > 0 else 100.0

    return create_response(
        success=True,
        data={
            "total_replays": total_replays,
            "matching_runs": matching,
            "mismatch_runs": mismatch,
            "determinism_score": score,
        },
        request_id=request_id,
    )


@router.get("/runtime-status")
async def runtime_status(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Model & infrastructure runtime status.
    """
    request_id = getattr(request.state, "request_id", None)

    # Active model from Redis
    active_model = settings.llm_model
    queue_size = 0
    try:
        r = _get_redis()
        active_model = r.get("snapai:active_model") or settings.llm_model
        # Celery default queue length approximation
        queue_size = r.llen("celery") or 0
        r.close()
    except Exception:
        pass

    # DB stats (single query)
    row = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'completed' AND deleted_at IS NULL) AS total_completed,
            COUNT(*) FILTER (WHERE status = 'processing' AND deleted_at IS NULL) AS currently_processing
        FROM jobs
    """)).fetchone()

    # Worker ping — lightweight
    worker_alive = False
    try:
        from app.workers.celery_app import celery_app
        ping = celery_app.control.ping(timeout=1.0)
        worker_alive = len(ping) > 0
    except Exception:
        pass

    return create_response(
        success=True,
        data={
            "active_model": active_model,
            "inference_backend": settings.llm_backend,
            "total_completed_jobs": row[0] or 0,
            "currently_processing": row[1] or 0,
            "worker_alive": worker_alive,
            "queue_size": queue_size,
        },
        request_id=request_id,
    )


@router.get("/slow-layers")
async def slow_layers(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Top slowest layers globally by average duration.
    """
    request_id = getattr(request.state, "request_id", None)

    rows = db.execute(text("""
        SELECT
            layer_id,
            ROUND(AVG(duration_ms))   AS avg_duration_ms,
            COUNT(*)                   AS run_count,
            ROUND(AVG(duration_ms) FILTER (WHERE success = true))  AS avg_success_ms,
            COUNT(*) FILTER (WHERE success = false)               AS failure_count
        FROM layer_metrics
        GROUP BY layer_id
        ORDER BY avg_duration_ms DESC
        LIMIT 10
    """)).fetchall()

    layers = []
    for r in rows:
        layers.append({
            "layer_id": r[0],
            "avg_duration_ms": int(r[1]) if r[1] else 0,
            "run_count": r[2],
            "avg_success_ms": int(r[3]) if r[3] else None,
            "failure_count": r[4] or 0,
        })

    return create_response(
        success=True,
        data={"layers": layers},
        request_id=request_id,
    )
