"""
Prompt Templates API routes.

Supports:
- Built-in layers (layer1_ctp, layer2_cie, layer3_ccc)
- User-created custom layers with streaming support
- Version history per layer with rollback
- Version selection (set any version as active)
- Default prompt management per layer
- Import/export of all prompts
"""

import re
import uuid as uuid_module
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request, HTTPException

from app.api.routes.auth import require_auth, require_admin
from app.db.models import User
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.db import get_db
from app.db.models import PromptTemplate, PromptVersion
from app.config import get_settings
from app.utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/prompts", tags=["Prompts"])

# Built-in layers (always available; have default files on disk)
BUILTIN_LAYERS = ["layer1_ctp", "layer2_cie", "layer3_ccc"]

LAYER_LABELS = {
    "layer1_ctp": "Layer 1 — Clinical Text Pre-Processor (CTP)",
    "layer2_cie": "Layer 2 — Complication Identification & Extraction (CIE)",
    "layer3_ccc": "Layer 3 — Clavien-Dindo Classification & CCI (CCC)",
}


def _get_prompt_files() -> dict[str, str]:
    """Build prompt filename dict using the current prompt_version from settings."""
    version = get_settings().prompt_version
    return {
        "layer1_ctp": f"layer1_ctp_v{version}.md",
        "layer2_cie": f"layer2_cie_v{version}.md",
        "layer3_ccc": f"layer3_ccc_v{version}.md",
    }

# Valid layer name regex (letters, numbers, underscore, hyphen)
LAYER_NAME_REGEX = re.compile(r'^[a-z][a-z0-9_\-]{2,49}$')


# ============================================
# Helpers
# ============================================


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
    """Load the default prompt from file for built-in layers.

    Uses the current prompt_version from settings to find the correct file.
    Falls back to v1.3 if the versioned file does not exist.
    """
    prompt_files = _get_prompt_files()
    filename = prompt_files.get(layer_name)
    if not filename:
        return ""

    paths = [
        Path(f"/app/prompts/snapai/{filename}"),
        Path(__file__).parent.parent.parent.parent / "prompts" / "snapai" / filename,
    ]

    for path in paths:
        if path.exists():
            logger.info("prompt_file_loaded", layer=layer_name, path=str(path))
            return path.read_text(encoding="utf-8")

    # Fallback: try v1.3 if versioned file not found
    fallback = f"{layer_name}_v1.3.md"
    fallback_paths = [
        Path(f"/app/prompts/snapai/{fallback}"),
        Path(__file__).parent.parent.parent.parent / "prompts" / "snapai" / fallback,
    ]
    for path in fallback_paths:
        if path.exists():
            logger.warning("prompt_file_fallback_v1.3", layer=layer_name, wanted=filename, using=str(path))
            return path.read_text(encoding="utf-8")

    return ""


def _validate_layer_name(layer_name: str) -> None:
    """Validate layer name format."""
    if not LAYER_NAME_REGEX.match(layer_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid layer name '{layer_name}'. Must start with a lowercase letter, "
                "be 3-50 chars, and contain only lowercase letters, numbers, underscores, or hyphens."
            ),
        )


def _get_effective_content(template: PromptTemplate, db: Session) -> str:
    """Get the effective prompt content, considering active version selection."""
    if template.active_version_id:
        version = db.query(PromptVersion).filter(
            PromptVersion.id == template.active_version_id
        ).first()
        if version:
            return version.content
    return template.content


# ============================================
# Schemas
# ============================================

class PromptUpdateRequest(BaseModel):
    """Request body for updating a prompt template."""
    content: str = Field(..., min_length=10)
    label: str | None = None


class CreateLayerRequest(BaseModel):
    """Request body for creating a new custom layer."""
    layer_name: str = Field(..., min_length=3, max_length=50)
    label: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=10)
    display_order: int | None = None


class SetVersionRequest(BaseModel):
    """Request body for setting an active version."""
    version_id: str | None = None  # None = use current template content


class SetDefaultRequest(BaseModel):
    """Request body for marking a prompt version as default."""
    set_default: bool = True


class ReorderRequest(BaseModel):
    """Request body for reordering pipeline layers."""
    order: list[dict] = Field(
        ...,
        description="List of {layer_name, display_order} dicts.",
    )


class RenameLayerRequest(BaseModel):
    """Request body for renaming a layer's display label."""
    label: str = Field(..., min_length=1, max_length=200)


