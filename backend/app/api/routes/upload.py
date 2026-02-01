"""
Upload API routes.

Handles file and text uploads for processing.
"""

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, Job, JobCase, AuditLog
from app.db.models import JobStatus, CaseStatus
from app.api.schemas import TextUploadRequest, UploadResponse
from app.utils import get_logger
from app.workers.tasks import process_batch

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1", tags=["Upload"])


def parse_cases_from_text(text: str) -> list[dict[str, str]]:
    """
    Parse multiple cases from text separated by headers.

    Looks for patterns like "Case #1", "Case #2", "Fall 1", etc.

    Args:
        text: Raw text potentially containing multiple cases

    Returns:
        List of dicts with 'case_label' and 'text'
    """
    # Pattern to match case headers
    pattern = r'(?:^|\n)\s*(Case\s*#?\s*\d+|Fall\s*#?\s*\d+|Patient\s*#?\s*\d+)'

    # Find all case header positions
    matches = list(re.finditer(pattern, text, re.IGNORECASE))

    if not matches:
        # No case headers found - treat as single case
        return [{"case_label": "Case 1", "text": text.strip()}]

    cases = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        case_text = text[start:end].strip()
        if case_text:
            cases.append({
                "case_label": match.group(1).strip(),
                "text": case_text,
            })

    return cases


def extract_text_from_docx(file_path: Path) -> str:
    """
    Extract text from a DOCX file.

    Args:
        file_path: Path to the DOCX file

    Returns:
        Extracted text content
    """
    from docx import Document

    doc = Document(file_path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


@router.post("/upload", response_model=None)
async def upload_cases(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile | None = File(None),
    text: str | None = Form(None),
):
    """
    Upload clinical cases for processing.

    Accepts either:
    - A DOCX file upload
    - Direct text input

    Cases are separated by headers like "Case #1", "Case #2", etc.
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    # Validate input
    if not file and not text:
        raise HTTPException(
            status_code=400,
            detail="Either file or text must be provided",
        )

    # Extract text from file or use direct text
    raw_text = ""
    source_type = "text"
    source_filename = None

    if file:
        # Validate file type
        if not file.filename.endswith(".docx"):
            raise HTTPException(
                status_code=400,
                detail="Only .docx files are supported",
            )

        # Validate file size
        contents = await file.read()
        if len(contents) > settings.max_upload_size_mb * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Maximum size is {settings.max_upload_size_mb}MB",
            )

        # Save temporarily and extract text
        temp_path = Path(f"/tmp/{uuid.uuid4()}.docx")
        try:
            temp_path.write_bytes(contents)
            raw_text = extract_text_from_docx(temp_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        source_type = "file"
        source_filename = file.filename

    else:
        raw_text = text

    # Parse cases from text
    cases = parse_cases_from_text(raw_text)

    if not cases:
        raise HTTPException(
            status_code=400,
            detail="No valid cases found in input",
        )

    logger.info(
        "upload_received",
        request_id=request_id,
        source_type=source_type,
        case_count=len(cases),
    )

    # Create job record
    job = Job(
        status=JobStatus.QUEUED,
        case_count=len(cases),
        source_type=source_type,
        source_filename=source_filename,
    )
    db.add(job)
    db.flush()

    # Create case records
    for i, case_data in enumerate(cases):
        case = JobCase(
            job_id=job.id,
            case_number=i + 1,
            case_label=case_data.get("case_label"),
            status=CaseStatus.QUEUED,
            input_text=case_data["text"],
        )
        db.add(case)

    # Create audit log
    audit = AuditLog(
        request_id=uuid.UUID(request_id) if request_id else None,
        job_id=job.id,
        action="job_created",
        details={
            "source_type": source_type,
            "source_filename": source_filename,
            "case_count": len(cases),
        },
    )
    db.add(audit)

    db.commit()

    # Queue processing task
    task = process_batch.delay(
        str(job.id),
        [{"case_id": str(i + 1), "text": c["text"]} for i, c in enumerate(cases)],
    )

    # Update job with task ID
    job.celery_task_id = task.id
    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    db.commit()

    logger.info(
        "job_queued",
        request_id=request_id,
        job_id=str(job.id),
        task_id=task.id,
        case_count=len(cases),
    )

    return {
        "success": True,
        "data": {
            "job_id": str(job.id),
            "status": "processing",
            "case_count": len(cases),
            "message": f"Processing {len(cases)} case(s)",
        },
        "error": None,
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id,
        },
    }


@router.post("/upload/text", response_model=None)
async def upload_text(
    request: Request,
    body: TextUploadRequest,
    db: Session = Depends(get_db),
):
    """
    Upload clinical text directly (JSON body).

    Alternative to form-data upload for programmatic access.
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    # Parse cases
    cases = parse_cases_from_text(body.text)

    if not cases:
        raise HTTPException(
            status_code=400,
            detail="No valid cases found in input",
        )

    # Create job
    job = Job(
        status=JobStatus.QUEUED,
        case_count=len(cases),
        source_type="text",
    )
    db.add(job)
    db.flush()

    # Create cases
    for i, case_data in enumerate(cases):
        case = JobCase(
            job_id=job.id,
            case_number=i + 1,
            case_label=case_data.get("case_label"),
            status=CaseStatus.QUEUED,
            input_text=case_data["text"],
        )
        db.add(case)

    db.commit()

    # Queue processing
    task = process_batch.delay(
        str(job.id),
        [{"case_id": str(i + 1), "text": c["text"]} for i, c in enumerate(cases)],
    )

    job.celery_task_id = task.id
    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    db.commit()

    return {
        "success": True,
        "data": {
            "job_id": str(job.id),
            "status": "processing",
            "case_count": len(cases),
            "message": f"Processing {len(cases)} case(s)",
        },
        "error": None,
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id,
        },
    }
