"""
Jobs API routes.

Handles job listing, status, results, and management.
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import desc, and_, Integer
from sqlalchemy.orm import Session

from app.api.routes.auth import require_auth, require_admin
from app.db.models import User, UserRole

from app.db import get_db, Job, JobCase, AuditLog
from app.db.models import JobStatus, CaseStatus
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])


def _check_job_ownership(job: Job, user: User) -> None:
    """Verify the authenticated user owns this job (or is admin)."""
    if user.role == UserRole.ADMIN:
        return
    if job.user_id is not None and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied: you do not own this job")


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
    user: User = Depends(require_auth),
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

    # Ownership: doctors only see own jobs, admin sees all
    if user.role != UserRole.ADMIN:
        query = query.filter(Job.user_id == user.id)

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
    user: User = Depends(require_auth),
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

    _check_job_ownership(job, user)

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


@router.get("/{job_id}/status")
async def get_job_status(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Lightweight job status endpoint for frontend polling fallback.

    Returns only job_id, status, completed/total counts.
    Used when SSE drops and the UI needs to poll for current state.
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

    _check_job_ownership(job, user)

    cases = db.query(JobCase).filter(JobCase.job_id == job_uuid).all()
    completed = sum(1 for c in cases if c.status == CaseStatus.COMPLETED)
    failed = sum(1 for c in cases if c.status == CaseStatus.FAILED)
    total = len(cases)

    return create_response(
        success=True,
        data={
            "job_id": job_id,
            "status": job.status.value,
            "completed": completed,
            "failed": failed,
            "total": total,
        },
        request_id=request_id,
    )


@router.get("/{job_id}/results")
async def get_job_results(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
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

    _check_job_ownership(job, user)

    # Get all cases with full output
    cases = db.query(JobCase).filter(JobCase.job_id == job_uuid).order_by(
        JobCase.case_number
    ).all()

    return create_response(
        success=True,
        data={
            "job_id": str(job.id),
            "status": job.status.value,
            "pipeline_snapshot": job.pipeline_snapshot,
            "model_name": job.model_name,
            "created_at": job.created_at.isoformat() + "Z" if job.created_at else None,
            "completed_at": job.completed_at.isoformat() + "Z" if job.completed_at else None,
            "results": [c.to_dict(include_outputs=True) for c in cases],
        },
        request_id=request_id,
    )


@router.get("/{job_id}/cases/{case_id}")
async def get_case_output(
    job_id: str,
    case_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Get full output for a single case from the database.

    Returns all layer outputs, raw LLM text, and metadata.
    Used by the frontend to load case outputs on page reload.
    """
    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    try:
        case_uuid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid case ID format")

    job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    _check_job_ownership(job, user)

    case = db.query(JobCase).filter(
        and_(JobCase.job_id == job_uuid, JobCase.id == case_uuid)
    ).first()

    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    return create_response(
        success=True,
        data=case.to_dict(include_outputs=True),
        request_id=request_id,
    )


@router.post("/{job_id}/reprocess")
async def reprocess_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
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

    _check_job_ownership(job, user)

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
        [{"case_id": str(c.id), "case_number": c.case_number, "text": c.input_text} for c in cases],
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
    user: User = Depends(require_auth),
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

    _check_job_ownership(job, user)

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
    user: User = Depends(require_auth),
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

    _check_job_ownership(job, user)

    # Check if job can be cancelled
    if job.status not in [JobStatus.QUEUED, JobStatus.PROCESSING]:
        raise HTTPException(
            status_code=409,
            detail=f"Job cannot be cancelled — current status: {job.status.value}",
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

    # Update job status to CANCELLED
    job.status = JobStatus.CANCELLED
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
        "JOB_CANCELLED",
        event="JOB_CANCELLED",
        request_id=request_id,
        job_id=job_id,
        cases_cancelled=len(processing_cases),
    )

    # Emit SSE job_failed event so the frontend stops its streaming state
    try:
        from app.utils.stream_manager import StreamPublisher
        pub = StreamPublisher()
        pub.publish_job_failed(job_id, "Job cancelled by user")
        pub.close()
    except Exception as sse_err:
        logger.warning("cancel_sse_emit_failed", job_id=job_id, error=str(sse_err))

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


# ============================================
# Replay
# ============================================


