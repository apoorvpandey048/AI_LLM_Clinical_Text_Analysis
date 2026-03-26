"""
Saved Cases API routes.

Provides CRUD for saved case snapshots.
Security: users can only access their own saved cases.
Backend enforces role-based filtering — doctors cannot see layer outputs.
"""

import copy
import uuid

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.routes.auth import require_auth
from app.db import get_db
from app.db.models import SavedCase, JobCase, User, UserRole, CaseStatus
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/saved-cases", tags=["Saved Cases"])


# Fields to strip for non-admin users
_SENSITIVE_FIELDS = {
    "layer1_output", "layer2_output", "layer3_output",
    "extra_layer_outputs",
    "layer1_raw_output", "layer2_raw_output", "layer3_raw_output",
    "extra_layer_raw_outputs",
    "prompt_version", "model_version", "pipeline_snapshot",
}


def _strip_sensitive(snapshot: dict | None, user: User) -> dict | None:
    """Remove layer outputs and raw data from snapshot for non-admin users."""
    if snapshot is None or user.role == UserRole.ADMIN:
        return snapshot
    return {k: v for k, v in snapshot.items() if k not in _SENSITIVE_FIELDS}


def create_response(success: bool, data=None, error=None, request_id=None):
    """Standardized API response."""
    return {
        "success": success,
        "data": data,
        "error": error,
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id,
        },
    }


def _check_ownership(saved_case: SavedCase, user: User):
    """Raise 403 if user doesn't own the saved case."""
    if saved_case.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")


# ============================================
# POST /saved-cases — save a case snapshot
# ============================================

@router.post("")
async def save_case(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Save a completed case as an immutable snapshot.

    Request body: { job_id, case_id, name (optional) }

    Rules:
    - Case must be completed
    - Duplicate (same job_id + case_id per user) is rejected
    - results_snapshot is a deep copy frozen at save time
    """
    request_id = getattr(request.state, "request_id", None)
    body = await request.json()

    job_id_str = body.get("job_id")
    case_id_str = body.get("case_id")
    name = body.get("name", "").strip()

    if not job_id_str or not case_id_str:
        raise HTTPException(status_code=400, detail="job_id and case_id are required")

    try:
        job_uuid = uuid.UUID(job_id_str)
        case_uuid = uuid.UUID(case_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID format")

    # Fetch original case
    job_case = db.query(JobCase).filter(
        JobCase.id == case_uuid,
        JobCase.job_id == job_uuid,
    ).first()

    if not job_case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Must be completed
    if job_case.status != CaseStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail="Cannot save — case is not completed yet",
        )

    # Check for duplicate (same user + job + case)
    existing = db.query(SavedCase).filter(
        and_(
            SavedCase.user_id == user.id,
            SavedCase.job_id == job_uuid,
            SavedCase.case_id == case_uuid,
            SavedCase.deleted_at.is_(None),
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This case is already saved",
        )

    # Build immutable deep copy of results
    results_snapshot = copy.deepcopy(job_case.to_dict(include_outputs=True))

    if not name:
        name = f"Case #{job_case.case_number}"
        if job_case.case_label:
            name = job_case.case_label

    saved = SavedCase(
        user_id=user.id,
        job_id=job_uuid,
        case_id=case_uuid,
        name=name,
        input_text=job_case.input_text or "",
        results_snapshot=results_snapshot,
    )
    db.add(saved)
    db.commit()
    db.refresh(saved)

    logger.info("case_saved", saved_case_id=str(saved.id), user_id=str(user.id),
                job_id=job_id_str, case_id=case_id_str)

    return create_response(
        success=True,
        data=saved.to_dict(),
        request_id=request_id,
    )


# ============================================
# GET /saved-cases — list (user's own only)
# ============================================

@router.get("")
async def list_saved_cases(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """List all saved cases for the current user (not soft-deleted)."""
    request_id = getattr(request.state, "request_id", None)

    cases = db.query(SavedCase).filter(
        and_(
            SavedCase.user_id == user.id,
            SavedCase.deleted_at.is_(None),
        )
    ).order_by(SavedCase.created_at.desc()).all()

    return create_response(
        success=True,
        data={"cases": [c.to_dict() for c in cases], "count": len(cases)},
        request_id=request_id,
    )


# ============================================
# GET /saved-cases/{id} — detail (role-filtered)
# ============================================

@router.get("/{saved_case_id}")
async def get_saved_case(
    saved_case_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Get full saved case detail. Layer outputs stripped for non-admin."""
    request_id = getattr(request.state, "request_id", None)

    try:
        sc_uuid = uuid.UUID(saved_case_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    saved = db.query(SavedCase).filter(
        and_(SavedCase.id == sc_uuid, SavedCase.deleted_at.is_(None))
    ).first()

    if not saved:
        raise HTTPException(status_code=404, detail="Saved case not found")

    _check_ownership(saved, user)

    data = saved.to_full_dict()
    # Role-based filtering: strip layer outputs for non-admin
    data["results_snapshot"] = _strip_sensitive(data.get("results_snapshot"), user)

    return create_response(success=True, data=data, request_id=request_id)


# ============================================
# PATCH /saved-cases/{id} — rename
# ============================================

@router.patch("/{saved_case_id}")
async def rename_saved_case(
    saved_case_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Rename a saved case."""
    request_id = getattr(request.state, "request_id", None)
    body = await request.json()
    new_name = body.get("name", "").strip()

    if not new_name:
        raise HTTPException(status_code=400, detail="Name is required")

    try:
        sc_uuid = uuid.UUID(saved_case_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    saved = db.query(SavedCase).filter(
        and_(SavedCase.id == sc_uuid, SavedCase.deleted_at.is_(None))
    ).first()

    if not saved:
        raise HTTPException(status_code=404, detail="Saved case not found")

    _check_ownership(saved, user)

    saved.name = new_name
    db.commit()

    logger.info("saved_case_renamed", saved_case_id=str(saved.id), new_name=new_name)

    return create_response(
        success=True,
        data=saved.to_dict(),
        request_id=request_id,
    )


# ============================================
# DELETE /saved-cases/{id} — soft delete ONLY
# ============================================

@router.delete("/{saved_case_id}")
async def delete_saved_case(
    saved_case_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Soft-delete a saved case. Never hard-deletes."""
    request_id = getattr(request.state, "request_id", None)

    try:
        sc_uuid = uuid.UUID(saved_case_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    saved = db.query(SavedCase).filter(
        and_(SavedCase.id == sc_uuid, SavedCase.deleted_at.is_(None))
    ).first()

    if not saved:
        raise HTTPException(status_code=404, detail="Saved case not found")

    _check_ownership(saved, user)

    saved.deleted_at = datetime.utcnow()
    db.commit()

    logger.info("saved_case_soft_deleted", saved_case_id=str(saved.id))

    return create_response(
        success=True,
        data={"message": "Saved case removed"},
        request_id=request_id,
    )
