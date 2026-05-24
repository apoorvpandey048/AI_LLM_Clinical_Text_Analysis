"""
Celery tasks for SNAP-AI pipeline processing.

Tasks persist results to PostgreSQL after processing.
Supports streaming token output via Redis pub/sub.
"""

import asyncio
import os
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
from app.utils.json_utils import safe_json_parse

logger = get_logger(__name__)
settings = get_settings()


class StreamAccumulator:
    """Captures per-layer stream buffers during pipeline execution.

    Sits alongside the existing StreamPublisher — every token that is
    forwarded to the UI via Redis pub/sub is ALSO appended here so we
    can persist the full raw stream for download.
    """

    def __init__(self):
        self.buffers: dict[str, str] = {}      # layer_name -> accumulated text
        self.start_times: dict[str, str] = {}  # layer_name -> ISO timestamp
        self.end_times: dict[str, str] = {}    # layer_name -> ISO timestamp

    def append(self, layer: str, token: str) -> None:
        """Append a single token to the specified layer buffer."""
        if layer not in self.buffers:
            self.buffers[layer] = ""
            self.start_times[layer] = datetime.utcnow().isoformat() + "Z"
        self.buffers[layer] += token

    def finalize_layer(self, layer: str) -> None:
        """Mark a layer as complete (records end timestamp)."""
        self.end_times[layer] = datetime.utcnow().isoformat() + "Z"

    def get_layer_data(self) -> dict:
        """Build per-layer stream data bundles for DB storage.

        Returns:
            Dict keyed by layer name, each containing:
            - raw_stream: exact accumulated text (what the UI showed)
            - assembled_text: same as raw_stream (for clarity)
            - parsed_json: best-effort JSON parse, or None
            - start_time: ISO timestamp of first token
            - end_time: ISO timestamp of layer completion
        """
        result = {}
        for layer, raw_stream in self.buffers.items():
            result[layer] = {
                "raw_stream": raw_stream,
                "assembled_text": raw_stream,
                "parsed_json": safe_json_parse(raw_stream),
                "start_time": self.start_times.get(layer),
                "end_time": self.end_times.get(layer),
            }
        return result


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


def _get_runtime_seed() -> int | None:
    """Get runtime seed from Redis, falling back to settings.
    
    Called per case to ensure dynamically updated seed
    is picked up immediately, even for queued jobs.
    Returns None when no seed is set (non-deterministic).
    """
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        seed = r.get("snapai:seed")
        r.close()
        if seed is not None:
            if seed == "" or seed.lower() == "none":
                return None
            return int(seed)
    except Exception:
        pass
    return settings.llm_seed


def _get_execution_mode() -> str:
    """Get unified execution mode from Redis.

    Returns one of: 'independent', 'chained', 'comparison'.
    Default: 'independent'.
    """
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        # Primary: unified key
        val = r.get("snapai:execution_mode")
        if val and val in ("independent", "chained", "comparison"):
            r.close()
            return val
        # Backward compat: check old dual-flag keys
        comp_val = r.get("snapai:comparison_mode")
        cm_val = r.get("snapai:chained_mode")
        r.close()
        if comp_val and comp_val.lower() in ("true", "1", "yes"):
            return "comparison"
        if cm_val and cm_val.lower() in ("true", "1", "yes"):
            return "chained"
    except Exception:
        pass
    return "independent"