@router.post("/{job_id}/replay")
async def replay_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Replay a completed job using its frozen pipeline snapshot.

    Creates a NEW job that executes with the exact same layer config,
    prompts, and order as the original run.  The original job is never
    mutated.  Current prompt edits do NOT affect the replay.

    Safety rules:
    - Cannot replay a job that is still running (QUEUED / PROCESSING)
    - Cannot replay a job without a pipeline_snapshot
    - Cannot replay a job that is itself a replay of a running source
    """
    import hashlib

    request_id = getattr(request.state, "request_id", None)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    source_job = db.query(Job).filter(
        and_(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).first()

    if not source_job:
        raise HTTPException(status_code=404, detail="Job not found")

    _check_job_ownership(source_job, user)

    # ── Safety checks ──
    if source_job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
        raise HTTPException(
            status_code=409,
            detail="Cannot replay a job that is still running",
        )

    if not source_job.pipeline_snapshot:
        raise HTTPException(
            status_code=409,
            detail="Cannot replay — job has no pipeline snapshot",
        )

    # Prevent duplicate replay loops: if there is already a QUEUED/PROCESSING
    # replay of this exact source, block a second one.
    existing_replay = db.query(Job).filter(
        Job.replay_source_id == source_job.id,
        Job.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING]),
        Job.deleted_at.is_(None),
    ).first()
    if existing_replay:
        raise HTTPException(
            status_code=409,
            detail=f"A replay of this job is already running (job {existing_replay.id})",
        )

    # ── Get original cases ──
    source_cases = db.query(JobCase).filter(
        JobCase.job_id == job_uuid
    ).order_by(JobCase.case_number).all()

    if not source_cases:
        raise HTTPException(status_code=409, detail="Source job has no cases to replay")

    # ── Create new job ──
    new_job = Job(
        status=JobStatus.QUEUED,
        case_count=len(source_cases),
        source_type=f"replay:{source_job.source_type or 'unknown'}",
        source_filename=source_job.source_filename,
        pipeline_snapshot=source_job.pipeline_snapshot,
        model_name=source_job.model_name,
        replay_source_id=source_job.id,
        user_id=user.id,
    )
    db.add(new_job)
    db.flush()

    # Create case records (copy input_text from source)
    for sc in source_cases:
        new_case = JobCase(
            job_id=new_job.id,
            case_number=sc.case_number,
            case_label=sc.case_label,
            status=CaseStatus.QUEUED,
            input_text=sc.input_text,
        )
        db.add(new_case)

    # Compute snapshot hash for audit
    import json as _json
    snapshot_str = _json.dumps(source_job.pipeline_snapshot, sort_keys=True)
    snapshot_hash = hashlib.sha256(snapshot_str.encode()).hexdigest()[:16]

    # Audit logs
    for action, details in [
        ("REPLAY_STARTED", {"source_job_id": str(source_job.id), "new_job_id": str(new_job.id), "snapshot_hash": snapshot_hash}),
        ("REPLAY_SOURCE_JOB", {"source_job_id": str(source_job.id), "case_count": len(source_cases)}),
        ("REPLAY_NEW_JOB", {"new_job_id": str(new_job.id), "snapshot_hash": snapshot_hash}),
    ]:
        audit = AuditLog(
            request_id=uuid.UUID(request_id) if request_id else None,
            job_id=new_job.id,
            action=action,
            details=details,
        )
        db.add(audit)

    db.commit()

    # ── Queue for processing ──
    from app.workers.tasks import process_batch

    db_cases = db.query(JobCase).filter(
        JobCase.job_id == new_job.id
    ).order_by(JobCase.case_number).all()

    task = process_batch.delay(
        str(new_job.id),
        [{"case_id": str(c.id), "case_number": c.case_number, "text": c.input_text} for c in db_cases],
    )

    new_job.celery_task_id = task.id
    new_job.status = JobStatus.PROCESSING
    new_job.started_at = datetime.utcnow()
    db.commit()

    logger.info(
        "REPLAY_STARTED",
        event="REPLAY_STARTED",
        request_id=request_id,
        source_job_id=job_id,
        new_job_id=str(new_job.id),
        case_count=len(source_cases),
        snapshot_hash=snapshot_hash,
    )

    return create_response(
        success=True,
        data={
            "job_id": str(new_job.id),
            "source_job_id": job_id,
            "status": "processing",
            "case_count": len(source_cases),
            "snapshot_hash": snapshot_hash,
            "message": f"Replaying {len(source_cases)} case(s) from job {job_id[:8]}...",
        },
        request_id=request_id,
    )


# ============================================
# Metrics API
# ============================================


@router.get("/{job_id}/metrics")
async def get_job_metrics(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Get per-layer execution metrics for a job.

    Returns all LayerMetric rows for the given job.
    """
    from app.db.models import LayerMetric
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

    _check_job_ownership(job, user)

    metrics = db.query(LayerMetric).filter(
        LayerMetric.job_id == job_uuid
    ).order_by(LayerMetric.case_number, LayerMetric.created_at).all()

    return create_response(
        success=True,
        data={
            "job_id": job_id,
            "metrics": [m.to_dict() for m in metrics],
        },
        request_id=request_id,
    )


