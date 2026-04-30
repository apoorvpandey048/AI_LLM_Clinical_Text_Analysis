"""
Pydantic schemas for API request/response validation.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# ============================================
# Response Wrappers
# ============================================


class ResponseMeta(BaseModel):
    """Metadata included in all responses."""
    timestamp: str
    request_id: str | None = None


class APIResponse(BaseModel):
    """Standard API response wrapper."""
    success: bool
    data: Any = None
    error: dict | None = None
    meta: ResponseMeta


# ============================================
# Upload Schemas
# ============================================


class TextUploadRequest(BaseModel):
    """Request body for text upload."""
    text: str = Field(..., min_length=10)
    case_id: str | None = None
    backend: str | None = None
    openrouter_key: str | None = None


class BatchUploadRequest(BaseModel):
    """Request body for batch text upload."""
    cases: list[dict[str, str]] = Field(..., min_length=1)


class UploadResponse(BaseModel):
    """Response for upload endpoint."""
    job_id: str
    status: str
    case_count: int
    message: str


# ============================================
# Job Schemas
# ============================================


class JobSummary(BaseModel):
    """Summary of a job for list endpoint."""
    job_id: str
    status: str
    case_count: int
    completed_count: int
    failed_count: int
    source_type: str | None
    source_filename: str | None
    created_at: str | None
    updated_at: str | None


class JobListResponse(BaseModel):
    """Response for jobs list endpoint."""
    jobs: list[JobSummary]
    total: int
    page: int
    limit: int


class CaseStatus(BaseModel):
    """Status of a single case."""
    case_id: str
    case_number: int
    status: str
    final_verdict: str | None = None
    final_cci: float | None = None


class JobDetailResponse(BaseModel):
    """Detailed job status response."""
    job_id: str
    status: str
    case_count: int
    completed_count: int
    failed_count: int
    cases: list[CaseStatus]
    created_at: str | None
    updated_at: str | None
    started_at: str | None
    completed_at: str | None


class CaseResult(BaseModel):
    """Full result for a single case."""
    case_id: str
    case_number: int
    case_label: str | None
    status: str
    layer1_output: dict | None
    layer2_output: dict | None
    layer3_output: dict | None
    final_verdict: str | None
    final_cci: float | None
    total_duration_ms: int | None
    total_tokens_input: int | None
    total_tokens_output: int | None
    prompt_version: str | None
    model_version: str | None
    error_message: str | None
    processed_at: str | None


class JobResultsResponse(BaseModel):
    """Full results for a job."""
    job_id: str
    status: str
    results: list[CaseResult]


# ============================================
# Reprocess Schemas
# ============================================


class ReprocessRequest(BaseModel):
    """Request for reprocessing a case."""
    case_id: str | None = None  # If None, reprocess all
    overwrite: bool = True


class ReprocessResponse(BaseModel):
    """Response for reprocess endpoint."""
    job_id: str
    message: str
    cases_queued: int


# ============================================
# Settings Schemas
# ============================================


class RetentionSettings(BaseModel):
    """Auto-delete retention settings."""
    enabled: bool = False
    days: int = 30


class RetentionSettingsResponse(BaseModel):
    """Response for retention settings."""
    settings: RetentionSettings
