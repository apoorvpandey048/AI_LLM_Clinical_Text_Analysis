"""
Celery tasks for SNAP-AI pipeline processing.

Tasks persist results to PostgreSQL after processing.
Supports streaming token output via Redis pub/sub.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any

from app.workers.celery_app import celery_app
from app.config import get_settings
from app.llm import get_llm_client, OllamaClient, VLLMClient
from app.pipeline import PipelineOrchestrator
from app.db.session import SessionLocal
from app.db.models import Job, JobCase, AuditLog, JobStatus, CaseStatus
from app.utils import get_logger

logger = get_logger(__name__)
settings = get_settings()


def _get_or_create_event_loop():
    """Get the current event loop or create a new one for the thread."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def _get_active_model() -> str:
    """Get the currently active model from Redis or settings."""
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        active = r.get("snapai:active_model")
        r.close()
        if active:
            return active
    except Exception:
        pass
    return settings.llm_model


def _get_custom_prompts() -> dict[str, str]:
    """Get custom prompts for BUILT-IN layers from the database.
    
    Respects active_version_id: if a template has an active version selected,
    that version's content is returned instead of the template's current content.
    """
    db = SessionLocal()
    try:
        from app.db.models import PromptTemplate, PromptVersion
        builtin_names = ["layer1_ctp", "layer2_cie", "layer3_ccc"]
        templates = db.query(PromptTemplate).filter(
            PromptTemplate.is_active == True,
            PromptTemplate.layer_name.in_(builtin_names),
        ).all()
        prompts = {}
        for t in templates:
            if t.active_version_id:
                version = db.query(PromptVersion).filter(
                    PromptVersion.id == t.active_version_id
                ).first()
                if version:
                    prompts[t.layer_name] = version.content
                    continue
            prompts[t.layer_name] = t.content
        return prompts
    except Exception:
        return {}
    finally:
        db.close()


def _get_extra_layers() -> list[dict]:
    """Get custom (non-built-in) layer configs for the pipeline.
    
    Returns a list of dicts with 'layer_name' and 'prompt' keys,
    ordered by display_order.
    """
    db = SessionLocal()
    try:
        from app.db.models import PromptTemplate, PromptVersion
        builtin_names = ["layer1_ctp", "layer2_cie", "layer3_ccc"]
        templates = db.query(PromptTemplate).filter(
            PromptTemplate.is_active == True,
            PromptTemplate.layer_name.notin_(builtin_names),
        ).order_by(PromptTemplate.display_order).all()
        
        layers = []
        for t in templates:
            content = t.content
            if t.active_version_id:
                version = db.query(PromptVersion).filter(
                    PromptVersion.id == t.active_version_id
                ).first()
                if version:
                    content = version.content
            layers.append({
                "layer_name": t.layer_name,
                "prompt": content,
            })
        return layers
    except Exception:
        return []
    finally:
        db.close()


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
        case = db.query(JobCase).filter(
            JobCase.job_id == uuid.UUID(job_id),
            JobCase.case_number == case_number,
        ).first()

        if case:
            case.status = CaseStatus.COMPLETED if result.get("success") else CaseStatus.FAILED
            case.layer1_output = result.get("layer1_output")
            case.layer2_output = result.get("layer2_output")
            case.layer3_output = result.get("layer3_output")
            # Save raw LLM outputs for review
            case.layer1_raw_output = result.get("layer1_raw_output")
            case.layer2_raw_output = result.get("layer2_raw_output")
            case.layer3_raw_output = result.get("layer3_raw_output")
            # Extra custom layer outputs
            case.extra_layer_outputs = result.get("extra_layer_outputs")
            case.extra_layer_raw_outputs = result.get("extra_layer_raw_outputs")
            case.final_verdict = result.get("final_verdict")
            case.final_cci = result.get("final_cci")
            case.total_duration_ms = result.get("total_duration_ms")
            case.total_tokens_input = result.get("total_tokens_input")
            case.total_tokens_output = result.get("total_tokens_output")
            case.error_message = result.get("error")
            case.prompt_version = settings.prompt_version
            case.model_version = result.get("model_used", settings.llm_model)
            case.processed_at = datetime.utcnow()

            job = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
            if job:
                if result.get("success"):
                    job.completed_count = (job.completed_count or 0) + 1
                else:
                    job.failed_count = (job.failed_count or 0) + 1

            audit = AuditLog(
                job_id=uuid.UUID(job_id),
                case_id=case.id,
                action="case_processed",
                layer="pipeline",
                details={
                    "success": result.get("success"),
                    "verdict": result.get("final_verdict"),
                    "cci": result.get("final_cci"),
                    "model_used": result.get("model_used"),
                },
                duration_ms=result.get("total_duration_ms"),
                tokens_input=result.get("total_tokens_input"),
                tokens_output=result.get("total_tokens_output"),
                prompt_version=settings.prompt_version,
                model_version=result.get("model_used", settings.llm_model),
            )
            db.add(audit)
            db.commit()

    except Exception as e:
        logger.error("save_case_result_failed", job_id=job_id, error=str(e))
        db.rollback()
    finally:
        db.close()


