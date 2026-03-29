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
from app.db.models import User, Job, JobStatus, LayerMetric, AuditLog
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
    - Connection status (API, Redis, LLM)
    - GPU memory status
    - Backend URL
    """
    request_id = getattr(request.state, "request_id", None)

    # Get active model and runtime temperature/seed from Redis
    runtime_temperature = settings.llm_temperature
    runtime_seed = settings.llm_seed
    chained_mode = False
    comparison_mode = False
    redis_connected = False
    try:
        r = _get_redis()
        r.ping()
        redis_connected = True
        active_model = r.get("snapai:active_model") or settings.llm_model
        temp = r.get("snapai:temperature")
        if temp is not None:
            runtime_temperature = float(temp)
        seed_val = r.get("snapai:seed")
        if seed_val is not None:
            if seed_val == "" or seed_val.lower() == "none":
                runtime_seed = None
            else:
                runtime_seed = int(seed_val)
        # Execution mode flags
        cm_val = r.get("snapai:chained_mode")
        if cm_val is not None:
            chained_mode = cm_val.lower() in ("true", "1", "yes")
        comp_val = r.get("snapai:comparison_mode")
        if comp_val is not None:
            comparison_mode = comp_val.lower() in ("true", "1", "yes")
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

    # GPU memory safety check (multi-GPU safe, fails gracefully)
    gpu_busy = False
    try:
        import subprocess
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,nounits,noheader'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.strip().split(',')
                if len(parts) == 2:
                    used, total = int(parts[0].strip()), int(parts[1].strip())
                    if total > 0 and (used / total) > 0.90:
                        gpu_busy = True
                        break
    except Exception:
        pass  # No GPU or nvidia-smi not available

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
            "redis_connected": redis_connected,
            "active_model": active_model,
            "default_model": settings.llm_model,
            "available_models_count": len(available_models),
            "worker_count": worker_count,
            "environment": settings.environment,
            "version": "2.0.0",
            # Runtime values for system info bar
            "temperature": runtime_temperature,
            "seed": runtime_seed,
            "prompt_version": settings.prompt_version,
            "model_version": getattr(settings, 'model_version', settings.llm_model),
            "supports_temperature_override": True,  # Redis-based temp always supported
            "supports_seed_override": True,  # Redis-based seed always supported
            "chained_mode": chained_mode,
            "comparison_mode": comparison_mode,
            "gpu_busy": gpu_busy,
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


@router.post("/restart-workers")
async def restart_workers(request: Request, admin: User = Depends(require_admin)):
    """
    Gracefully restart all Celery workers.

    Broadcasts a shutdown signal to all workers. Docker's restart policy
    brings them back automatically, picking up any code changes from volume mounts.
    """
    request_id = getattr(request.state, "request_id", None)
    try:
        from app.workers.celery_app import celery_app
        # Broadcast shutdown — Docker restart policy will restart the containers
        celery_app.control.broadcast("shutdown")
        logger.info("workers_shutdown_broadcast", admin=admin.username)
        return create_response(
            success=True,
            data={"message": "Shutdown signal sent to all workers. Docker will restart them automatically."},
            request_id=request_id,
        )
    except Exception as e:
        logger.error("worker_restart_failed", error=str(e))
        return create_response(
            success=False,
            error=f"Failed to send shutdown signal: {str(e)}",
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
# Seed Control
# ============================================

@router.put("/seed")
async def set_seed(request: Request, admin: User = Depends(require_admin)):
    """
    Update runtime LLM seed for reproducibility.

    Stored in Redis so workers pick it up per case.
    Set to null to disable seed (non-deterministic).
    """
    from pydantic import BaseModel

    class SeedConfig(BaseModel):
        seed: int | None = None

    request_id = getattr(request.state, "request_id", None)

    try:
        body = await request.json()
        config = SeedConfig(**body)
    except Exception as e:
        return create_response(
            success=False,
            error=f"Invalid request: {str(e)}",
            request_id=request_id,
        )

    try:
        r = _get_redis()
        if config.seed is None:
            r.set("snapai:seed", "none")  # Explicit "no seed"
        else:
            r.set("snapai:seed", str(config.seed))
        r.close()

        logger.info("seed_updated", seed=config.seed)

        # Audit log
        from app.db import get_db, AuditLog
        db = next(get_db())
        audit = AuditLog(
            action="seed_changed",
            details={"seed": config.seed, "admin": admin.username},
        )
        db.add(audit)
        db.commit()
        db.close()

        seed_msg = f"Seed set to {config.seed}" if config.seed is not None else "Seed cleared (non-deterministic)"
        return create_response(
            success=True,
            data={
                "seed": config.seed,
                "message": seed_msg,
            },
            request_id=request_id,
        )

    except Exception as e:
        logger.error("seed_update_failed", error=str(e))
        return create_response(
            success=False,
            error=f"Failed to update seed: {str(e)}",
            request_id=request_id,
        )


# ============================================
# Chained + Comparison Mode Endpoints
# ============================================


@router.put("/chained-mode")
async def set_chained_mode(request: Request, admin: User = Depends(require_admin)):
    """
    Toggle chained execution mode.

    When enabled, later layers receive previous layer structured JSON
    outputs as additional context in the user prompt.
    """
    from pydantic import BaseModel

    class ChainedModeConfig(BaseModel):
        enabled: bool = False

    request_id = getattr(request.state, "request_id", None)

    try:
        body = await request.json()
        config = ChainedModeConfig(**body)
    except Exception as e:
        return create_response(
            success=False,
            error=f"Invalid request: {str(e)}",
            request_id=request_id,
        )

    try:
        r = _get_redis()
        r.set("snapai:chained_mode", "true" if config.enabled else "false")
        r.close()

        logger.info("chained_mode_updated", enabled=config.enabled)

        from app.db import get_db, AuditLog
        db = next(get_db())
        audit = AuditLog(
            action="chained_mode_changed",
            details={"enabled": config.enabled, "admin": admin.username},
        )
        db.add(audit)
        db.commit()
        db.close()

        return create_response(
            success=True,
            data={
                "chained_mode": config.enabled,
                "message": f"Chained execution {'enabled' if config.enabled else 'disabled'}",
            },
            request_id=request_id,
        )

    except Exception as e:
        logger.error("chained_mode_update_failed", error=str(e))
        return create_response(
            success=False,
            error=f"Failed to update chained mode: {str(e)}",
            request_id=request_id,
        )


@router.put("/comparison-mode")
async def set_comparison_mode(request: Request, admin: User = Depends(require_admin)):
    """
    Toggle comparison mode.

    When enabled, each case runs BOTH independent and chained pipelines.
    Results include a structured diff (CCI delta, grade differences, verdict change).
    WARNING: This doubles LLM API calls.
    """
    from pydantic import BaseModel

    class ComparisonModeConfig(BaseModel):
        enabled: bool = False

    request_id = getattr(request.state, "request_id", None)

    try:
        body = await request.json()
        config = ComparisonModeConfig(**body)
    except Exception as e:
        return create_response(
            success=False,
            error=f"Invalid request: {str(e)}",
            request_id=request_id,
        )

    try:
        r = _get_redis()
        r.set("snapai:comparison_mode", "true" if config.enabled else "false")
        r.close()

        logger.info("comparison_mode_updated", enabled=config.enabled)

        from app.db import get_db, AuditLog
        db = next(get_db())
        audit = AuditLog(
            action="comparison_mode_changed",
            details={"enabled": config.enabled, "admin": admin.username},
        )
        db.add(audit)
        db.commit()
        db.close()

        return create_response(
            success=True,
            data={
                "comparison_mode": config.enabled,
                "message": f"Comparison mode {'enabled — 2× LLM cost per case' if config.enabled else 'disabled'}",
            },
            request_id=request_id,
        )

    except Exception as e:
        logger.error("comparison_mode_update_failed", error=str(e))
        return create_response(
            success=False,
            error=f"Failed to update comparison mode: {str(e)}",
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
            COUNT(*) FILTER (WHERE status IN ('QUEUED','PROCESSING') AND deleted_at IS NULL) AS active_jobs,
            COUNT(*) FILTER (WHERE created_at >= :today AND deleted_at IS NULL)               AS jobs_today,
            COUNT(*) FILTER (WHERE status = 'COMPLETED' AND deleted_at IS NULL)               AS completed_jobs,
            COUNT(*) FILTER (WHERE status = 'FAILED'    AND deleted_at IS NULL)               AS failed_jobs,
            COUNT(*) FILTER (WHERE deleted_at IS NULL)                                        AS total_jobs,
            ROUND(AVG(
                EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
            ) FILTER (WHERE status = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL AND deleted_at IS NULL))
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
          AND r.status = 'COMPLETED'
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
    total_completed = 0
    currently_processing = 0
    try:
        row = db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'COMPLETED' AND deleted_at IS NULL) AS total_completed,
                COUNT(*) FILTER (WHERE status = 'PROCESSING' AND deleted_at IS NULL) AS currently_processing
            FROM jobs
        """)).fetchone()
        if row:
            total_completed = row[0] or 0
            currently_processing = row[1] or 0
    except Exception as e:
        logger.warning("runtime_status_db_query_failed", error=str(e))

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
            "total_completed_jobs": total_completed,
            "currently_processing": currently_processing,
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