class PromptImportRequest(BaseModel):
    """Request body for importing prompts."""
    prompts: dict


# ============================================
# Fixed-path routes FIRST (before /{layer_name})
# ============================================

@router.get("")
async def list_prompts(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    List all prompt layers (built-in + custom) with their current prompt.

    Returns all layers in display order.
    """
    request_id = getattr(request.state, "request_id", None)

    prompts = []

    # 1) Built-in layers
    for layer in BUILTIN_LAYERS:
        custom = db.query(PromptTemplate).filter(
            PromptTemplate.layer_name == layer,
            PromptTemplate.is_active == True,
        ).first()

        if custom:
            effective_content = _get_effective_content(custom, db)
            prompts.append({
                "layer_name": layer,
                "layer_label": custom.label or LAYER_LABELS.get(layer, layer),
                "source": "custom",
                "content": effective_content,
                "label": custom.label or LAYER_LABELS.get(layer, layer),
                "version": custom.version,
                "id": str(custom.id),
                "is_builtin": True,
                "display_order": custom.display_order if custom.display_order != 99 else BUILTIN_LAYERS.index(layer),
                "is_default_prompt": custom.is_default_prompt,
                "active_version_id": str(custom.active_version_id) if custom.active_version_id else None,
                "version_count": len(custom.versions) if custom.versions else 0,
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
                "version": get_settings().prompt_version,
                "id": None,
                "is_builtin": True,
                "display_order": BUILTIN_LAYERS.index(layer),
                "is_default_prompt": True,
                "active_version_id": None,
                "version_count": 0,
                "updated_at": None,
            })

    # 2) Custom layers
    custom_templates = db.query(PromptTemplate).filter(
        PromptTemplate.is_active == True,
        PromptTemplate.layer_name.notin_(BUILTIN_LAYERS),
    ).order_by(PromptTemplate.display_order, PromptTemplate.created_at).all()

    for tmpl in custom_templates:
        effective_content = _get_effective_content(tmpl, db)
        prompts.append({
            "layer_name": tmpl.layer_name,
            "layer_label": tmpl.label or tmpl.layer_name,
            "source": "custom",
            "content": effective_content,
            "label": tmpl.label or tmpl.layer_name,
            "version": tmpl.version,
            "id": str(tmpl.id),
            "is_builtin": False,
            "display_order": tmpl.display_order,
            "is_default_prompt": tmpl.is_default_prompt,
            "active_version_id": str(tmpl.active_version_id) if tmpl.active_version_id else None,
            "version_count": len(tmpl.versions) if tmpl.versions else 0,
            "updated_at": tmpl.updated_at.isoformat() + "Z" if tmpl.updated_at else None,
        })

    # Sort by display_order
    prompts.sort(key=lambda p: p["display_order"])

    return create_response(
        success=True,
        data={"prompts": prompts},
        request_id=request_id,
    )


@router.get("/layers")
async def list_layers(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """
    List all available layer names with metadata (for streaming/pipeline/frontend use).
    """
    request_id = getattr(request.state, "request_id", None)

    layers = []

    # Built-in layers
    for idx, layer in enumerate(BUILTIN_LAYERS):
        custom = db.query(PromptTemplate).filter(
            PromptTemplate.layer_name == layer,
            PromptTemplate.is_active == True,
        ).first()

        layers.append({
            "layer_name": layer,
            "label": custom.label if custom else LAYER_LABELS.get(layer, layer),
            "is_builtin": True,
            "display_order": idx,
            "has_custom_prompt": custom is not None,
        })

    # Custom layers
    custom_templates = db.query(PromptTemplate).filter(
        PromptTemplate.is_active == True,
        PromptTemplate.layer_name.notin_(BUILTIN_LAYERS),
    ).order_by(PromptTemplate.display_order, PromptTemplate.created_at).all()

    for tmpl in custom_templates:
        layers.append({
            "layer_name": tmpl.layer_name,
            "label": tmpl.label or tmpl.layer_name,
            "is_builtin": False,
            "display_order": tmpl.display_order,
            "has_custom_prompt": True,
        })

    return create_response(
        success=True,
        data={
            "layers": layers,
            "builtin": BUILTIN_LAYERS,
        },
        request_id=request_id,
    )


@router.get("/export")
async def export_prompts(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Export all prompts (built-in + custom) as JSON."""
    request_id = getattr(request.state, "request_id", None)

    export_data = {
        "version": "2.0",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "prompts": {},
    }

    # Built-in layers
    for layer in BUILTIN_LAYERS:
        custom = db.query(PromptTemplate).filter(
            PromptTemplate.layer_name == layer,
            PromptTemplate.is_active == True,
        ).first()

        if custom:
            export_data["prompts"][layer] = {
                "content": _get_effective_content(custom, db),
                "label": custom.label,
                "version": custom.version,
                "is_builtin": True,
                "updated_at": custom.updated_at.isoformat() + "Z" if custom.updated_at else None,
            }
        else:
            export_data["prompts"][layer] = {
                "content": _load_default_prompt(layer),
                "label": LAYER_LABELS.get(layer),
                "version": get_settings().prompt_version,
                "source": "default",
                "is_builtin": True,
            }

    # Custom layers
    custom_templates = db.query(PromptTemplate).filter(
        PromptTemplate.is_active == True,
        PromptTemplate.layer_name.notin_(BUILTIN_LAYERS),
    ).all()

    for tmpl in custom_templates:
        export_data["prompts"][tmpl.layer_name] = {
            "content": _get_effective_content(tmpl, db),
            "label": tmpl.label,
            "version": tmpl.version,
            "is_builtin": False,
            "display_order": tmpl.display_order,
            "updated_at": tmpl.updated_at.isoformat() + "Z" if tmpl.updated_at else None,
        }

    return create_response(success=True, data=export_data, request_id=request_id)


@router.post("/import")
async def import_prompts(
    body: PromptImportRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Import prompts from JSON. Creates or updates layers."""
    request_id = getattr(request.state, "request_id", None)

    # Size limits
    if len(body.prompts) > 20:
        raise HTTPException(status_code=400, detail="Import limited to 20 prompts maximum")

    imported = []
    for layer_name, prompt_data in body.prompts.items():
        content = prompt_data.get("content")
        if not content or len(content) < 10:
            continue
        if len(content) > 100_000:
            raise HTTPException(
                status_code=400,
                detail=f"Prompt '{layer_name}' exceeds 100KB content limit",
            )

        is_builtin = prompt_data.get("is_builtin", layer_name in BUILTIN_LAYERS)

        # Deactivate existing
        db.query(PromptTemplate).filter(
            PromptTemplate.layer_name == layer_name,
            PromptTemplate.is_active == True,
        ).update({"is_active": False})

        # Skip default built-in prompts (no need to re-import them)
        if prompt_data.get("source") == "default":
            continue

        template = PromptTemplate(
            layer_name=layer_name,
            content=content,
            label=prompt_data.get("label") or LAYER_LABELS.get(layer_name, layer_name),
            version="imported",
            is_active=True,
            is_builtin=is_builtin,
            display_order=prompt_data.get("display_order", 99),
            is_default_prompt=False,
        )
        db.add(template)
        imported.append(layer_name)

    db.commit()

    # Audit log
    from app.db import AuditLog
    audit = AuditLog(
        action="prompts_imported",
        details={"imported_layers": imported, "admin": admin.username},
    )
    db.add(audit)
    db.commit()

    logger.info("prompts_imported", imported_layers=imported, request_id=request_id)

    return create_response(
        success=True,
        data={
            "message": f"Imported {len(imported)} prompt(s)",
            "imported_layers": imported,
        },
        request_id=request_id,
    )


@router.post("/layers")
async def create_custom_layer(
    body: CreateLayerRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Create a new custom pipeline layer.

    Custom layers get their own prompt template and can stream output
    just like built-in layers.
    """
    request_id = getattr(request.state, "request_id", None)
    layer_name = body.layer_name.lower().strip()

    _validate_layer_name(layer_name)

    # Check for duplicates (including built-in)
    if layer_name in BUILTIN_LAYERS:
        raise HTTPException(status_code=409, detail=f"'{layer_name}' is a built-in layer name")

    existing = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if existing:
        raise HTTPException(status_code=409, detail=f"Layer '{layer_name}' already exists")

    # Determine display order
    if body.display_order is not None:
        display_order = body.display_order
    else:
        max_order = db.query(PromptTemplate.display_order).filter(
            PromptTemplate.is_active == True
        ).order_by(PromptTemplate.display_order.desc()).first()
        display_order = (max_order[0] + 1) if max_order else len(BUILTIN_LAYERS)

    template = PromptTemplate(
        layer_name=layer_name,
        label=body.label,
        content=body.content,
        version="v1",
        is_active=True,
        is_builtin=False,
        display_order=display_order,
        is_default_prompt=True,
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    logger.info(
        "custom_layer_created",
        layer=layer_name,
        label=body.label,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "prompt": template.to_dict(),
            "message": f"Custom layer '{body.label}' created",
        },
        request_id=request_id,
    )


@router.post("/reorder")
async def reorder_layers(
    body: ReorderRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Reorder pipeline layers by setting display_order on each.

    Accepts a list of {layer_name, display_order} dicts.
    Validates that all active layers are included.
    """
    request_id = getattr(request.state, "request_id", None)

    if not body.order:
        raise HTTPException(status_code=400, detail="Order list is empty")

    if len(body.order) > 50:
        raise HTTPException(status_code=400, detail="Too many layers (max 50)")

    # Validate no duplicate layer_names
    names = [item.get("layer_name") for item in body.order]
    if len(set(names)) != len(names):
        raise HTTPException(status_code=400, detail="Duplicate layer_name in order list")

    # Load all active templates
    active_templates = db.query(PromptTemplate).filter(
        PromptTemplate.is_active == True
    ).all()
    active_map = {t.layer_name: t for t in active_templates}

    # Validate all submitted names exist and are active
    for item in body.order:
        name = item.get("layer_name")
        order = item.get("display_order")
        if name not in active_map and name not in BUILTIN_LAYERS:
            raise HTTPException(
                status_code=400,
                detail=f"Layer '{name}' not found or not active",
            )
        if order is None or not isinstance(order, (int, float)):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid display_order for layer '{name}'",
            )

    # Apply new order
    updated = []
    for item in body.order:
        name = item["layer_name"]
        order = int(item["display_order"])

        template = active_map.get(name)
        if template:
            template.display_order = order
            updated.append(name)
        elif name in BUILTIN_LAYERS:
            # Built-in layer without a DB record yet — create a minimal
            # record so display_order is persisted.  The prompt content
            # stays as the on-disk default; this row only carries metadata.
            default_content = _load_default_prompt(name)
            new_template = PromptTemplate(
                layer_name=name,
                label=LAYER_LABELS.get(name, name),
                content=default_content,
                version=get_settings().prompt_version,
                is_active=True,
                is_builtin=True,
                display_order=order,
                is_default_prompt=True,
            )
            db.add(new_template)
            updated.append(name)

    db.commit()

    logger.info(
        "layers_reordered",
        updated=updated,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "message": f"Reordered {len(updated)} layer(s)",
            "updated": updated,
        },
        request_id=request_id,
    )


# ============================================
# Parameterised /{layer_name} routes
# ============================================

@router.get("/{layer_name}")
async def get_prompt(
    layer_name: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Get the current prompt for a specific layer."""
    request_id = getattr(request.state, "request_id", None)

    # Check for custom active prompt
    custom = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if custom:
        effective_content = _get_effective_content(custom, db)
        return create_response(
            success=True,
            data={
                "prompt": {
                    **custom.to_dict(),
                    "effective_content": effective_content,
                },
                "source": "custom",
                "default_content": _load_default_prompt(layer_name),
            },
            request_id=request_id,
        )

    # Check if this is a built-in layer with default content
    if layer_name in BUILTIN_LAYERS:
        default_content = _load_default_prompt(layer_name)
        return create_response(
            success=True,
            data={
                "prompt": {
                    "layer_name": layer_name,
                    "content": default_content,
                    "version": get_settings().prompt_version,
                    "is_active": True,
                    "is_builtin": True,
                },
                "source": "default",
                "default_content": default_content,
            },
            request_id=request_id,
        )

    # Unknown layer with no template
    raise HTTPException(status_code=404, detail=f"No prompt found for layer '{layer_name}'")


@router.put("/{layer_name}")
async def update_prompt(
    layer_name: str,
    body: PromptUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Save or update a prompt for a layer. Creates version history."""
    request_id = getattr(request.state, "request_id", None)

    existing = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if existing:
        # Save current content to version history before updating
        version_count = db.query(PromptVersion).filter(
            PromptVersion.template_id == existing.id
        ).count()

        version = PromptVersion(
            template_id=existing.id,
            content=existing.content,
            version_label=f"v{version_count + 1}",
        )
        db.add(version)

        # Prune old versions (keep last 10)
        if version_count >= 10:
            oldest = db.query(PromptVersion).filter(
                PromptVersion.template_id == existing.id
            ).order_by(PromptVersion.created_at.asc()).first()
            if oldest:
                # Don't delete if it's the active version
                if existing.active_version_id != oldest.id:
                    db.delete(oldest)

        # Update template content
        existing.content = body.content
        existing.label = body.label or existing.label
        existing.active_version_id = None  # Reset to use current content
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)

        template = existing
    else:
        # For built-in layers, create a custom override
        if layer_name in BUILTIN_LAYERS:
            template = PromptTemplate(
                layer_name=layer_name,
                content=body.content,
                label=body.label or LAYER_LABELS.get(layer_name, layer_name),
                version="custom",
                is_active=True,
                is_builtin=True,
                display_order=BUILTIN_LAYERS.index(layer_name),
                is_default_prompt=False,
            )
        else:
            # Unknown layer — reject (use POST /prompts/layers to create new ones)
            raise HTTPException(
                status_code=404,
                detail=f"Layer '{layer_name}' not found. Create it first via POST /api/v1/prompts/layers.",
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
            "message": f"Prompt saved for {template.label or layer_name}",
        },
        request_id=request_id,
    )


@router.delete("/{layer_name}")
async def delete_custom_layer(
    layer_name: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Delete a custom layer. Built-in layers cannot be deleted."""
    request_id = getattr(request.state, "request_id", None)

    if layer_name in BUILTIN_LAYERS:
        raise HTTPException(status_code=400, detail="Cannot delete built-in layers. Use reset instead.")

    template = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail=f"Layer '{layer_name}' not found")

    template.is_active = False
    db.commit()

    logger.info("custom_layer_deleted", layer=layer_name, request_id=request_id)

    return create_response(
        success=True,
        data={"message": f"Layer '{layer_name}' deleted"},
        request_id=request_id,
    )


@router.put("/{layer_name}/rename")
async def rename_layer(
    layer_name: str,
    body: RenameLayerRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Rename a layer's display label."""
    request_id = getattr(request.state, "request_id", None)

    template = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if not template:
        if layer_name in BUILTIN_LAYERS:
            # Create a DB record for the built-in layer so we can store the label
            template = PromptTemplate(
                layer_name=layer_name,
                content=_load_default_prompt(layer_name),
                label=body.label,
                version="custom",
                is_active=True,
                is_builtin=True,
                display_order=BUILTIN_LAYERS.index(layer_name),
                is_default_prompt=True,
            )
            db.add(template)
            db.commit()
            db.refresh(template)
        else:
            raise HTTPException(status_code=404, detail=f"Layer '{layer_name}' not found")
    else:
        template.label = body.label
        template.updated_at = datetime.utcnow()
        db.commit()

    logger.info(
        "layer_renamed",
        layer=layer_name,
        new_label=body.label,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "message": f"Layer renamed to '{body.label}'",
            "layer_name": layer_name,
            "label": body.label,
        },
        request_id=request_id,
    )


# ============================================
# Reset (built-in only)
# ============================================

@router.post("/{layer_name}/reset")
async def reset_prompt(
    layer_name: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Reset a layer's prompt to the default."""
    request_id = getattr(request.state, "request_id", None)

    if layer_name not in BUILTIN_LAYERS:
        raise HTTPException(
            status_code=400,
            detail="Only built-in layers can be reset to defaults. Delete a custom layer instead.",
        )

    count = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).update({"is_active": False})
    db.commit()

    logger.info("prompt_reset", layer=layer_name, deactivated_count=count, request_id=request_id)

    return create_response(
        success=True,
        data={
            "message": f"Prompt reset to default for {LAYER_LABELS.get(layer_name, layer_name)}",
            "content": _load_default_prompt(layer_name),
        },
        request_id=request_id,
    )


# ============================================
# Version History / Selection / Default
# ============================================

@router.get("/{layer_name}/history")
async def get_prompt_history(
    layer_name: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Get version history for a layer's prompt (up to last 10)."""
    request_id = getattr(request.state, "request_id", None)

    template = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if not template:
        return create_response(
            success=True,
            data={
                "layer_name": layer_name,
                "versions": [],
                "current_version_id": None,
                "active_version_id": None,
                "message": (
                    "No custom prompt exists for this layer"
                    if layer_name in BUILTIN_LAYERS
                    else "Layer not found"
                ),
            },
            request_id=request_id,
        )

    versions = db.query(PromptVersion).filter(
        PromptVersion.template_id == template.id
    ).order_by(PromptVersion.created_at.desc()).limit(10).all()

    # Prepend the current template content as "current"
    current_entry = {
        "id": "current",
        "template_id": str(template.id),
        "content": template.content,
        "version_label": "Current",
        "created_at": template.updated_at.isoformat() + "Z" if template.updated_at else None,
        "is_active": template.active_version_id is None,
    }

    version_list = [current_entry] + [
        {
            **v.to_dict(),
            "is_active": (
                str(v.id) == str(template.active_version_id)
                if template.active_version_id
                else False
            ),
        }
        for v in versions
    ]

    return create_response(
        success=True,
        data={
            "layer_name": layer_name,
            "current_id": str(template.id),
            "active_version_id": str(template.active_version_id) if template.active_version_id else "current",
            "versions": version_list,
        },
        request_id=request_id,
    )


@router.post("/{layer_name}/set-version")
async def set_active_version(
    layer_name: str,
    body: SetVersionRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Set which version of a prompt to use for this layer.

    - version_id=null or "current" → use the template's current content
    - version_id=<uuid> → use a specific historical version
    """
    request_id = getattr(request.state, "request_id", None)

    template = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail=f"No prompt found for layer '{layer_name}'")

    if not body.version_id or body.version_id == "current":
        template.active_version_id = None
        db.commit()
        return create_response(
            success=True,
            data={"message": "Using current prompt content", "active_version_id": "current"},
            request_id=request_id,
        )

    # Validate version exists and belongs to this template
    try:
        version_uuid = uuid_module.UUID(body.version_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid version ID")

    version = db.query(PromptVersion).filter(
        PromptVersion.id == version_uuid,
        PromptVersion.template_id == template.id,
    ).first()

    if not version:
        raise HTTPException(status_code=404, detail="Version not found for this layer")

    template.active_version_id = version.id
    db.commit()

    logger.info(
        "version_selected",
        layer=layer_name,
        version_id=body.version_id,
        version_label=version.version_label,
        request_id=request_id,
    )

    return create_response(
        success=True,
        data={
            "message": f"Using version {version.version_label or body.version_id[:8]}",
            "active_version_id": str(version.id),
            "content": version.content,
        },
        request_id=request_id,
    )


@router.post("/{layer_name}/set-default")
async def set_as_default(
    layer_name: str,
    body: SetDefaultRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Mark the current prompt as the default for this layer."""
    request_id = getattr(request.state, "request_id", None)

    template = db.query(PromptTemplate).filter(
        PromptTemplate.layer_name == layer_name,
        PromptTemplate.is_active == True,
    ).first()

    if not template:
        raise HTTPException(status_code=404, detail=f"No custom prompt for layer '{layer_name}'")

    template.is_default_prompt = body.set_default
    db.commit()

    action = "set as" if body.set_default else "unset as"
    logger.info("prompt_default_changed", layer=layer_name, is_default=body.set_default, request_id=request_id)

    return create_response(
        success=True,
        data={
            "message": f"Prompt {action} default for {template.label or layer_name}",
            "is_default_prompt": body.set_default,
        },
        request_id=request_id,
    )


@router.post("/{layer_name}/rollback/{version_id}")
async def rollback_prompt(
    layer_name: str,
    version_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Rollback a layer's prompt to a previous version (copies version content into current)."""
    request_id = getattr(request.state, "request_id", None)

    try:
        version_uuid = uuid_module.UUID(version_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid version ID format")

    version = db.query(PromptVersion).filter(PromptVersion.id == version_uuid).first()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    template = db.query(PromptTemplate).filter(
        PromptTemplate.id == version.template_id,
        PromptTemplate.layer_name == layer_name,
    ).first()
    if not template:
        raise HTTPException(status_code=400, detail="Version does not belong to this layer")

    # Save current content as a version before overwriting
    current_version = PromptVersion(
        template_id=template.id,
        content=template.content,
        version_label="pre-rollback",
    )
    db.add(current_version)

    template.content = version.content
    template.active_version_id = None  # Use the now-current content directly
    template.updated_at = datetime.utcnow()
    db.commit()

    logger.info("prompt_rollback", layer=layer_name, version_id=version_id, request_id=request_id)

    return create_response(
        success=True,
        data={
            "message": f"Rolled back to version {version.version_label or version_id[:8]}",
            "prompt": template.to_dict(),
        },
        request_id=request_id,
    )
