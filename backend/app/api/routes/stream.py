"""
SSE Streaming endpoint for real-time job progress and LLM token streaming.

Provides Server-Sent Events for:
- Job progress updates (case completion, layer progress)
- Real-time LLM token streaming (typewriter effect)
- Heartbeats to keep connection alive
"""

import json
import uuid
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import and_

from app.db.session import SessionLocal
from app.db.models import Job, JobCase, CaseStatus
from app.utils import get_logger
from app.utils.stream_manager import StreamSubscriber

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Streaming"])


def _get_current_layer(case: JobCase) -> str | None:
    """Determine the current processing layer for a case."""
    if case.status != CaseStatus.PROCESSING:
        return None
    if case.layer3_output is not None:
        return "layer3_ccc"
    if case.layer2_output is not None:
        return "layer3_ccc"
    if case.layer1_output is not None:
        return "layer2_cie"
    return "layer1_ctp"


async def generate_job_events(
    job_id: str,
    db_factory,
    poll_interval: float = 1.0,
    timeout: int = 3600,
) -> AsyncGenerator[str, None]:
    """
    Generate SSE events for job progress with real-time token streaming.

    Combines Redis pub/sub token events with database polling for status.
    """
    import asyncio

    job_uuid = uuid.UUID(job_id)
    last_status = None
    last_completed = -1

    # Subscribe to streaming events from Redis
    subscriber = StreamSubscriber()

    async def poll_db_status():
        """Get current job status from database."""
        db = db_factory()
        try:
            job = db.query(Job).filter(
                and_(Job.id == job_uuid, Job.deleted_at.is_(None))
            ).first()

            if not job:
                return None

            cases = db.query(JobCase).filter(
                JobCase.job_id == job_uuid
            ).order_by(JobCase.case_number).all()

            total = len(cases)
            completed = sum(1 for c in cases if c.status == CaseStatus.COMPLETED)
            failed = sum(1 for c in cases if c.status == CaseStatus.FAILED)
            processing = sum(1 for c in cases if c.status == CaseStatus.PROCESSING)
            done = completed + failed

            return {
                "job_id": job_id,
                "status": job.status.value,
                "total": total,
                "completed": completed,
                "failed": failed,
                "processing": processing,
                "progress_percent": round((done / total) * 100, 1) if total > 0 else 0,
                "cases": [
                    {
                        "case_number": c.case_number,
                        "case_label": c.case_label or f"Case #{c.case_number}",
                        "status": c.status.value,
                        "final_verdict": c.final_verdict,
                        "final_cci": c.final_cci,
                        "current_layer": _get_current_layer(c),
                    }
                    for c in cases
                ],
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        finally:
            db.close()

    try:
        # Start streaming events from Redis pub/sub
        stream_task = asyncio.create_task(_stream_tokens(subscriber, job_id))

        start_time = asyncio.get_event_loop().time()
        token_buffer = []

        async for event in subscriber.subscribe(job_id, timeout=timeout):
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                yield f"event: timeout\ndata: {json.dumps({'message': 'Stream timeout'})}\n\n"
                break

            event_type = event.get("type", "")

            if event_type == "token":
                # Forward token events for typewriter effect
                yield f"event: token\ndata: {json.dumps(event)}\n\n"

            elif event_type == "layer_start":
                yield f"event: layer_start\ndata: {json.dumps(event)}\n\n"

            elif event_type == "layer_complete":
                yield f"event: layer_complete\ndata: {json.dumps(event)}\n\n"

                # Also poll DB for updated progress
                progress = await poll_db_status()
                if progress:
                    yield f"event: progress\ndata: {json.dumps(progress)}\n\n"

            elif event_type == "case_complete":
                yield f"event: case_complete\ndata: {json.dumps(event)}\n\n"

                # Poll DB for updated progress
                progress = await poll_db_status()
                if progress:
                    current_status = (progress["status"], progress["completed"])
                    if current_status != last_status:
                        yield f"event: progress\ndata: {json.dumps(progress)}\n\n"
                        last_status = current_status

                    # Check if job is complete
                    if progress["status"] in ["completed", "failed"]:
                        yield f"event: complete\ndata: {json.dumps(progress)}\n\n"
                        break

            elif event_type == "heartbeat":
                # Periodically poll DB status
                progress = await poll_db_status()
                if progress:
                    current_status = (progress["status"], progress["completed"])
                    if current_status != last_status:
                        yield f"event: progress\ndata: {json.dumps(progress)}\n\n"
                        last_status = current_status

                    if progress["status"] in ["completed", "failed"]:
                        yield f"event: complete\ndata: {json.dumps(progress)}\n\n"
                        break

                yield f"event: heartbeat\ndata: {json.dumps({'time': datetime.utcnow().isoformat() + 'Z'})}\n\n"

    except Exception as e:
        logger.error("sse_stream_error", job_id=job_id, error=str(e))
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
    finally:
        await subscriber.close()


async def _stream_tokens(subscriber: StreamSubscriber, job_id: str):
    """Background task placeholder for token streaming."""
    pass


@router.get("/stream/{job_id}")
async def stream_job(job_id: str):
    """
    Stream real-time updates for a job via Server-Sent Events.

    Events:
    - `token`: LLM token for typewriter display (layer, token)
    - `layer_start`: Layer processing started (case_number, layer)
    - `layer_complete`: Layer processing finished (case_number, layer, success)
    - `case_complete`: Case processing finished (case_number, success, verdict, cci)
    - `progress`: Job-level progress update (status, completed, total)
    - `complete`: Job finished (final status)
    - `heartbeat`: Connection keepalive
    - `error`: Error occurred
    """
    # Validate job exists
    db = SessionLocal()
    try:
        job = db.query(Job).filter(
            and_(Job.id == uuid.UUID(job_id), Job.deleted_at.is_(None))
        ).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
    finally:
        db.close()

    return StreamingResponse(
        generate_job_events(job_id, SessionLocal),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
