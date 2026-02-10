"""
System Info API routes.

Provides system status, LLM backend info, worker count, and health info.
"""

from datetime import datetime

from fastapi import APIRouter, Request

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
async def get_system_info(request: Request):
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

    # Get active model from Redis
    try:
        r = _get_redis()
        active_model = r.get("snapai:active_model") or settings.llm_model
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
            # vLLM health check
            llm_connected = await client.health_check()
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
