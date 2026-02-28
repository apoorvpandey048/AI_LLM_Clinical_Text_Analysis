"""
System Info API routes.

Provides system status, LLM backend info, worker count, and health info.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request

from app.api.routes.auth import require_auth, require_admin
from app.db.models import User
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