def update_job_status(job_id: str, status: JobStatus) -> None:
    """Update job status in the database."""
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


def _process_case_impl(
    case_id: str,
    job_id: str,
    raw_text: str,
    case_number: int = 1,
    task_id: str = None,
) -> dict[str, Any]:
    """
    Core implementation for processing a single clinical case.
    
    This is called directly from process_batch (not as a Celery subtask)
    to avoid the "Never call result.get() within a task" error.
    """
    from app.utils.stream_manager import StreamPublisher

    logger.info(
        "task_started",
        task_id=task_id,
        case_id=case_id,
        job_id=job_id,
        text_length=len(raw_text),
    )

    # Initialize streaming publisher
    publisher = StreamPublisher()

    try:
        # Get active model and custom prompts
        active_model = _get_active_model()
        custom_prompts = _get_custom_prompts()
        extra_layers = _get_extra_layers()

        # Create LLM client via factory (respects llm_backend setting)
        llm_client = get_llm_client()
        # Override model if a specific one was selected in Redis
        if hasattr(llm_client, 'model'):
            llm_client.model = active_model
        orchestrator = PipelineOrchestrator(llm_client)

        # Create streaming callbacks
        def make_token_callback():
            """Create sync wrapper for token publishing."""
            async def on_token(layer: str, token: str):
                publisher.publish_token(job_id, case_number, layer, token)
            return on_token

        async def on_layer_start(layer: str):
            publisher.publish_layer_start(job_id, case_number, layer)

        async def on_layer_complete(layer: str, success: bool, duration_ms: int):
            publisher.publish_layer_complete(job_id, case_number, layer, success, duration_ms)

        # Run pipeline with streaming
        loop = _get_or_create_event_loop()
        try:
            result = loop.run_until_complete(
                orchestrator.execute(
                    raw_text=raw_text,
                    custom_prompts=custom_prompts if custom_prompts else None,
                    extra_layers=extra_layers if extra_layers else None,
                    on_token=make_token_callback(),
                    on_layer_start=on_layer_start,
                    on_layer_complete=on_layer_complete,
                )
            )
        finally:
            loop.close()

        logger.info(
            "task_completed",
            task_id=task_id,
            case_id=case_id,
            job_id=job_id,
            success=result.success,
            duration_ms=result.total_duration_ms,
            model_used=active_model,
        )

        result_dict = {
            "case_id": case_id,
            "case_number": case_number,
            "job_id": job_id,
            "success": result.success,
            "layer1_output": result.layer1_result.output if result.layer1_result else None,
            "layer2_output": result.layer2_result.output if result.layer2_result else None,
            "layer3_output": result.layer3_result.output if result.layer3_result else None,
            # Raw LLM outputs for review
            "layer1_raw_output": result.layer1_result.raw_response if result.layer1_result else None,
            "layer2_raw_output": result.layer2_result.raw_response if result.layer2_result else None,
            "layer3_raw_output": result.layer3_result.raw_response if result.layer3_result else None,
            # Extra custom layer outputs
            "extra_layer_outputs": {
                name: lr.output for name, lr in result.extra_layer_results.items()
            } if result.extra_layer_results else None,
            "extra_layer_raw_outputs": {
                name: lr.raw_response for name, lr in result.extra_layer_results.items()
            } if result.extra_layer_results else None,
            "final_verdict": result.final_verdict,
            "final_cci": result.final_cci,
            "total_duration_ms": result.total_duration_ms,
            "total_tokens_input": result.total_tokens_input,
            "total_tokens_output": result.total_tokens_output,
            "error": result.error,
            "model_used": active_model,
            "processed_at": datetime.utcnow().isoformat() + "Z",
        }

        # Publish case completion
        publisher.publish_case_complete(
            job_id, case_number, result.success,
            result.final_verdict, result.final_cci,
        )

        # Save result to database
        save_case_result(job_id, case_number, result_dict)

        return result_dict

    except Exception as e:
        logger.error(
            "task_failed",
            task_id=task_id,
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

        save_case_result(job_id, case_number, error_result)
        return error_result
    finally:
        publisher.close()


@celery_app.task(bind=True, name="process_case")
def process_case(
    self,
    case_id: str,
    job_id: str,
    raw_text: str,
    case_number: int = 1,
) -> dict[str, Any]:
    """
    Process a single clinical case through the pipeline with streaming.

    This Celery task wraps the implementation function.
    For batch processing, use _process_case_impl directly instead.

    Args:
        case_id: Unique identifier for this case
        job_id: Parent job ID
        raw_text: Raw clinical text to process
        case_number: Case number within the job

    Returns:
        Dict with processing results
    """
    return _process_case_impl(
        case_id=case_id,
        job_id=job_id,
        raw_text=raw_text,
        case_number=case_number,
        task_id=self.request.id,
    )



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
        case_number = case.get("case_number", i + 1)

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

        # Process each case - call the function directly, not as a subtask
        # (Celery doesn't allow calling .get() inside a task)
        result = _process_case_impl(case_id, job_id, text, case_number)

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
