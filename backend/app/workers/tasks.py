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


def _classify_error(error_msg: str | None) -> str | None:
    """Classify an error message into a standard error type.

    Categories: LLM_ERROR, PARSE_ERROR, TIMEOUT, SCHEMA_ERROR, UNKNOWN
    Simple heuristic classification is acceptable per spec.
    """
    if not error_msg:
        return None
    e = error_msg.lower()
    if "timeout" in e or "timed out" in e:
        return "TIMEOUT"
    if "json" in e or "parse" in e or "decode" in e:
        return "PARSE_ERROR"
    if "schema" in e or "validation" in e or "required field" in e:
        return "SCHEMA_ERROR"
    if "llm" in e or "model" in e or "generate" in e or "ollama" in e or "vllm" in e or "connection" in e:
        return "LLM_ERROR"
    return "UNKNOWN"


def save_layer_metric(
    job_id: str,
    case_number: int,
    layer_id: str,
    duration_ms: int,
    success: bool,
    error_message: str | None = None,
) -> None:
    """Persist a single layer metric row. Called once per layer completion."""
    from app.db.models import LayerMetric
    db = SessionLocal()
    try:
        metric = LayerMetric(
            job_id=uuid.UUID(job_id),
            case_number=case_number,
            layer_id=layer_id,
            duration_ms=duration_ms,
            success=success,
            error_type=_classify_error(error_message),
            error_message=error_message[:500] if error_message else None,
        )
        db.add(metric)
        db.commit()
    except Exception as e:
        logger.warning("save_layer_metric_failed", layer_id=layer_id, error=str(e))
        db.rollback()
    finally:
        db.close()


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


def _get_runtime_temperature() -> float:
    """Get runtime temperature from Redis, falling back to settings.
    
    Called per case to ensure dynamically updated temperature
    is picked up immediately, even for queued jobs.
    """
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        temp = r.get("snapai:temperature")
        r.close()
        if temp is not None:
            return float(temp)
    except Exception:
        pass
    return settings.llm_temperature


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


