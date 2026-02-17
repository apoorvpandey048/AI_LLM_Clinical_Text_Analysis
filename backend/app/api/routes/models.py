"""
Models API routes.

Allows doctors to list available models, switch between them,
pull new models, and delete unused ones.
"""

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm import OllamaClient, VLLMClient, get_llm_client
from app.utils import get_logger

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1/models", tags=["Models"])


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


class SetActiveModelRequest(BaseModel):
    """Request to set the active model."""
    model: str = Field(..., min_length=1)


class PullModelRequest(BaseModel):
    """Request to pull a new model."""
    model: str = Field(..., min_length=1)


class DeleteModelRequest(BaseModel):
    """Request to delete a model."""
    model: str = Field(..., min_length=1)


@router.get("")
async def list_models(request: Request):
    """
    List all available models. Supports both Ollama and vLLM backends.
    """
    request_id = getattr(request.state, "request_id", None)

    models = []
    try:
        if settings.llm_backend == "vllm":
            client = VLLMClient()
            model_names = await client.list_models()
            models = [{"name": m, "size": "N/A", "family": "vllm"} for m in model_names]
        else:
            client = OllamaClient()
            models = await client.list_models()
    except Exception as e:
        logger.warning("list_models_failed", backend=settings.llm_backend, error=str(e))

    # Get active model
    try:
        r = _get_redis()
        active_model = r.get("snapai:active_model") or settings.llm_model
        r.close()
    except Exception:
        active_model = settings.llm_model

    return create_response(
        success=True,
        data={
            "models": models,
            "active_model": active_model,
            "default_model": settings.llm_model,
            "backend": settings.llm_backend,
        },
        request_id=request_id,
    )


@router.get("/active")
async def get_active_model(request: Request):
    """Get the currently active model."""
    request_id = getattr(request.state, "request_id", None)

    try:
        r = _get_redis()
        active_model = r.get("snapai:active_model") or settings.llm_model
        r.close()
    except Exception:
        active_model = settings.llm_model

    return create_response(
        success=True,
        data={"active_model": active_model},
        request_id=request_id,
    )


@router.put("/active")
async def set_active_model(body: SetActiveModelRequest, request: Request):
    """
    Set the active model for processing. Works with both Ollama and vLLM.
    """
    request_id = getattr(request.state, "request_id", None)

    # Verify model exists (skip check for vLLM as model is pre-loaded)
    if settings.llm_backend == "ollama":
        client = OllamaClient()
        models = await client.list_models()
        model_names = [m["name"] for m in models]

        if body.model not in model_names:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{body.model}' not found. Available models: {', '.join(model_names)}",
            )
    else:
        # For vLLM, verify via API
        client = VLLMClient()
        model_names = await client.list_models()
        if model_names and body.model not in model_names:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{body.model}' not loaded in vLLM. Available: {', '.join(model_names)}",
            )

    # Store active model in Redis
    try:
        r = _get_redis()
        r.set("snapai:active_model", body.model)
        r.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set active model: {str(e)}")

    logger.info(
        "active_model_changed",
        model=body.model,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "active_model": body.model,
            "message": f"Active model changed to {body.model}",
        },
        request_id=request_id,
    )


@router.post("/pull")
async def pull_model(body: PullModelRequest, request: Request):
    """
    Pull a model from the Ollama registry with SSE progress streaming.

    Only available for the Ollama backend — vLLM models are pre-loaded.
    """
    request_id = getattr(request.state, "request_id", None)

    if settings.llm_backend == "vllm":
        raise HTTPException(
            status_code=400,
            detail="Model pull is not applicable for the vLLM backend. "
                   "Models are pre-loaded when vLLM starts.",
        )

    client = OllamaClient()
    logger.info("model_pull_requested", model=body.model, request_id=request_id)

    async def progress_stream():
        """Generator that yields SSE events for pull progress."""
        success = False
        last_percent = -1
        try:
            async for data in client.pull_model_stream(body.model):
                # Calculate percentage if bytes info available
                completed = data.get("completed", 0)
                total = data.get("total", 0)
                status = data.get("status", "")

                event = {"status": status}
                if total > 0:
                    percent = int((completed / total) * 100)
                    event["completed"] = completed
                    event["total"] = total
                    event["percent"] = percent
                    # Throttle: only send every 2% change to avoid flooding
                    if percent == last_percent and percent < 100:
                        continue
                    last_percent = percent
                elif "success" in status.lower():
                    event["percent"] = 100

                yield f"data: {json.dumps(event)}\n\n"

            success = True
            yield f"data: {json.dumps({'done': True, 'success': True, 'message': f'Model {body.model} pulled successfully'})}\n\n"

            logger.info("model_pull_complete", model=body.model, request_id=request_id)

        except Exception as e:
            logger.error("model_pull_failed", model=body.model, error=str(e), request_id=request_id)
            yield f"data: {json.dumps({'done': True, 'success': False, 'error': str(e)})}\n\n"

    return StreamingResponse(
        progress_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("")
async def delete_model(body: DeleteModelRequest, request: Request):
    """
    Delete a model from Ollama.

    Cannot delete the currently active model.
    Only available for the Ollama backend.
    """
    request_id = getattr(request.state, "request_id", None)

    if settings.llm_backend == "vllm":
        raise HTTPException(
            status_code=400,
            detail="Model deletion is not applicable for the vLLM backend. "
                   "Models are managed by the vLLM server process.",
        )

    # Check if it's the active model
    try:
        r = _get_redis()
        active_model = r.get("snapai:active_model") or settings.llm_model
        r.close()
    except Exception:
        active_model = settings.llm_model

    if body.model == active_model:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the currently active model. Switch to a different model first.",
        )

    client = OllamaClient()
    success = await client.delete_model(body.model)

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to delete model '{body.model}'")

    # Refresh model list
    models = await client.list_models()

    logger.info(
        "model_deleted",
        model=body.model,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "message": f"Model '{body.model}' deleted",
            "models": models,
        },
        request_id=request_id,
    )


@router.get("/{model_name}/info")
async def get_model_info(model_name: str, request: Request):
    """Get detailed info about a specific model."""
    request_id = getattr(request.state, "request_id", None)

    if settings.llm_backend == "vllm":
        # For vLLM, check if the model is loaded via the /v1/models endpoint
        client = VLLMClient()
        model_names = await client.list_models()
        if model_name not in model_names:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model_name}' not loaded in vLLM. Available: {', '.join(model_names)}",
            )
        return create_response(
            success=True,
            data={
                "model": model_name,
                "info": {
                    "name": model_name,
                    "backend": "vllm",
                    "status": "loaded",
                },
            },
            request_id=request_id,
        )

    client = OllamaClient()
    info = await client.model_info(model_name)

    if not info:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    return create_response(
        success=True,
        data={"model": model_name, "info": info},
        request_id=request_id,
    )