def _compute_comparison(independent: dict, chained: dict) -> dict:
    """Compute a structured diff between independent and chained results.

    Returns a comparison dict with CCI delta, grade differences,
    and verdict change indicator.
    """
    ind_cci = independent.get("final_cci") or 0
    chn_cci = chained.get("final_cci") or 0

    ind_verdict = independent.get("final_verdict", "")
    chn_verdict = chained.get("final_verdict", "")

    # Grade-level comparison
    ind_complications = (independent.get("layer2_output") or {}).get("complications", [])
    chn_complications = (chained.get("layer2_output") or {}).get("complications", [])

    ind_grades = sorted([c.get("cd_grade", "?") for c in ind_complications])
    chn_grades = sorted([c.get("cd_grade", "?") for c in chn_complications])

    return {
        "cci_delta": round(chn_cci - ind_cci, 2),
        "cci_independent": ind_cci,
        "cci_chained": chn_cci,
        "grade_difference": ind_grades != chn_grades,
        "grades_independent": ind_grades,
        "grades_chained": chn_grades,
        "verdict_changed": ind_verdict != chn_verdict,
        "verdict_independent": ind_verdict,
        "verdict_chained": chn_verdict,
        "complication_count_independent": len(ind_complications),
        "complication_count_chained": len(chn_complications),
    }

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
                version_label = template.version or "custom"
                if template.active_version_id:
                    version = db.query(PromptVersion).filter(
                        PromptVersion.id == template.active_version_id
                    ).first()
                    if version:
                        content = version.content
                        version_label = version.version_label or version_label

                layers.append({
                    "layer_name": name,
                    "label": template.label or LAYER_LABELS.get(name, name),
                    "prompt": content,
                    "is_builtin": True,
                    "display_order": template.display_order if template.display_order != 99 else idx,
                    "prompt_version": version_label,
                })
            else:
                layers.append({
                    "layer_name": name,
                    "label": LAYER_LABELS.get(name, name),
                    "prompt": _load_default_prompt(name),
                    "is_builtin": True,
                    "display_order": idx,
                    "prompt_version": settings.prompt_version,
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

            version_label = tmpl.version or "custom"
            if tmpl.active_version_id:
                ver = db.query(PromptVersion).filter(
                    PromptVersion.id == tmpl.active_version_id
                ).first()
                if ver and ver.version_label:
                    version_label = ver.version_label

            layers.append({
                "layer_name": tmpl.layer_name,
                "label": tmpl.label or tmpl.layer_name,
                "prompt": content,
                "is_builtin": False,
                "display_order": tmpl.display_order,
                "prompt_version": version_label,
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
            # Per-layer stream capture (full raw streams for download)
            case.layer_stream_data = result.get("layer_stream_data")
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
                    "seed_used": result.get("seed_used"),
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
            if status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.STOPPING]:
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
    source_file: str | None = None,
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
        runtime_seed = _get_runtime_seed()
        execution_mode = _get_execution_mode()  # "independent" | "chained" | "comparison"
        chained_mode = execution_mode in ("chained", "comparison")
        comparison_mode = execution_mode == "comparison"

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
        default_client = get_llm_client()
        # Override model if a specific one was selected in Redis
        if hasattr(default_client, 'model'):
            default_client.model = active_model

        # Check Redis for OpenRouter key
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        or_key = r.get(f"openrouter:{job_id}")
        r.close()

        if or_key:
            from app.llm.openrouter_client import OpenRouterClient
            from app.llm.fallback_client import FallbackLLMClient
            # Initialize OpenRouter Client and wrap with fallback
            or_client = OpenRouterClient(api_key=or_key, model="openai/gpt-oss-120b")
            llm_client = FallbackLLMClient(primary=or_client, fallback=default_client)
            active_model = "openai/gpt-oss-120b (OpenRouter)" # for logging
        else:
            llm_client = default_client

        orchestrator = PipelineOrchestrator(llm_client)

        # ── Stream accumulator: captures per-layer raw streams for download ──
        accumulator = StreamAccumulator()

        # Create streaming callbacks
        def make_token_callback():
            """Create sync wrapper for token publishing + accumulation."""
            async def on_token(layer: str, token: str):
                # Publish to UI via Redis (existing behavior)
                publisher.publish_token(
                    job_id, case_number, layer, token,
                    label=layer_labels.get(layer),
                )
                # Accumulate for per-layer download (new)
                accumulator.append(layer, token)
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
            # Finalize layer in stream accumulator
            accumulator.finalize_layer(layer)
            # Persist metric (one write per layer, not per token)
            save_layer_metric(
                job_id=job_id,
                case_number=case_number,
                layer_id=layer,
                duration_ms=duration_ms,
                success=success,
                error_message=error_message,
            )

        # ── Helper to build a result_dict from a PipelineResult ──
        def _result_to_dict(result, mode_label: str) -> dict:
            return {
                "case_id": case_id,
                "case_number": case_number,
                "job_id": job_id,
                "success": result.success,
                "layer1_output": result.layer1_result.output if result.layer1_result else None,
                "layer2_output": result.layer2_result.output if result.layer2_result else None,
                "layer3_output": result.layer3_result.output if result.layer3_result else None,
                "layer1_raw_output": result.layer1_result.raw_response if result.layer1_result else None,
                "layer2_raw_output": result.layer2_result.raw_response if result.layer2_result else None,
                "layer3_raw_output": result.layer3_result.raw_response if result.layer3_result else None,
                "extra_layer_outputs": {
                    name: lr.output for name, lr in result.extra_layer_results.items()
                } if result.extra_layer_results else None,
                "extra_layer_raw_outputs": {
                    name: lr.raw_response for name, lr in result.extra_layer_results.items()
                } if result.extra_layer_results else None,
                "layer_stream_data": accumulator.get_layer_data(),
                "final_verdict": result.final_verdict,
                "final_cci": result.final_cci,
                "total_duration_ms": result.total_duration_ms,
                "total_tokens_input": result.total_tokens_input,
                "total_tokens_output": result.total_tokens_output,
                "error": result.error,
                "model_used": active_model,
                "temperature_used": runtime_temperature,
                "seed_used": runtime_seed,
                "execution_mode": mode_label,
                "processed_at": datetime.utcnow().isoformat() + "Z",
            }

        # =============================================
        # COMPARISON MODE — run both pipelines
        # =============================================
        if comparison_mode:
            logger.info("PIPELINE-COMPARISON-START", case_id=case_id, job_id=job_id)

            # Pass 1: Independent (no chaining, no streaming — silent)
            logger.info("PIPELINE-INDEPENDENT-START", case_id=case_id)
            try:
                ind_result = loop.run_until_complete(
                    orchestrator.execute(
                        raw_text=raw_text,
                        pipeline_layers=pipeline_layers,
                        custom_prompts=custom_prompts if custom_prompts else None,
                        extra_layers=extra_layers if extra_layers else None,
                        on_token=None,  # no streaming for independent pass
                        on_layer_start=None,
                        on_layer_complete=None,
                        temperature=runtime_temperature,
                        seed=runtime_seed,
                        chained_mode=False,
                    )
                )
                ind_dict = _result_to_dict(ind_result, "independent")
                logger.info("PIPELINE-INDEPENDENT-END", case_id=case_id, success=ind_result.success)
            except Exception as e:
                logger.error("PIPELINE-INDEPENDENT-FAILED", case_id=case_id, error=str(e))
                ind_dict = {
                    "success": False, "error": str(e), "execution_mode": "independent",
                    "final_verdict": None, "final_cci": None,
                    "layer2_output": None, "layer3_output": None,
                }

            # Pass 2: Chained (with streaming)
            logger.info("PIPELINE-CHAINED-START", case_id=case_id)
            try:
                chn_result = loop.run_until_complete(
                    orchestrator.execute(
                        raw_text=raw_text,
                        pipeline_layers=pipeline_layers,
                        custom_prompts=custom_prompts if custom_prompts else None,
                        extra_layers=extra_layers if extra_layers else None,
                        on_token=make_token_callback(),
                        on_layer_start=on_layer_start,
                        on_layer_complete=on_layer_complete,
                        temperature=runtime_temperature,
                        seed=runtime_seed,
                        chained_mode=True,
                    )
                )
                chn_dict = _result_to_dict(chn_result, "chained")
                logger.info("PIPELINE-CHAINED-END", case_id=case_id, success=chn_result.success)
            except Exception as e:
                logger.error("PIPELINE-CHAINED-FAILED", case_id=case_id, error=str(e))
                chn_dict = {
                    "success": False, "error": str(e), "execution_mode": "chained",
                    "final_verdict": None, "final_cci": None,
                    "layer2_output": None, "layer3_output": None,
                }

            # Compute comparison diff
            comparison_diff = _compute_comparison(ind_dict, chn_dict)

            # Build merged result — primary display uses chained results
            result_dict = chn_dict.copy()
            result_dict["execution_mode"] = "comparison"
            result_dict["independent"] = ind_dict
            result_dict["chained"] = chn_dict
            result_dict["comparison"] = comparison_diff

            # Store comparison data inside extra_layer_outputs so save_case_result
            # persists it (no schema change needed — reuses existing JSON column)
            existing_extras = result_dict.get("extra_layer_outputs") or {}
            existing_extras["__comparison_data__"] = {
                "execution_mode": "comparison",
                "independent": {
                    "layer1_output": ind_dict.get("layer1_output"),
                    "layer2_output": ind_dict.get("layer2_output"),
                    "layer3_output": ind_dict.get("layer3_output"),
                    "final_verdict": ind_dict.get("final_verdict"),
                    "final_cci": ind_dict.get("final_cci"),
                },
                "chained": {
                    "layer1_output": chn_dict.get("layer1_output"),
                    "layer2_output": chn_dict.get("layer2_output"),
                    "layer3_output": chn_dict.get("layer3_output"),
                    "final_verdict": chn_dict.get("final_verdict"),
                    "final_cci": chn_dict.get("final_cci"),
                },
                "comparison": comparison_diff,
            }
            result_dict["extra_layer_outputs"] = existing_extras

            # Use the chained result's success / verdict / cci as primary
            result_dict["success"] = chn_dict.get("success", False) or ind_dict.get("success", False)

            logger.info(
                "PIPELINE-COMPARISON-END",
                case_id=case_id,
                cci_delta=comparison_diff.get("cci_delta"),
                verdict_changed=comparison_diff.get("verdict_changed"),
                grade_difference=comparison_diff.get("grade_difference"),
            )

        # =============================================
        # SINGLE MODE — independent or chained
        # =============================================
        else:
            effective_mode = chained_mode
            mode_label = "chained" if effective_mode else "independent"
            logger.info(f"PIPELINE-{mode_label.upper()}-START", case_id=case_id)

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
                    seed=runtime_seed,
                    chained_mode=effective_mode,
                )
            )

            logger.info(
                f"PIPELINE-{mode_label.upper()}-END",
                case_id=case_id,
                success=result.success,
                duration_ms=result.total_duration_ms,
            )

            result_dict = _result_to_dict(result, mode_label)

        logger.info(
            "task_completed",
            task_id=task_id,
            case_id=case_id,
            job_id=job_id,
            success=result_dict.get("success"),
            execution_mode=result_dict.get("execution_mode"),
            model_used=active_model,
        )

        # Publish case completion
        publisher.publish_case_complete(
            job_id, case_number, result_dict.get("success", False),
            result_dict.get("final_verdict"), result_dict.get("final_cci"),
            source_file=source_file,
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



# =====================================================================================
# Per-case fan-out architecture (replaces the monolithic process_batch loop).
#
#   process_batch(shim) ─► _start_job_impl ─► N × process_one_case ─► finalize_job
#
# Each process_one_case is a short, idempotent, retryable unit of recovery. Results are
# persisted to PostgreSQL (not the Redis result backend). The orchestrator/pipeline and the
# per-case persistence (_process_case_impl / save_case_result) are reused UNCHANGED.
# =====================================================================================


def _fanout_enabled() -> bool:
    """Kill-switch for per-case fan-out. Default ON. Set BATCH_FANOUT_ENABLED=0/false to fall
    back to the legacy sequential process_batch (rollback path; IMPLEMENTATION_PLAN §4)."""
    val = os.getenv("BATCH_FANOUT_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


_RETRYABLE_ERROR_TYPES = {"LLM_ERROR", "TIMEOUT"}


def _is_retryable(error_msg: str | None) -> bool:
    """Transient failures worth retrying (LLM/connection/timeout). Deterministic parse/schema
    errors are not retried (a retry would just fail the same way)."""
    return _classify_error(error_msg) in _RETRYABLE_ERROR_TYPES


def _retry_backoff_seconds(retries: int) -> int:
    """Exponential backoff: 30s, 60s, capped at 120s."""
    return min(30 * (2 ** retries), 120)


def _count_cases_by_status(db, job_uuid, statuses) -> int:
    return db.query(JobCase).filter(
        JobCase.job_id == job_uuid,
        JobCase.status.in_(statuses),
    ).count()


def _recompute_job_counts(job_id: str) -> None:
    """Set Job.completed_count / failed_count to the TRUE values from JobCase rows.

    Authoritative, idempotent count source — unlike save_case_result's incremental +1, this
    is safe under retries / resume / redelivery (RISK_REGISTER N-3).
    """
    db = SessionLocal()
    try:
        job_uuid = uuid.UUID(job_id)
        job = db.query(Job).filter(Job.id == job_uuid).first()
        if not job:
            return
        job.completed_count = _count_cases_by_status(db, job_uuid, [CaseStatus.COMPLETED])
        job.failed_count = _count_cases_by_status(db, job_uuid, [CaseStatus.FAILED])
        db.commit()
    except Exception as e:
        logger.warning("recompute_job_counts_failed", job_id=job_id, error=str(e))
        db.rollback()
    finally:
        db.close()


def _ensure_snapshot(job_id: str) -> list[dict]:
    """Freeze (or reuse) the pipeline snapshot on the Job. Replay jobs reuse the stored
    snapshot; fresh jobs build + persist one. Mirrors the legacy snapshot handling."""
    snapshot = None
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
        if job and job.pipeline_snapshot:
            snapshot = job.pipeline_snapshot
            logger.info("PIPELINE_SNAPSHOT_REUSE", job_id=job_id, layer_count=len(snapshot))
        else:
            snapshot = _build_pipeline_snapshot()
            if job:
                job.pipeline_snapshot = snapshot
            logger.info("PIPELINE_SNAPSHOT", job_id=job_id, layer_count=len(snapshot))
        if job:
            if not job.started_at:
                job.started_at = datetime.utcnow()
            if not job.model_name:
                job.model_name = _get_active_model()
            db.commit()
    except Exception as e:
        logger.error("PIPELINE_SNAPSHOT_ERROR", job_id=job_id, error=str(e), exc_info=True)
    finally:
        db.close()
    if snapshot is None:
        snapshot = _build_pipeline_snapshot()
    return snapshot


def _start_job_impl(job_id: str) -> dict[str, Any]:
    """Coordinator logic: freeze snapshot, fan out one process_one_case per unfinished case.

    Enqueues cases in QUEUED / FAILED / PROCESSING (PROCESSING = stale from a crashed worker).
    COMPLETED cases are never re-enqueued, and process_one_case skips them anyway, so a stray
    re-enqueue is harmless. This is the SAME path used by /resume.
    """
    update_job_status(job_id, JobStatus.PROCESSING)
    _ensure_snapshot(job_id)

    db = SessionLocal()
    try:
        job_uuid = uuid.UUID(job_id)
        cases = db.query(JobCase).filter(
            JobCase.job_id == job_uuid,
            JobCase.status.in_([CaseStatus.QUEUED, CaseStatus.FAILED, CaseStatus.PROCESSING]),
        ).order_by(JobCase.case_number).all()
        to_enqueue = [(str(c.id), c.case_number) for c in cases]
    finally:
        db.close()

    for case_id, case_number in to_enqueue:
        process_one_case.apply_async(args=[job_id, case_id, case_number], queue="cases")

    logger.info("JOB_FANOUT_ENQUEUED", job_id=job_id, enqueued=len(to_enqueue))

    if not to_enqueue:
        # Nothing to do (e.g. resume of an already-finished job) — finalize immediately.
        finalize_job.apply_async(args=[job_id], queue="celery")

    return {"job_id": job_id, "enqueued": len(to_enqueue)}


@celery_app.task(bind=True, name="start_job")
def start_job(self, job_id: str) -> dict[str, Any]:
    """Lightweight coordinator task (no LLM). Used by /resume and by the process_batch shim."""
    logger.info("START_JOB", task_id=self.request.id, job_id=job_id)
    return _start_job_impl(job_id)


def _set_case_status(job_id: str, case_number: int, status: CaseStatus) -> None:
    db = SessionLocal()
    try:
        case = db.query(JobCase).filter(
            JobCase.job_id == uuid.UUID(job_id),
            JobCase.case_number == case_number,
        ).first()
        if case:
            case.status = status
            db.commit()
    except Exception as e:
        logger.warning("set_case_status_failed", job_id=job_id, case_number=case_number, error=str(e))
        db.rollback()
    finally:
        db.close()


def _maybe_finalize(job_id: str) -> None:
    """If no cases remain QUEUED/PROCESSING, enqueue finalize_job (idempotent)."""
    db = SessionLocal()
    try:
        incomplete = _count_cases_by_status(
            db, uuid.UUID(job_id), [CaseStatus.QUEUED, CaseStatus.PROCESSING]
        )
    except Exception as e:
        logger.warning("maybe_finalize_count_failed", job_id=job_id, error=str(e))
        incomplete = -1
    finally:
        db.close()
    if incomplete == 0:
        finalize_job.apply_async(args=[job_id], queue="celery")


@celery_app.task(
    bind=True,
    name="process_one_case",
    ignore_result=True,
    max_retries=2,
    acks_late=True,
)
def process_one_case(self, job_id: str, case_id: str, case_number: int) -> dict[str, Any]:
    """Process ONE case. Short, idempotent, retryable — the unit of recovery.

    - Skips if already COMPLETED (makes resume trivial; RISK_REGISTER R-2).
    - Honors cancellation (job STOPPING/CANCELLED).
    - ignore_result=True so no per-case payload lands in the Redis backend (R-4).
    - Transient failures retry with backoff; deterministic failures stay FAILED.
    - The last case to finish triggers finalize_job (DB-driven, no chord; N-2).
    """
    db = SessionLocal()
    raw_text = ""
    source_file = None
    snapshot = None
    try:
        job = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
        case = db.query(JobCase).filter(
            JobCase.job_id == uuid.UUID(job_id),
            JobCase.case_number == case_number,
        ).first()

        if job is None or case is None:
            logger.warning("PROCESS_ONE_CASE_MISSING", job_id=job_id, case_number=case_number)
            return {"skipped": "missing", "case_number": case_number}

        # Cancellation: mark this case terminal so counts settle and finalize can run.
        if job.status in (JobStatus.STOPPING, JobStatus.CANCELLED):
            if case.status not in (CaseStatus.COMPLETED, CaseStatus.FAILED):
                case.status = CaseStatus.FAILED
                case.error_message = "Job cancelled by user"
                db.commit()
            _maybe_finalize(job_id)
            return {"skipped": "cancelled", "case_number": case_number}

        # Idempotent skip — already done.
        if case.status == CaseStatus.COMPLETED:
            _maybe_finalize(job_id)
            return {"skipped": "already_completed", "case_number": case_number}

        # Claim the case.
        raw_text = case.input_text or ""
        source_file = case.source_file
        snapshot = job.pipeline_snapshot
        case.status = CaseStatus.PROCESSING
        db.commit()
    finally:
        db.close()

    # Run the pipeline (own event loop per task; impl persists result + status to DB).
    try:
        result = _process_case_impl(
            case_id=case_id,
            job_id=job_id,
            raw_text=raw_text,
            case_number=case_number,
            task_id=self.request.id,
            loop=None,
            pipeline_layers=snapshot if snapshot else None,
            source_file=source_file,
        )
    except Exception as exc:  # _process_case_impl normally swallows errors; defensive net
        logger.error("PROCESS_ONE_CASE_CRASH", job_id=job_id, case_number=case_number, error=str(exc))
        result = {"success": False, "error": str(exc)}
        save_case_result(job_id, case_number, {
            "case_id": case_id, "case_number": case_number, "job_id": job_id,
            "success": False, "error": str(exc),
        })

    # Retry transient failures.
    if not result.get("success"):
        err = result.get("error")
        if _is_retryable(err) and self.request.retries < self.max_retries:
            # Reset to QUEUED so the "last one finalizes" check stays accurate during backoff.
            _set_case_status(job_id, case_number, CaseStatus.QUEUED)
            logger.info(
                "CASE_RETRY", job_id=job_id, case_number=case_number,
                attempt=self.request.retries + 1, error=err,
            )
            raise self.retry(countdown=_retry_backoff_seconds(self.request.retries))

    _recompute_job_counts(job_id)
    _maybe_finalize(job_id)
    return {"case_number": case_number, "success": bool(result.get("success"))}


def _finalize_job_impl(job_id: str) -> dict[str, Any]:
    """Recompute counts from the DB and set the terminal job status. Idempotent — safe to call
    multiple times (races, redelivery, resume)."""
    from app.utils.stream_manager import StreamPublisher

    db = SessionLocal()
    final_status = None
    total = completed = failed = 0
    try:
        job_uuid = uuid.UUID(job_id)
        job = db.query(Job).filter(Job.id == job_uuid).first()
        if not job:
            return {"job_id": job_id, "status": "missing"}

        total = db.query(JobCase).filter(JobCase.job_id == job_uuid).count()
        completed = _count_cases_by_status(db, job_uuid, [CaseStatus.COMPLETED])
        failed = _count_cases_by_status(db, job_uuid, [CaseStatus.FAILED])
        incomplete = total - completed - failed
        was_cancelling = job.status in (JobStatus.STOPPING, JobStatus.CANCELLED)

        job.completed_count = completed
        job.failed_count = failed

        if incomplete > 0 and not was_cancelling:
            # Called too early — leave PROCESSING, just persist refreshed counts.
            db.commit()
            return {"job_id": job_id, "status": "still_processing", "incomplete": incomplete}

        if was_cancelling:
            final_status = JobStatus.CANCELLED
        elif total > 0 and completed > 0:
            final_status = JobStatus.COMPLETED  # full or partial success
        else:
            final_status = JobStatus.FAILED

        job.status = final_status
        job.completed_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        logger.error("finalize_job_failed", job_id=job_id, error=str(e), exc_info=True)
        db.rollback()
        return {"job_id": job_id, "status": "error", "error": str(e)}
    finally:
        db.close()

    # Publish terminal SSE event (best effort).
    try:
        pub = StreamPublisher()
        if final_status == JobStatus.CANCELLED:
            pub.publish_job_failed(job_id, f"Job cancelled ({completed}/{total} cases completed)")
        elif final_status == JobStatus.FAILED:
            pub.publish_job_failed(job_id, f"All cases failed ({failed}/{total})")
        else:
            pub.publish_job_complete(job_id, total, completed)
        pub.close()
    except Exception:
        pass

    logger.info(
        "JOB_FINALIZED", job_id=job_id,
        status=final_status.value if final_status else None,
        total=total, completed=completed, failed=failed,
    )
    return {
        "job_id": job_id, "status": final_status.value if final_status else None,
        "total": total, "completed": completed, "failed": failed,
    }


@celery_app.task(bind=True, name="finalize_job")
def finalize_job(self, job_id: str) -> dict[str, Any]:
    """Chord-free finalizer: invoked by the last case to complete (or by start_job when there
    is nothing to enqueue)."""
    return _finalize_job_impl(job_id)


@celery_app.task(bind=True, name="process_batch")
def process_batch(self, job_id: str, cases: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Entry point kept for backward compatibility (upload, reprocess, replay all call this).

    Default: delegates to the per-case fan-out coordinator (start_job). The `cases` arg is no
    longer needed (the coordinator reads cases from the DB) but is still accepted so existing
    callers are unchanged.

    Kill-switch: if BATCH_FANOUT_ENABLED is off, runs the LEGACY sequential processor
    (rollback path; IMPLEMENTATION_PLAN §4).
    """
    if not _fanout_enabled():
        logger.warning("FANOUT_DISABLED_USING_LEGACY", job_id=job_id)
        return _process_batch_legacy(self, job_id, cases or [])

    logger.info("PROCESS_BATCH_DELEGATE_FANOUT", task_id=self.request.id, job_id=job_id)
    return _start_job_impl(job_id)


def _process_batch_legacy(
    self,
    job_id: str,
    cases: list[dict[str, str]],
) -> dict[str, Any]:
    """
    LEGACY sequential batch processor (kept as a rollback path only).

    Runs every case in ONE task on a shared event loop — the original behavior. Reached only
    when the fan-out kill-switch (BATCH_FANOUT_ENABLED) is OFF. The default path is the
    per-case fan-out (start_job / process_one_case).

    Args:
        job_id: Job ID for the batch
        cases: List of dicts with 'case_id' and 'text'
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

    cancelled = False

    try:
        for i, case in enumerate(cases):
            case_id = case.get("case_id", str(i + 1))
            text = case.get("text", "")
            case_number = case.get("case_number", i + 1)

            # ── Cancellation check: query job status before each case ──
            db = SessionLocal()
            try:
                job_check = db.query(Job).filter(Job.id == uuid.UUID(job_id)).first()
                if job_check and job_check.status in [JobStatus.STOPPING, JobStatus.CANCELLED]:
                    logger.info(
                        "JOB_STOPPING_DETECTED",
                        job_id=job_id,
                        case_index=i,
                        completed_so_far=i,
                        total=len(cases),
                    )
                    # Mark remaining cases as cancelled
                    remaining_cases = db.query(JobCase).filter(
                        JobCase.job_id == uuid.UUID(job_id),
                        JobCase.status.in_([CaseStatus.QUEUED, CaseStatus.PROCESSING]),
                    ).all()
                    for rc in remaining_cases:
                        rc.status = CaseStatus.FAILED
                        rc.error_message = "Job cancelled by user"
                    db.commit()
                    cancelled = True
                    break
            finally:
                db.close()

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

            # ── Per-case fail-safe: one case failure must NOT crash the batch ──
            logger.info("CASE_STARTED", job_id=job_id, case_id=case_id, case_number=case_number, index=i + 1, total=len(cases))
            try:
                result = _process_case_impl(
                    case_id, job_id, text, case_number,
                    loop=loop,
                    pipeline_layers=pipeline_snapshot if pipeline_snapshot else None,
                    source_file=case.get("source_file"),
                )
                logger.info(
                    "CASE_FINISHED",
                    job_id=job_id,
                    case_id=case_id,
                    case_number=case_number,
                    success=result.get("success", False),
                )
            except Exception as case_err:
                logger.error(
                    "CASE_FAILED",
                    job_id=job_id,
                    case_id=case_id,
                    case_number=case_number,
                    error=str(case_err),
                    exc_info=True,
                )
                result = {
                    "case_id": case_id,
                    "case_number": case_number,
                    "job_id": job_id,
                    "success": False,
                    "error": str(case_err),
                    "processed_at": datetime.utcnow().isoformat() + "Z",
                }
                # Persist the failure so partial results are visible
                save_case_result(job_id, case_number, result)

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

    if cancelled:
        final_status = JobStatus.CANCELLED
    elif success_count == len(cases):
        final_status = JobStatus.COMPLETED
    elif success_count > 0:
        # Partial success — still mark as completed but with failed count
        final_status = JobStatus.COMPLETED
    else:
        final_status = JobStatus.FAILED

    # Update job status
    update_job_status(job_id, final_status)

    # Emit job completion SSE event
    try:
        if job_publisher:
            if cancelled:
                job_publisher.publish_job_failed(job_id, f"Job cancelled ({success_count}/{len(cases)} cases completed)")
            else:
                job_publisher.publish_job_complete(job_id, len(cases), success_count)
            job_publisher.close()
    except Exception:
        pass

    lifecycle_event = "JOB_CANCELLED" if cancelled else ("JOB_COMPLETED" if final_status == JobStatus.COMPLETED else "JOB_FAILED")
    logger.info(
        lifecycle_event,
        task_id=self.request.id,
        job_id=job_id,
        case_count=len(cases),
        success_count=success_count,
        processed_count=len(results),
        status=final_status.value,
        cancelled=cancelled,
    )

    return {
        "job_id": job_id,
        "status": final_status.value,
        "case_count": len(cases),
        "success_count": success_count,
        "processed_count": len(results),
        "cancelled": cancelled,
        "results": results,
    }

