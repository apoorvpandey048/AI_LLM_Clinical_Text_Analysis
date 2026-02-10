"""
Models API routes.

Allows doctors to list available models, switch between them,
pull new models, and delete unused ones.
"""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm import OllamaClient
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
    List all available Ollama models with details.

    Returns model name, size, family, and quantization info.
    """
    request_id = getattr(request.state, "request_id", None)

    client = OllamaClient()
    models = await client.list_models()

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
    Set the active model for processing.

    The model must already be available in Ollama.
    """
    request_id = getattr(request.state, "request_id", None)

    # Verify model exists
    client = OllamaClient()
    models = await client.list_models()
    model_names = [m["name"] for m in models]

    if body.model not in model_names:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{body.model}' not found. Available models: {', '.join(model_names)}",
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
    Pull a model from the Ollama registry.

    This downloads the model and makes it available for use.
    Note: Large models may take several minutes to download.
    """
    request_id = getattr(request.state, "request_id", None)

    client = OllamaClient()

    logger.info("model_pull_requested", model=body.model, request_id=request_id)

    success = await client.pull_model(body.model)

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to pull model '{body.model}'")

    # Refresh model list
    models = await client.list_models()

    return create_response(
        success=True,
        data={
            "message": f"Model '{body.model}' pulled successfully",
            "models": models,
        },
        request_id=request_id,
    )


@router.delete("")
async def delete_model(body: DeleteModelRequest, request: Request):
    """
    Delete a model from Ollama.

    Cannot delete the currently active model.
    """
    request_id = getattr(request.state, "request_id", None)

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

    client = OllamaClient()
    info = await client.model_info(model_name)

    if not info:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    return create_response(
        success=True,
        data={"model": model_name, "info": info},
        request_id=request_id,
    )
