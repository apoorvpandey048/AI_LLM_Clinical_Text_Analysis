"""
Settings API routes.

Handles application settings like retention policy.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.api.schemas import RetentionSettings
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

# In-memory settings (would be stored in DB in production)
_retention_settings = RetentionSettings(enabled=False, days=30)


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


@router.get("/retention")
async def get_retention_settings(request: Request):
    """
    Get current auto-delete retention settings.

    Note: Auto-delete is disabled by default per requirements.
    """
    request_id = getattr(request.state, "request_id", None)

    return create_response(
        success=True,
        data={
            "settings": {
                "enabled": _retention_settings.enabled,
                "days": _retention_settings.days,
            },
        },
        request_id=request_id,
    )


@router.put("/retention")
async def update_retention_settings(
    settings: RetentionSettings,
    request: Request,
):
    """
    Update auto-delete retention settings.

    Args:
        enabled: Whether auto-delete is enabled
        days: Number of days to retain results before deletion
    """
    global _retention_settings
    request_id = getattr(request.state, "request_id", None)

    _retention_settings = settings

    logger.info(
        "retention_settings_updated",
        request_id=request_id,
        enabled=settings.enabled,
        days=settings.days,
    )

    return create_response(
        success=True,
        data={
            "settings": {
                "enabled": _retention_settings.enabled,
                "days": _retention_settings.days,
            },
            "message": "Settings updated",
        },
        request_id=request_id,
    )
