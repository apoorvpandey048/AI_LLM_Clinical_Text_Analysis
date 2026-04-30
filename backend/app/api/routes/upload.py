"""
Upload API routes.

Handles file and text uploads for processing.
Supports multiple file uploads (DOCX, PDF, TXT) with multi-case parsing.
"""

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.routes.auth import require_auth
from app.db.models import User

from app.config import get_settings
from app.db import get_db, Job, JobCase, AuditLog
from app.db.models import JobStatus, CaseStatus
from app.api.schemas import TextUploadRequest, UploadResponse
from app.utils import get_logger
from app.workers.tasks import process_batch

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1", tags=["Upload"])

# Safety limits
MAX_TOTAL_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB
ALLOWED_EXTENSIONS = {".docx", ".pdf", ".txt"}


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


def extract_text_from_pdf(file_path: Path) -> str:
    """
    Extract text from a PDF file.

    Args:
        file_path: Path to the PDF file

    Returns:
        Extracted text content
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    reader = PdfReader(str(file_path))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text and text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def extract_text_from_txt(file_path: Path) -> str:
    """
    Extract text from a plain text file.

    Args:
        file_path: Path to the TXT file

    Returns:
        Text content
    """
    return file_path.read_text(encoding="utf-8", errors="replace")


def extract_text_from_file(file_path: Path, filename: str) -> str:
    """
    Dispatch text extraction based on file extension.

    Args:
        file_path: Path to the saved temp file
        filename: Original filename (for extension detection)

    Returns:
        Extracted text content

    Raises:
        ValueError: If file type is not supported
    """
    ext = Path(filename).suffix.lower()
    if ext == ".docx":
        return extract_text_from_docx(file_path)
    elif ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext == ".txt":
        return extract_text_from_txt(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


@router.post("/upload", response_model=None)
async def upload_cases(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
    files: List[UploadFile] = File(default=[]),
    file: UploadFile | None = File(None),
    text: str | None = Form(None),
    backend: str | None = Form(None),
    openrouter_key: str | None = Form(None),
):
    """
    Upload clinical cases for processing.

    Accepts:
    - Multiple file uploads (DOCX/PDF/TXT) via 'files' field
    - A single file upload via 'file' field (backward compat)
    - Direct text input via 'text' field

    Cases are separated by headers like "Case #1", "Case #2", etc.
    Each file may contain multiple cases. All cases are flattened into one job.
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    logger.info(
        "upload_handler_entered",
        request_id=request_id,
        files_count=len(files) if files else 0,
        has_single_file=bool(file and file.filename),
        has_text=bool(text),
    )

    # Collect all files (merge 'file' into 'files' for backward compat)
    all_files: List[UploadFile] = list(files) if files else []
    if file and file.filename:
        all_files.append(file)

    # Validate: must have files or text
    if not all_files and not text:
        raise HTTPException(
            status_code=400,
            detail="Either file(s) or text must be provided",
        )

    all_cases = []
    source_filenames = []
    file_errors = []

    if all_files:
        # Validate extensions upfront
        for f in all_files:
            ext = Path(f.filename).suffix.lower() if f.filename else ""
            if ext not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file type: '{f.filename}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
                )

        # Read all files and check total size
        file_contents = []
        total_size = 0
        for f in all_files:
            contents = await f.read()
            total_size += len(contents)
            if total_size > MAX_TOTAL_SIZE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Total upload size exceeds {MAX_TOTAL_SIZE_BYTES // (1024 * 1024)}MB limit",
                )
            file_contents.append((f, contents))

        # Process each file individually (graceful per-file failure)
        upload_dir = Path("/app/uploads")
        if not upload_dir.exists():
            upload_dir = Path("./uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)

        for f, contents in file_contents:
            ext = Path(f.filename).suffix.lower()
            temp_path = upload_dir / f"{uuid.uuid4()}{ext}"
            try:
                temp_path.write_bytes(contents)
                raw_text = extract_text_from_file(temp_path, f.filename)

                if not raw_text or not raw_text.strip():
                    file_errors.append(f"{f.filename}: empty or no extractable text")
                    logger.warning("file_empty", filename=f.filename)
                    continue

                cases = parse_cases_from_text(raw_text)
                for case in cases:
                    case["source_file"] = f.filename
                all_cases.extend(cases)
                source_filenames.append(f.filename)

            except Exception as e:
                logger.error("file_extraction_failed", error=str(e), filename=f.filename)
                file_errors.append(f"{f.filename}: {str(e)}")
                # Continue with other files — don't kill the whole upload
                continue
            finally:
                if temp_path.exists():
                    temp_path.unlink()

        source_type = "file"
        # Truncate to fit VARCHAR(255) — with many files, show count instead
        if source_filenames:
            joined = ", ".join(source_filenames)
            if len(joined) > 255:
                source_filename = f"{len(source_filenames)} files: {joined[:200]}..."
            else:
                source_filename = joined
        else:
            source_filename = None

    else:
        # Text-only upload (existing path)
        raw_text = text
        cases = parse_cases_from_text(raw_text)
        for case in cases:
            case["source_file"] = None
        all_cases.extend(cases)
        source_type = "text"
        source_filename = None

    if not all_cases:
        detail = "No valid cases found in input"
        if file_errors:
            detail += f". File errors: {'; '.join(file_errors)}"
        raise HTTPException(status_code=400, detail=detail)

    logger.info(
        "upload_received",
        request_id=request_id,
        source_type=source_type,
        file_count=len(source_filenames) if source_filenames else 0,
        case_count=len(all_cases),
        file_errors=file_errors if file_errors else None,
    )

    # Create job record
    job = Job(
        status=JobStatus.QUEUED,
        case_count=len(all_cases),
        source_type=source_type,
        source_filename=source_filename,
        user_id=user.id,
    )
    db.add(job)
    db.flush()

    # Create case records
    for i, case_data in enumerate(all_cases):
        case = JobCase(
            job_id=job.id,
            case_number=i + 1,
            case_label=case_data.get("case_label"),
            source_file=case_data.get("source_file"),
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
            "case_count": len(all_cases),
            "file_count": len(source_filenames) if source_filenames else 0,
            "file_errors": file_errors if file_errors else None,
        },
    )
    db.add(audit)

    db.commit()

    # Fetch actual case UUIDs from DB after flush
    db_cases = db.query(JobCase).filter(JobCase.job_id == job.id).order_by(JobCase.case_number).all()

    # Queue processing task with real DB case IDs
    task = process_batch.delay(
        str(job.id),
        [{"case_id": str(c.id), "case_number": c.case_number, "text": c.input_text, "source_file": c.source_file} for c in db_cases],
    )

    # Update job with task ID
    job.celery_task_id = task.id
    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    db.commit()

    # Store OpenRouter key in Redis temporarily if applicable
    if backend == "openrouter" and openrouter_key:
        import redis
        r = redis.from_url(settings.redis_url)
        r.setex(f"openrouter:{job.id}", 3600, openrouter_key)
        r.close()

    logger.info(
        "job_queued",
        request_id=request_id,
        job_id=str(job.id),
        task_id=task.id,
        case_count=len(all_cases),
    )

    response_data = {
        "job_id": str(job.id),
        "status": "processing",
        "case_count": len(all_cases),
        "message": f"Processing {len(all_cases)} case(s) from {len(source_filenames)} file(s)" if source_filenames else f"Processing {len(all_cases)} case(s)",
    }
    if file_errors:
        response_data["warnings"] = file_errors

    return {
        "success": True,
        "data": response_data,
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
    user: User = Depends(require_auth),
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
        user_id=user.id,
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

    # Fetch actual case UUIDs from DB after flush
    db_cases_text = db.query(JobCase).filter(JobCase.job_id == job.id).order_by(JobCase.case_number).all()

    # Queue processing
    task = process_batch.delay(
        str(job.id),
        [{"case_id": str(c.id), "case_number": c.case_number, "text": c.input_text} for c in db_cases_text],
    )

    job.celery_task_id = task.id
    job.status = JobStatus.PROCESSING
    job.started_at = datetime.utcnow()
    db.commit()

    # Store OpenRouter key in Redis temporarily if applicable
    if body.backend == "openrouter" and body.openrouter_key:
        import redis
        r = redis.from_url(settings.redis_url)
        r.setex(f"openrouter:{job.id}", 3600, body.openrouter_key)
        r.close()

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
