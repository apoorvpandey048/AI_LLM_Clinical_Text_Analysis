"""
Download API routes for per-layer stream export.

Provides endpoints to download:
- Raw stream text per layer (exact UI fidelity)
- Parsed JSON per layer
- Full layer execution bundle
- Complete case export (all layers)
- Complete job/session export (all cases × all layers)
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.routes.auth import require_auth
from app.db import get_db
from app.db.models import Job, JobCase, User, UserRole
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/downloads", tags=["Downloads"])


# ============================================
# Helpers
# ============================================


def _check_job_ownership(job: Job, user: User) -> None:
    """Verify the authenticated user owns this job (or is admin)."""
    if user.role == UserRole.ADMIN:
        return
    if job.user_id is not None and job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied: you do not own this job")


def _load_case(case_id: str, db: Session, user: User) -> JobCase:
    """Load a case by ID with ownership check."""
    try:
        case_uuid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid case ID format")

    case = db.query(JobCase).filter(JobCase.id == case_uuid).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Check job ownership
    job = db.query(Job).filter(
        and_(Job.id == case.job_id, Job.deleted_at.is_(None))
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Parent job not found")

    _check_job_ownership(job, user)
    return case


def _get_stream_data_for_layer(case: JobCase, layer_name: str) -> dict | None:
    """Get stream data for a specific layer.

    Checks layer_stream_data first, then falls back to legacy raw_output columns.
    """
    # Primary source: per-layer stream capture
    if case.layer_stream_data and layer_name in case.layer_stream_data:
        return case.layer_stream_data[layer_name]

    # Fallback: legacy raw_output columns (for cases processed before this feature)
    legacy_map = {
        "layer1_ctp": case.layer1_raw_output,
        "layer2_cie": case.layer2_raw_output,
        "layer3_ccc": case.layer3_raw_output,
    }

    # Also check sub-layer names that map to parent layers
    sublayer_to_parent = {
        "layer1a_extraction": "layer1_ctp",
        "layer1b_structuring": "layer1_ctp",
        "layer2a_extraction": "layer2_cie",
        "layer2b_grading": "layer2_cie",
        "layer3a_evidence": "layer3_ccc",
        "layer3b_rules": "layer3_ccc",
        "layer3c_omissions": "layer3_ccc",
        "layer3d_auditor": "layer3_ccc",
    }

    raw_text = legacy_map.get(layer_name)

    # Try extra_layer_raw_outputs for custom layers
    if raw_text is None and case.extra_layer_raw_outputs:
        raw_text = case.extra_layer_raw_outputs.get(layer_name)

    # Try sub-layer to parent mapping
    if raw_text is None:
        parent = sublayer_to_parent.get(layer_name)
        if parent:
            raw_text = legacy_map.get(parent)

    if raw_text is None:
        return None

    # Build a compatible structure from legacy data
    from app.utils.json_utils import safe_json_parse
    return {
        "raw_stream": raw_text,
        "assembled_text": raw_text,
        "parsed_json": safe_json_parse(raw_text),
        "start_time": None,
        "end_time": None,
        "_source": "legacy_fallback",
    }


def _get_all_layer_names(case: JobCase) -> list[str]:
    """Get all available layer names for a case."""
    layers = []

    if case.layer_stream_data:
        layers.extend(case.layer_stream_data.keys())
    else:
        # Fallback: infer from existing columns
        if case.layer1_raw_output:
            layers.append("layer1_ctp")
        if case.layer2_raw_output:
            layers.append("layer2_cie")
        if case.layer3_raw_output:
            layers.append("layer3_ccc")
        if case.extra_layer_raw_outputs:
            layers.extend(case.extra_layer_raw_outputs.keys())

    return layers


def _get_layer_output(case: JobCase, layer_name: str) -> dict | None:
    """Get the parsed output for a specific layer."""
    output_map = {
        "layer1_ctp": case.layer1_output,
        "layer2_cie": case.layer2_output,
        "layer3_ccc": case.layer3_output,
    }

    result = output_map.get(layer_name)
    if result is None and case.extra_layer_outputs:
        result = case.extra_layer_outputs.get(layer_name)

    return result


# ============================================
# Per-Layer Download Endpoints
# ============================================


@router.get("/cases/{case_id}/layers/{layer_name}/raw")
async def download_layer_raw(
    case_id: str,
    layer_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Download raw stream text for a specific layer.

    Returns the exact text that was displayed in the UI during streaming.
    Content-Type: text/plain with download headers.
    """
    case = _load_case(case_id, db, user)
    stream_data = _get_stream_data_for_layer(case, layer_name)

    if not stream_data or not stream_data.get("raw_stream"):
        raise HTTPException(
            status_code=404,
            detail=f"No stream data available for layer '{layer_name}'"
        )

    raw_text = stream_data["raw_stream"]
    filename = f"snap-ai_{layer_name}_raw_{case_id[:8]}.txt"

    return PlainTextResponse(
        content=raw_text,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/cases/{case_id}/layers/{layer_name}/json")
async def download_layer_json(
    case_id: str,
    layer_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Download parsed JSON for a specific layer.

    Returns the best-effort JSON parse of the layer stream.
    Returns 404 if no valid JSON could be extracted.
    """
    import json

    case = _load_case(case_id, db, user)

    # Try stream data first
    stream_data = _get_stream_data_for_layer(case, layer_name)
    parsed = None

    if stream_data:
        parsed = stream_data.get("parsed_json")

    # Fall back to stored layer output
    if parsed is None:
        parsed = _get_layer_output(case, layer_name)

    if parsed is None:
        raise HTTPException(
            status_code=404,
            detail=f"No valid JSON available for layer '{layer_name}'"
        )

    filename = f"snap-ai_{layer_name}_parsed_{case_id[:8]}.json"
    content = json.dumps(parsed, indent=2, ensure_ascii=False)

    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/cases/{case_id}/layers/{layer_name}/full")
async def download_layer_full(
    case_id: str,
    layer_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Download full execution bundle for a specific layer.

    Returns a JSON document containing:
    - layer_id, case_id
    - input text
    - raw_stream (exact streamed text)
    - parsed_json (best-effort parse)
    - metadata (model, timestamps, etc.)
    """
    import json

    case = _load_case(case_id, db, user)
    stream_data = _get_stream_data_for_layer(case, layer_name)

    if not stream_data:
        raise HTTPException(
            status_code=404,
            detail=f"No data available for layer '{layer_name}'"
        )

    bundle = {
        "layer_id": layer_name,
        "case_id": str(case.id),
        "case_number": case.case_number,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "input": case.input_text,
        "raw_stream": stream_data.get("raw_stream", ""),
        "assembled_text": stream_data.get("assembled_text", ""),
        "parsed_json": stream_data.get("parsed_json"),
        "layer_output": _get_layer_output(case, layer_name),
        "metadata": {
            "model": case.model_version,
            "prompt_version": case.prompt_version,
            "total_duration_ms": case.total_duration_ms,
            "start_time": stream_data.get("start_time"),
            "end_time": stream_data.get("end_time"),
        },
    }

    filename = f"snap-ai_{layer_name}_full_{case_id[:8]}.json"
    content = json.dumps(bundle, indent=2, ensure_ascii=False)

    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ============================================
# Case & Session Export Endpoints
# ============================================


@router.get("/cases/{case_id}/export")
async def export_case(
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Export all layer data for a single case.

    Returns a JSON document with all layers, their raw streams,
    parsed outputs, and metadata.
    """
    import json

    case = _load_case(case_id, db, user)
    layer_names = _get_all_layer_names(case)

    layers = []
    for layer_name in layer_names:
        stream_data = _get_stream_data_for_layer(case, layer_name)
        layers.append({
            "layer_id": layer_name,
            "raw_stream": stream_data.get("raw_stream", "") if stream_data else None,
            "parsed_json": stream_data.get("parsed_json") if stream_data else None,
            "layer_output": _get_layer_output(case, layer_name),
            "start_time": stream_data.get("start_time") if stream_data else None,
            "end_time": stream_data.get("end_time") if stream_data else None,
        })

    export = {
        "case_id": str(case.id),
        "case_number": case.case_number,
        "case_label": case.case_label,
        "input_text": case.input_text,
        "final_verdict": case.final_verdict,
        "final_cci": case.final_cci,
        "status": case.status.value,
        "model_version": case.model_version,
        "prompt_version": case.prompt_version,
        "total_duration_ms": case.total_duration_ms,
        "processed_at": case.processed_at.isoformat() + "Z" if case.processed_at else None,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "layers": layers,
    }

    filename = f"snap-ai_case_{case_id[:8]}_export.json"
    content = json.dumps(export, indent=2, ensure_ascii=False)

    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/jobs/{job_id}/export")
async def export_session(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    Export all layers for all cases in a job/session.

    Returns a complete JSON document with:
    - Session metadata
    - All cases with all layer streams and outputs
    """
    import json

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

    cases = db.query(JobCase).filter(
        JobCase.job_id == job_uuid
    ).order_by(JobCase.case_number).all()

    session_layers = []
    for case in cases:
        layer_names = _get_all_layer_names(case)
        case_layers = []
        for layer_name in layer_names:
            stream_data = _get_stream_data_for_layer(case, layer_name)
            case_layers.append({
                "layer_id": layer_name,
                "raw_stream": stream_data.get("raw_stream", "") if stream_data else None,
                "parsed_json": stream_data.get("parsed_json") if stream_data else None,
                "layer_output": _get_layer_output(case, layer_name),
                "start_time": stream_data.get("start_time") if stream_data else None,
                "end_time": stream_data.get("end_time") if stream_data else None,
            })

        session_layers.append({
            "case_id": str(case.id),
            "case_number": case.case_number,
            "case_label": case.case_label,
            "input_text": case.input_text,
            "final_verdict": case.final_verdict,
            "final_cci": case.final_cci,
            "status": case.status.value,
            "layers": case_layers,
        })

    export = {
        "session_id": str(job.id),
        "status": job.status.value,
        "model_name": job.model_name,
        "case_count": len(cases),
        "created_at": job.created_at.isoformat() + "Z" if job.created_at else None,
        "completed_at": job.completed_at.isoformat() + "Z" if job.completed_at else None,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "pipeline_snapshot": job.pipeline_snapshot,
        "cases": session_layers,
    }

    filename = f"snap-ai_session_{job_id[:8]}_export.json"
    content = json.dumps(export, indent=2, ensure_ascii=False)

    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ============================================
# Layer listing (for frontend to know what's available)
# ============================================


@router.get("/cases/{case_id}/layers")
async def list_case_layers(
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    List available layers for download for a given case.

    Returns layer names and what data is available (raw, json, full).
    """
    case = _load_case(case_id, db, user)
    layer_names = _get_all_layer_names(case)

    layers_info = []
    for layer_name in layer_names:
        stream_data = _get_stream_data_for_layer(case, layer_name)
        has_raw = bool(stream_data and stream_data.get("raw_stream"))
        has_json = bool(
            (stream_data and stream_data.get("parsed_json"))
            or _get_layer_output(case, layer_name)
        )

        layers_info.append({
            "layer_name": layer_name,
            "has_raw_stream": has_raw,
            "has_parsed_json": has_json,
            "has_full_data": has_raw,  # full is available if raw is available
        })

    return {
        "success": True,
        "data": {
            "case_id": case_id,
            "layers": layers_info,
        },
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
        },
    }
