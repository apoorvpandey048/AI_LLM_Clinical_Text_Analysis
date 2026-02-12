"""
Jobs API routes.

Handles job listing, status, results, and management.
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import desc, and_
from sqlalchemy.orm import Session

from app.db import get_db, Job, JobCase, AuditLog
from app.db.models import JobStatus, CaseStatus
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])


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


@router.get("")
async def list_jobs(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
):
    """
    List all processing jobs.

    Supports pagination and filtering by status.
    """
    request_id = getattr(request.state, "request_id", None)

    # Base query (exclude soft-deleted)
    query = db.query(Job).filter(Job.deleted_at.is_(None))

    # Filter by status if provided
    if status:
        try:
            status_enum = JobStatus(status)
            query = query.filter(Job.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {status}",
            )

    # Get total count
    total = query.count()

    # Apply pagination
    offset = (page - 1) * limit
    jobs = query.order_by(desc(Job.created_at)).offset(offset).limit(limit).all()

    return create_response(
        success=True,
        data={
            "jobs": [job.to_dict() for job in jobs],
            "total": total,
            "page": page,
            "limit": limit,
        },
        request_id=request_id,
    )


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Get job status by ID.

    Includes per-case status for progress tracking.
    """
    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Get case statuses
    cases = db.query(JobCase).filter(JobCase.job_id == job_uuid).order_by(
        JobCase.case_number
    ).all()

    # Update counts from actual case data
    completed = sum(1 for c in cases if c.status == CaseStatus.COMPLETED)
    failed = sum(1 for c in cases if c.status == CaseStatus.FAILED)

    # Check if job is complete
    if completed + failed == len(cases) and job.status == JobStatus.PROCESSING:
        job.status = JobStatus.COMPLETED if failed == 0 else JobStatus.FAILED
        job.completed_at = datetime.utcnow()
        job.completed_count = completed
        job.failed_count = failed
        db.commit()

    return create_response(
        success=True,
        data={
            **job.to_dict(),
            "cases": [
                {
                    "case_id": str(c.id),
                    "case_number": c.case_number,
                    "case_label": c.case_label,
                    "status": c.status.value,
                    "final_verdict": c.final_verdict,
                    "final_cci": c.final_cci,
                }
                for c in cases
            ],
        },
        request_id=request_id,
    )


@router.get("/{job_id}/results")
async def get_job_results(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Get full results for a job.

    Includes all layer outputs for each case.
    """
    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Get all cases with full output
    cases = db.query(JobCase).filter(JobCase.job_id == job_uuid).order_by(
        JobCase.case_number
    ).all()

    return create_response(
        success=True,
        data={
            "job_id": str(job.id),
            "status": job.status.value,
            "results": [c.to_dict(include_outputs=True) for c in cases],
        },
        request_id=request_id,
    )


@router.post("/{job_id}/reprocess")
async def reprocess_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    case_id: Optional[str] = None,
    overwrite: bool = True,
):
    """
    Reprocess a job or specific case.

    Args:
        job_id: Job to reprocess
        case_id: Optional specific case to reprocess
        overwrite: Whether to overwrite existing results
    """
    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Get cases to reprocess
    if case_id:
        try:
            case_uuid = uuid.UUID(case_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid case ID format")

        cases = db.query(JobCase).filter(
            and_(JobCase.job_id == job_uuid, JobCase.id == case_uuid)
        ).all()
    else:
        cases = db.query(JobCase).filter(JobCase.job_id == job_uuid).all()

    if not cases:
        raise HTTPException(status_code=404, detail="No cases found")

    # Reset case status
    for case in cases:
        case.status = CaseStatus.QUEUED
        case.layer1_output = None if overwrite else case.layer1_output
        case.layer2_output = None if overwrite else case.layer2_output
        case.layer3_output = None if overwrite else case.layer3_output
        case.error_message = None

    # Reset job status
    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    job.completed_at = None

    # Audit log
    audit = AuditLog(
        request_id=uuid.UUID(request_id) if request_id else None,
        job_id=job.id,
        action="job_reprocessed",
        details={
            "case_count": len(cases),
            "overwrite": overwrite,
        },
    )
    db.add(audit)
    db.commit()

    # Queue for reprocessing
    from app.workers.tasks import process_batch

    task = process_batch.delay(
        str(job.id),
        [{"case_id": str(c.case_number), "text": c.input_text} for c in cases],
    )

    job.celery_task_id = task.id
    db.commit()

    logger.info(
        "job_reprocessed",
        request_id=request_id,
        job_id=job_id,
        case_count=len(cases),
    )

    return create_response(
        success=True,
        data={
            "job_id": job_id,
            "message": f"Reprocessing {len(cases)} case(s)",
            "cases_queued": len(cases),
        },
        request_id=request_id,
    )


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Soft delete a job.

    Job is marked as deleted but data is retained.
    """
    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Soft delete
    job.deleted_at = datetime.utcnow()

    # Audit log
    audit = AuditLog(
        request_id=uuid.UUID(request_id) if request_id else None,
        job_id=job.id,
        action="job_deleted",
    )
    db.add(audit)
    db.commit()

    logger.info("job_deleted", request_id=request_id, job_id=job_id)

    return create_response(
        success=True,
        data={"message": "Job deleted"},
        request_id=request_id,
    )


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Cancel a running job.

    Revokes the Celery task and marks the job as failed.
    """
    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check if job can be cancelled
    if job.status not in [JobStatus.QUEUED, JobStatus.PROCESSING]:
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be cancelled - current status: {job.status.value}",
        )

    # Revoke Celery task if available
    revoked = False
    if job.celery_task_id:
        try:
            from app.workers.celery_app import celery_app
            celery_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
            revoked = True
            logger.info("celery_task_revoked", job_id=job_id, task_id=job.celery_task_id)
        except Exception as e:
            logger.warning("celery_revoke_failed", job_id=job_id, error=str(e))

    # Update job status
    job.status = JobStatus.FAILED
    job.completed_at = datetime.utcnow()

    # Update any processing cases to failed
    processing_cases = db.query(JobCase).filter(
        JobCase.job_id == job_uuid,
        JobCase.status.in_([CaseStatus.QUEUED, CaseStatus.PROCESSING]),
    ).all()

    for case in processing_cases:
        case.status = CaseStatus.FAILED
        case.error_message = "Job cancelled by user"

    # Audit log
    audit = AuditLog(
        request_id=uuid.UUID(request_id) if request_id else None,
        job_id=job.id,
        action="job_cancelled",
        details={
            "celery_revoked": revoked,
            "cases_cancelled": len(processing_cases),
        },
    )
    db.add(audit)
    db.commit()

    logger.info(
        "job_cancelled",
        request_id=request_id,
        job_id=job_id,
        cases_cancelled=len(processing_cases),
    )

    return create_response(
        success=True,
        data={
            "message": "Job cancelled",
            "job_id": job_id,
            "celery_revoked": revoked,
            "cases_cancelled": len(processing_cases),
        },
        request_id=request_id,
    )
