"""
Settings API routes.

Handles application settings like retention policy.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.routes.auth import require_auth, require_admin
from app.db.models import User

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
async def get_retention_settings(request: Request, user: User = Depends(require_auth)):
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
    admin: User = Depends(require_admin),
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

    # Audit log
    from app.db import get_db, AuditLog
    db = next(get_db())
    audit = AuditLog(
        action="retention_settings_changed",
        details={"enabled": settings.enabled, "days": settings.days, "admin": admin.username},
    )
    db.add(audit)
    db.commit()
    db.close()

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
