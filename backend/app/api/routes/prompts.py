"""
Prompt Templates API routes.

Allows doctors to view, edit, and manage custom prompt templates
for each pipeline layer without touching code.
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.db import get_db
from app.db.models import PromptTemplate
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/prompts", tags=["Prompts"])

VALID_LAYERS = ["layer1_ctp", "layer2_cie", "layer3_ccc"]

LAYER_LABELS = {
    "layer1_ctp": "Layer 1 — Clinical Text Pre-Processor (CTP)",
    "layer2_cie": "Layer 2 — Complication Identification & Extraction (CIE)",
    "layer3_ccc": "Layer 3 — Clavien-Dindo Classification & CCI (CCC)",
}

PROMPT_FILES = {
    "layer1_ctp": "layer1_ctp_v1.3.md",
    "layer2_cie": "layer2_cie_v1.3.md",
    "layer3_ccc": "layer3_ccc_v1.3.md",
}


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


def _load_default_prompt(layer_name: str) -> str:
    """Load the default prompt from file."""
    filename = PROMPT_FILES.get(layer_name)
    if not filename:
        return ""

    # Try Docker path first, then development path
    paths = [
        Path(f"/app/prompts/snapai/{filename}"),
        Path(__file__).parent.parent.parent.parent / "prompts" / "snapai" / filename,
    ]

    for path in paths:
        if path.exists():
            return path.read_text(encoding="utf-8")

    return ""


class PromptUpdateRequest(BaseModel):
    """Request body for updating a prompt template."""
    content: str = Field(..., min_length=10)
    label: str | None = None


@router.get("")
async def list_prompts(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    List all prompt templates (default + custom).

    Returns the current active prompt for each layer.
    """
    request_id = getattr(request.state, "request_id", None)

    prompts = []
    for layer in VALID_LAYERS:
        # Check for custom active prompt
        custom = db.query(PromptTemplate).filter(
            PromptTemplate.layer_name == layer,
            PromptTemplate.is_active == True,
        ).first()

        if custom:
            prompts.append({
                "layer_name": layer,
                "layer_label": LAYER_LABELS.get(layer, layer),
                "source": "custom",
                "content": custom.content,
                "label": custom.label,
                "version": custom.version,
                "id": str(custom.id),
                "updated_at": custom.updated_at.isoformat() + "Z" if custom.updated_at else None,
            })
        else:
            default_content = _load_default_prompt(layer)
            prompts.append({
                "layer_name": layer,
                "layer_label": LAYER_LABELS.get(layer, layer),
                "source": "default",
                "content": default_content,
                "label": LAYER_LABELS.get(layer, layer),
                "version": "1.3",
                "id": None,
                "updated_at": None,
            })

    return create_response(
        success=True,
        data={"prompts": prompts},
        request_id=request_id,
    )


@router.get("/{layer_name}")
async def get_prompt(
    layer_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Get the current prompt for a specific layer."""
    request_id = getattr(request.state, "request_id", None)

    if layer_name not in VALID_LAYERS:
        raise HTTPException(status_code=400, detail=f"Invalid layer: {layer_name}")

    # Check for custom active prompt
    custom = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if custom:
        return create_response(
            success=True,
            data={
                "prompt": custom.to_dict(),
                "source": "custom",
                "default_content": _load_default_prompt(layer_name),
            },
            request_id=request_id,
        )

    default_content = _load_default_prompt(layer_name)
    return create_response(
        success=True,
        data={
            "prompt": {
                "layer_name": layer_name,
                "content": default_content,
                "version": "1.3",
                "is_active": True,
            },
            "source": "default",
            "default_content": default_content,
        },
        request_id=request_id,
    )


@router.put("/{layer_name}")
async def update_prompt(
    layer_name: str,
    body: PromptUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save or update a custom prompt for a layer."""
    request_id = getattr(request.state, "request_id", None)

    if layer_name not in VALID_LAYERS:
        raise HTTPException(status_code=400, detail=f"Invalid layer: {layer_name}")

    # Deactivate existing custom prompts for this layer
    db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).update({"is_active": False})

    # Create new custom prompt
    template = PromptTemplate(
        layer_name=layer_name,
        content=body.content,
        label=body.label or LAYER_LABELS.get(layer_name, layer_name),
        version="custom",
        is_active=True,
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    logger.info(
        "prompt_updated",
        layer=layer_name,
        prompt_id=str(template.id),
        content_length=len(body.content),
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "prompt": template.to_dict(),
            "message": f"Custom prompt saved for {LAYER_LABELS.get(layer_name, layer_name)}",
        },
        request_id=request_id,
    )


@router.post("/{layer_name}/reset")
async def reset_prompt(
    layer_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Reset a layer's prompt to the default."""
    request_id = getattr(request.state, "request_id", None)

    if layer_name not in VALID_LAYERS:
        raise HTTPException(status_code=400, detail=f"Invalid layer: {layer_name}")

    # Deactivate all custom prompts for this layer
    count = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).update({"is_active": False})
    db.commit()

    logger.info(
        "prompt_reset",
        layer=layer_name,
        deactivated_count=count,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "message": f"Prompt reset to default for {LAYER_LABELS.get(layer_name, layer_name)}",
            "content": _load_default_prompt(layer_name),
        },
        request_id=request_id,
    )