def _build_pipeline_snapshot() -> list[dict]:
    """Build a deterministic snapshot of the current pipeline configuration.

    Combines built-in layers (with optional DB overrides) and custom layers,
    sorted by display_order.  This snapshot is frozen at job start so that
    prompt edits during execution cannot affect a running job.

    Returns:
        Sorted list of layer config dicts:
        [{layer_name, label, prompt, is_builtin, display_order}, ...]
    """
    from app.db.models import PromptTemplate, PromptVersion
    from app.api.routes.prompts import BUILTIN_LAYERS, LAYER_LABELS, _load_default_prompt

    db = SessionLocal()
    try:
        layers: list[dict] = []

        # Built-in layers
        for idx, name in enumerate(BUILTIN_LAYERS):
            template = db.query(PromptTemplate).filter(
                PromptTemplate.layer_name == name,
                PromptTemplate.is_active == True,
            ).first()

            if template:
                content = template.content
                if template.active_version_id:
                    version = db.query(PromptVersion).filter(
                        PromptVersion.id == template.active_version_id
                    ).first()
                    if version:
                        content = version.content

                layers.append({
                    "layer_name": name,
                    "label": template.label or LAYER_LABELS.get(name, name),
                    "prompt": content,
                    "is_builtin": True,
                    "display_order": template.display_order if template.display_order != 99 else idx,
                })
            else:
                layers.append({
                    "layer_name": name,
                    "label": LAYER_LABELS.get(name, name),
                    "prompt": _load_default_prompt(name),
                    "is_builtin": True,
                    "display_order": idx,
                })

        # Custom layers
        custom_templates = db.query(PromptTemplate).filter(
            PromptTemplate.is_active == True,
            PromptTemplate.layer_name.notin_(BUILTIN_LAYERS),
        ).order_by(PromptTemplate.display_order).all()

        for tmpl in custom_templates:
            content = tmpl.content
            if tmpl.active_version_id:
                version = db.query(PromptVersion).filter(
                    PromptVersion.id == tmpl.active_version_id
                ).first()
                if version:
                    content = version.content

            layers.append({
                "layer_name": tmpl.layer_name,
                "label": tmpl.label or tmpl.layer_name,
                "prompt": content,
                "is_builtin": False,
                "display_order": tmpl.display_order,
            })

        # Sort by display_order for deterministic execution
        layers.sort(key=lambda l: l["display_order"])
        return layers
    except Exception:
        logger.warning("pipeline_snapshot_build_failed", exc_info=True)
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
                    "temperature_used": result.get("temperature_used"),
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
            if status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
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
    loop: asyncio.AbstractEventLoop = None,
    pipeline_layers: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Core implementation for processing a single clinical case.
    
    This is called directly from process_batch (not as a Celery subtask)
    to avoid the "Never call result.get() within a task" error.
    
    Args:
        loop: Optional event loop to reuse. If None, creates and closes one.
              When called from process_batch, pass a shared loop.
        pipeline_layers: Frozen pipeline snapshot. When provided, the dynamic
              execution path is used. When None, legacy path is used.
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

    # Determine if we own the loop (and should close it)
    owns_loop = loop is None
    if owns_loop:
        loop = _get_or_create_event_loop()

    try:
        # Get active model, custom prompts, and runtime temperature — all per case
        active_model = _get_active_model()
        runtime_temperature = _get_runtime_temperature()

        # Build label lookup from snapshot (for streaming events)
        layer_labels: dict[str, str] = {}
        if pipeline_layers:
            layer_labels = {
                l["layer_name"]: l.get("label", l["layer_name"]) for l in pipeline_layers
            }

        # Legacy params — only needed when no snapshot
        custom_prompts = None
        extra_layers = None
        if not pipeline_layers:
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
                publisher.publish_token(
                    job_id, case_number, layer, token,
                    label=layer_labels.get(layer),
                )
            return on_token

        async def on_layer_start(layer: str):
            publisher.publish_layer_start(
                job_id, case_number, layer,
                label=layer_labels.get(layer),
            )

        async def on_layer_complete(layer: str, success: bool, duration_ms: int, error_message: str | None = None):
            publisher.publish_layer_complete(
                job_id, case_number, layer, success, duration_ms,
                label=layer_labels.get(layer),
                error_message=error_message,
            )
            # Persist metric (one write per layer, not per token)
            save_layer_metric(
                job_id=job_id,
                case_number=case_number,
                layer_id=layer,
                duration_ms=duration_ms,
                success=success,
                error_message=error_message,
            )

        # Run pipeline with streaming and per-case temperature
        result = loop.run_until_complete(
            orchestrator.execute(
                raw_text=raw_text,
                pipeline_layers=pipeline_layers,
                custom_prompts=custom_prompts if custom_prompts else None,
                extra_layers=extra_layers if extra_layers else None,
                on_token=make_token_callback(),
                on_layer_start=on_layer_start,
                on_layer_complete=on_layer_complete,
                temperature=runtime_temperature,
            )
        )

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
            "temperature_used": runtime_temperature,
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
        if owns_loop:
            loop.close()


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
        "JOB_STARTED",
        task_id=self.request.id,
        job_id=job_id,
        case_count=len(cases),
    )

    results = []

    # Create a single event loop for the entire batch
    loop = _get_or_create_event_loop()

    # Explicitly mark job as PROCESSING so DB reflects correct state FIRST
    # (before anything else can fail)
    update_job_status(job_id, JobStatus.PROCESSING)

    # Create publisher for job-level events (optional — SSE streaming only)
    from app.utils.stream_manager import StreamPublisher
    try:
        job_publisher = StreamPublisher()
    except Exception as pub_err:
        logger.warning(
            "stream_publisher_unavailable",
            job_id=job_id,
            error=str(pub_err),
        )
        job_publisher = None

    # ── Build pipeline snapshot (frozen at job start) ──
    # For replay jobs, the snapshot is already set on the Job record — use it directly.
    # IMPORTANT: initialise to None BEFORE the try so the variable is always bound.
    pipeline_snapshot = None
    db = SessionLocal()
    try:
        job_start = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
        if job_start and job_start.pipeline_snapshot:
            # Replay path: use the pre-set snapshot
            pipeline_snapshot = job_start.pipeline_snapshot
            logger.info(
                "PIPELINE_SNAPSHOT_REPLAY",
                job_id=job_id,
                layer_count=len(pipeline_snapshot),
            )
        else:
            # Normal path: build a fresh snapshot
            pipeline_snapshot = _build_pipeline_snapshot()
            if job_start:
                job_start.pipeline_snapshot = pipeline_snapshot
            logger.info(
                "PIPELINE_SNAPSHOT",
                job_id=job_id,
                layer_count=len(pipeline_snapshot),
                layers=[l["layer_name"] for l in pipeline_snapshot],
            )
        # Always set started_at and model_name
        if job_start:
            job_start.started_at = datetime.utcnow()
            if not job_start.model_name:
                job_start.model_name = _get_active_model()
            db.commit()
    except Exception as e:
        logger.error(
            "PIPELINE_SNAPSHOT_ERROR",
            job_id=job_id,
            error=str(e),
            exc_info=True,
        )
    finally:
        db.close()

    # Fallback: if snapshot building failed (DB error, etc.), build without DB persistence
    if pipeline_snapshot is None:
        logger.warning(
            "PIPELINE_SNAPSHOT_FALLBACK",
            job_id=job_id,
        )
        pipeline_snapshot = _build_pipeline_snapshot()

    try:
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

            # Process each case — reuse the shared event loop + frozen snapshot
            result = _process_case_impl(
                case_id, job_id, text, case_number,
                loop=loop,
                pipeline_layers=pipeline_snapshot if pipeline_snapshot else None,
            )

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
    except Exception as e:
        # Emit JOB_FAILED SSE so frontend exits processing immediately
        logger.error(
            "JOB_FAILED",
            task_id=self.request.id,
            job_id=job_id,
            error=str(e),
            exc_info=True,
        )
        try:
            if job_publisher:
                job_publisher.publish_job_failed(job_id, str(e))
        except Exception:
            pass
        update_job_status(job_id, JobStatus.FAILED)
        try:
            if job_publisher:
                job_publisher.close()
        except Exception:
            pass
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass
        raise
    finally:
        # Only close if not already closed in the except block
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass

    # Determine final job status
    success_count = sum(1 for r in results if r.get("success"))
    final_status = JobStatus.COMPLETED if success_count == len(cases) else JobStatus.FAILED

    # Update job status
    update_job_status(job_id, final_status)

    # Emit job completion SSE event
    try:
        if job_publisher:
            job_publisher.publish_job_complete(job_id, len(cases), success_count)
            job_publisher.close()
    except Exception:
        pass

    lifecycle_event = "JOB_COMPLETED" if final_status == JobStatus.COMPLETED else "JOB_FAILED"
    logger.info(
        lifecycle_event,
        task_id=self.request.id,
        job_id=job_id,
        case_count=len(cases),
        success_count=success_count,
        status=final_status.value,
    )

    return {
        "job_id": job_id,
        "status": final_status.value,
        "case_count": len(cases),
        "success_count": success_count,
        "results": results,
    }