@router.get("/metrics/aggregate")
async def get_aggregate_metrics(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Get aggregated execution metrics across all jobs for the current user.

    Returns per-layer: avg_duration_ms, total_runs, failures, success_rate.
    """
    from app.db.models import LayerMetric
    from sqlalchemy import func
    request_id = getattr(request.state, "request_id", None)

    # Build base query scoped to user's jobs
    base = db.query(LayerMetric).join(Job, LayerMetric.job_id == Job.id).filter(
        Job.deleted_at.is_(None),
    )
    if user.role != UserRole.ADMIN:
        base = base.filter(Job.user_id == user.id)

    rows = base.with_entities(
        LayerMetric.layer_id,
        func.avg(LayerMetric.duration_ms).label("avg_duration_ms"),
        func.count().label("total_runs"),
        func.sum(func.cast(LayerMetric.success == False, Integer)).label("failures"),
    ).group_by(LayerMetric.layer_id).all()

    result = []
    for row in rows:
        total = row.total_runs or 1
        failures = row.failures or 0
        result.append({
            "layer_id": row.layer_id,
            "avg_duration_ms": round(row.avg_duration_ms) if row.avg_duration_ms else 0,
            "total_runs": total,
            "failures": failures,
            "success_rate": round(((total - failures) / total) * 100, 1),
        })

    return create_response(
        success=True,
        data={"layers": result},
        request_id=request_id,
    )


# ============================================
# Run Comparison
# ============================================


@router.get("/compare")
async def compare_jobs(
    request: Request,
    job_a: str = Query(..., description="First job ID"),
    job_b: str = Query(..., description="Second job ID"),
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Compare two job executions side-by-side.

    Returns layer durations, success/failure diffs, and snapshot diffs.
    """
    from app.db.models import LayerMetric
    request_id = getattr(request.state, "request_id", None)

    def _load_job(jid_str):
        try:
            jid = uuid.UUID(jid_str)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid job ID: {jid_str}")
        job = db.query(Job).filter(and_(Job.id == jid, Job.deleted_at.is_(None))).first()
        if not job:
            raise HTTPException(status_code=404, detail=f"Job not found: {jid_str}")
        _check_job_ownership(job, user)
        return job

    ja = _load_job(job_a)
    jb = _load_job(job_b)

    def _metrics_by_layer(job_uuid):
        rows = db.query(LayerMetric).filter(LayerMetric.job_id == job_uuid).all()
        grouped = {}
        for m in rows:
            grouped.setdefault(m.layer_id, []).append(m)
        summary = {}
        for lid, ms in grouped.items():
            total = len(ms)
            fails = sum(1 for m in ms if not m.success)
            avg_dur = sum(m.duration_ms for m in ms) / total if total else 0
            summary[lid] = {
                "avg_duration_ms": round(avg_dur),
                "total_runs": total,
                "failures": fails,
                "success_rate": round(((total - fails) / total) * 100, 1) if total else 0,
            }
        return summary

    ma = _metrics_by_layer(ja.id)
    mb = _metrics_by_layer(jb.id)
    all_layers = sorted(set(list(ma.keys()) + list(mb.keys())))

    comparisons = []
    for lid in all_layers:
        a_data = ma.get(lid, {})
        b_data = mb.get(lid, {})
        comparisons.append({
            "layer_id": lid,
            "job_a": a_data,
            "job_b": b_data,
        })

    # Snapshot diff
    snap_a = ja.pipeline_snapshot or []
    snap_b = jb.pipeline_snapshot or []
    snap_a_names = [l.get("layer_name") for l in snap_a]
    snap_b_names = [l.get("layer_name") for l in snap_b]
    snapshot_same = snap_a_names == snap_b_names

    return create_response(
        success=True,
        data={
            "job_a": {"job_id": str(ja.id), "status": ja.status.value, "created_at": ja.created_at.isoformat() + "Z" if ja.created_at else None, "model_name": ja.model_name},
            "job_b": {"job_id": str(jb.id), "status": jb.status.value, "created_at": jb.created_at.isoformat() + "Z" if jb.created_at else None, "model_name": jb.model_name},
            "layers": comparisons,
            "snapshot_same": snapshot_same,
            "snapshot_a_layers": snap_a_names,
            "snapshot_b_layers": snap_b_names,
        },
        request_id=request_id,
    )