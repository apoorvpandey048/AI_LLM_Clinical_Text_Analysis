"""
Celery tasks for SNAP-AI pipeline processing.

Tasks persist results to PostgreSQL after processing.
"""

import uuid
from datetime import datetime
from typing import Any

from app.workers.celery_app import celery_app
from app.config import get_settings
from app.llm import OllamaClient
from app.pipeline import PipelineOrchestrator
from app.db.session import SessionLocal
from app.db.models import Job, JobCase, AuditLog, JobStatus, CaseStatus
from app.utils import get_logger

logger = get_logger(__name__)
settings = get_settings()


def save_case_result(job_id: str, case_number: int, result: dict) -> None:
    """
    Save a case result to the database.

    Args:
        job_id: UUID of the parent job
        case_number: Case number within the job
        result: Result dictionary from pipeline
    """
    db = SessionLocal()
    try:
        # Find the case by job_id and case_number
        case = db.query(JobCase).filter(
            JobCase.job_id == uuid.UUID(job_id),
            JobCase.case_number == case_number,
        ).first()

        if case:
            case.status = CaseStatus.COMPLETED if result.get("success") else CaseStatus.FAILED
            case.layer1_output = result.get("layer1_output")
            case.layer2_output = result.get("layer2_output")
            case.layer3_output = result.get("layer3_output")
            case.final_verdict = result.get("final_verdict")
            case.final_cci = result.get("final_cci")
            case.total_duration_ms = result.get("total_duration_ms")
            case.total_tokens_input = result.get("total_tokens_input")
            case.total_tokens_output = result.get("total_tokens_output")
            case.error_message = result.get("error")
            case.prompt_version = settings.prompt_version
            case.model_version = settings.llm_model
            case.processed_at = datetime.utcnow()

            # Update job counts
            job = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
            if job:
                if result.get("success"):
                    job.completed_count = (job.completed_count or 0) + 1
                else:
                    job.failed_count = (job.failed_count or 0) + 1

            # Create audit log
            audit = AuditLog(
                job_id=uuid.UUID(job_id),
                case_id=case.id,
                action="case_processed",
                layer="pipeline",
                details={
                    "success": result.get("success"),
                    "verdict": result.get("final_verdict"),
                    "cci": result.get("final_cci"),
                },
                duration_ms=result.get("total_duration_ms"),
                tokens_input=result.get("total_tokens_input"),
                tokens_output=result.get("total_tokens_output"),
                prompt_version=settings.prompt_version,
                model_version=settings.llm_model,
            )
            db.add(audit)
            db.commit()

    except Exception as e:
        logger.error("save_case_result_failed", job_id=job_id, error=str(e))
        db.rollback()
    finally:
        db.close()


def update_job_status(job_id: str, status: JobStatus) -> None:
    """
    Update job status in the database.

    Args:
        job_id: UUID of the job
        status: New status
    """
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
        if job:
            job.status = status
            if status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                job.completed_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        logger.error("update_job_status_failed", job_id=job_id, error=str(e))
        db.rollback()
    finally:
        db.close()


@celery_app.task(bind=True, name="process_case")
def process_case(
    self,
    case_id: str,
    job_id: str,
    raw_text: str,
    case_number: int = 1,
) -> dict[str, Any]:
    """
    Process a single clinical case through the pipeline.

    Args:
        case_id: Unique identifier for this case
        job_id: Parent job ID
        raw_text: Raw clinical text to process
        case_number: Case number within the job

    Returns:
        Dict with processing results
    """
    import asyncio

    logger.info(
        "task_started",
        task_id=self.request.id,
        case_id=case_id,
        job_id=job_id,
        text_length=len(raw_text),
    )

    try:
        # Create LLM client and orchestrator
        llm_client = OllamaClient()
        orchestrator = PipelineOrchestrator(llm_client)

        # Run pipeline (async in sync context)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(orchestrator.execute(raw_text))
        finally:
            loop.close()

        logger.info(
            "task_completed",
            task_id=self.request.id,
            case_id=case_id,
            job_id=job_id,
            success=result.success,
            duration_ms=result.total_duration_ms,
        )

        result_dict = {
            "case_id": case_id,
            "case_number": case_number,
            "job_id": job_id,
            "success": result.success,
            "layer1_output": result.layer1_result.output if result.layer1_result else None,
            "layer2_output": result.layer2_result.output if result.layer2_result else None,
            "layer3_output": result.layer3_result.output if result.layer3_result else None,
            "final_verdict": result.final_verdict,
            "final_cci": result.final_cci,
            "total_duration_ms": result.total_duration_ms,
            "total_tokens_input": result.total_tokens_input,
            "total_tokens_output": result.total_tokens_output,
            "error": result.error,
            "processed_at": datetime.utcnow().isoformat() + "Z",
        }

        # Save result to database
        save_case_result(job_id, case_number, result_dict)

        return result_dict

    except Exception as e:
        logger.error(
            "task_failed",
            task_id=self.request.id,
            case_id=case_id,
            job_id=job_id,
            error=str(e),
        )

        error_result = {
            "case_id": case_id,
            "case_number": case_number,
            "job_id": job_id,
            "success": False,
            "error": str(e),
            "processed_at": datetime.utcnow().isoformat() + "Z",
        }

        # Save error to database
        save_case_result(job_id, case_number, error_result)

        return error_result


@celery_app.task(bind=True, name="process_batch")
def process_batch(
    self,
    job_id: str,
    cases: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Process a batch of cases.

    Args:
        job_id: Job ID for the batch
        cases: List of dicts with 'case_id' and 'text'

    Returns:
        Dict with batch processing status
    """
    logger.info(
        "batch_started",
        task_id=self.request.id,
        job_id=job_id,
        case_count=len(cases),
    )

    results = []

    for i, case in enumerate(cases):
        case_id = case.get("case_id", str(i + 1))
        text = case.get("text", "")
        case_number = i + 1

        # Update case status to processing
        db = SessionLocal()
        try:
            job_case = db.query(JobCase).filter(
                JobCase.job_id == uuid.UUID(job_id),
                JobCase.case_number == case_number,
            ).first()
            if job_case:
                job_case.status = CaseStatus.PROCESSING
                db.commit()
        finally:
            db.close()

        # Process each case
        result = process_case.apply(
            args=[case_id, job_id, text, case_number]
        ).get()

        results.append(result)

        # Update progress
        self.update_state(
            state="PROGRESS",
            meta={
                "job_id": job_id,
                "completed": i + 1,
                "total": len(cases),
                "current_case": case_id,
            },
        )

    # Determine final job status
    success_count = sum(1 for r in results if r.get("success"))
    final_status = JobStatus.COMPLETED if success_count == len(cases) else JobStatus.FAILED

    # Update job status
    update_job_status(job_id, final_status)

    logger.info(
        "batch_completed",
        task_id=self.request.id,
        job_id=job_id,
        case_count=len(cases),
        success_count=success_count,
    )

    return {
        "job_id": job_id,
        "status": final_status.value,
        "case_count": len(cases),
        "success_count": success_count,
        "results": results,
    }