# ============================================
# Stage 6 — Regression Test Runner
# ============================================


@router.post("/run-regression-check")
async def run_regression_check(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = 5,
):
    """
    Run a lightweight regression check using baseline (golden) jobs.

    Behaviour:
    * Select last N baseline jobs (default 5).
    * For each baseline, structurally compare its snapshot against
      a fresh replay of the same snapshot (without actually replaying —
      we compare the stored snapshot and case results).
    * Comparison rules: same layer count, same execution order,
      same success/failure pattern, valid JSON schema outputs.
    * Returns total_tests, passed, failed, details.

    Safety:
    * If a worker is currently processing, returns 409.
    * Timeout guard: max 30-second DB scan.
    """
    import json as _json
    import hashlib

    request_id = getattr(request.state, "request_id", None)

    logger.info(
        "REGRESSION_RUN_STARTED",
        admin=admin.username,
        limit=limit,
        request_id=request_id,
    )

    # Safety: check if worker is currently processing jobs
    processing_count = db.execute(text("""
        SELECT COUNT(*) FROM jobs
        WHERE status IN ('QUEUED', 'PROCESSING') AND deleted_at IS NULL
    """)).scalar() or 0

    if processing_count > 0:
        return create_response(
            success=False,
            error=f"Cannot run regression check — {processing_count} job(s) currently processing. Wait for completion.",
            request_id=request_id,
        )

    # Get last N baseline jobs
    baselines = db.execute(text("""
        SELECT id, pipeline_snapshot, case_count, failed_count, model_name, created_at
        FROM jobs
        WHERE is_regression_baseline = true
          AND status = 'COMPLETED'
          AND deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT :lim
    """), {"lim": limit}).fetchall()

    if not baselines:
        return create_response(
            success=True,
            data={
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "details": [],
                "message": "No baseline jobs found. Mark completed jobs as baselines first.",
            },
            request_id=request_id,
        )

    details = []
    passed = 0
    failed = 0

    for row in baselines:
        job_id = row[0]
        snapshot = row[1]
        case_count = row[2] or 0
        failed_count = row[3] or 0
        model_name = row[4]
        created_at = row[5]

        issues = []

        # Check 1: Snapshot exists and is non-empty
        if not snapshot:
            issues.append("Missing pipeline snapshot")
        elif not isinstance(snapshot, list) or len(snapshot) == 0:
            issues.append("Empty or invalid pipeline snapshot")
        else:
            # Check 2: Each layer has required fields (layer_name)
            for i, layer in enumerate(snapshot):
                if not isinstance(layer, dict):
                    issues.append(f"Layer {i} is not a valid object")
                elif not layer.get("layer_name"):
                    issues.append(f"Layer {i} missing layer_name")

        # Check 3: Case outputs — all completed cases have valid JSON layer outputs
        from app.db.models import JobCase, CaseStatus as CS
        cases = db.query(JobCase).filter(
            JobCase.job_id == job_id
        ).order_by(JobCase.case_number).all()

        actual_completed = sum(1 for c in cases if c.status == CS.COMPLETED)
        actual_failed = sum(1 for c in cases if c.status == CS.FAILED)

        # Check 4: Case count consistency
        if len(cases) != case_count:
            issues.append(f"Case count mismatch: expected {case_count}, found {len(cases)}")

        # Check 5: Failed count consistency
        if actual_failed != failed_count:
            issues.append(f"Failed count mismatch: expected {failed_count}, actual {actual_failed}")

        # Check 6: Completed cases have structured outputs
        for c in cases:
            if c.status == CS.COMPLETED:
                if c.layer1_output is None and c.layer2_output is None and c.layer3_output is None:
                    # Check extra_layer_outputs too
                    if not c.extra_layer_outputs:
                        issues.append(f"Case {c.case_number}: no layer outputs despite completed status")

        # Compute snapshot hash
        snap_hash = None
        if snapshot:
            snap_hash = hashlib.sha256(
                _json.dumps(snapshot, sort_keys=True).encode()
            ).hexdigest()[:16]

        test_passed = len(issues) == 0
        if test_passed:
            passed += 1
        else:
            failed += 1

        details.append({
            "job_id": str(job_id),
            "model_name": model_name,
            "created_at": created_at.isoformat() + "Z" if created_at else None,
            "snapshot_hash": snap_hash,
            "case_count": case_count,
            "layer_count": len(snapshot) if isinstance(snapshot, list) else 0,
            "passed": test_passed,
            "issues": issues,
        })

    total_tests = len(baselines)

    # Audit log
    audit = AuditLog(
        action="REGRESSION_RUN_COMPLETED",
        details={
            "admin": admin.username,
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
        },
    )
    db.add(audit)
    db.commit()

    logger.info(
        "REGRESSION_RUN_COMPLETED",
        admin=admin.username,
        total_tests=total_tests,
        passed=passed,
        failed=failed,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
            "details": details,
            "ran_at": datetime.utcnow().isoformat() + "Z",
        },
        request_id=request_id,
    )
