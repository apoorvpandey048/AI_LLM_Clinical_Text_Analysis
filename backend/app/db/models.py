"""
SQLAlchemy models for SNAP-AI.

All models follow the implementation rules:
- UUID primary keys
- created_at, updated_at timestamps
- Soft delete via deleted_at
- JSONB for layer outputs
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import types as sa_types
from sqlalchemy.orm import declarative_base, relationship

import enum


# Cross-dialect JSON type: uses JSONB on PostgreSQL, falls back to JSON elsewhere (e.g. SQLite)
class PortableJSON(sa_types.TypeDecorator):
    """A JSON type that uses JSONB on PostgreSQL and plain JSON on other databases."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

Base = declarative_base()


class UserRole(str, enum.Enum):
    """User roles."""
    ADMIN = "admin"
    DOCTOR = "doctor"
    PENDING = "pending"  # Waiting for admin approval


class JobStatus(str, enum.Enum):
    """Job processing status."""
    QUEUED = "queued"
    PROCESSING = "processing"
    STOPPING = "stopping"  # Graceful stop requested — worker will finish current case then halt
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CaseStatus(str, enum.Enum):
    """Individual case processing status."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class User(Base):
    """User model for authentication and authorization."""

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.PENDING, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    # Relationships
    jobs = relationship("Job", back_populates="user")

    __table_args__ = (
        Index("ix_users_role", "role"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": str(self.id),
            "username": self.username,
            "name": self.name,
            "role": self.role.value,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "last_login": self.last_login.isoformat() + "Z" if self.last_login else None,
        }


class Job(Base):
    """
    Job model - represents a batch processing job.

    A job contains one or more cases to be processed.
    """

    __tablename__ = "jobs"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # User who created this job
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)

    # Status
    status = Column(
        SQLEnum(JobStatus),
        default=JobStatus.QUEUED,
        nullable=False,
        index=True,
    )

    # Counts
    case_count = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)

    # Celery task ID
    celery_task_id = Column(String(255), nullable=True)

    # Source info
    source_type = Column(String(50))  # "file" or "text"
    source_filename = Column(String(255), nullable=True)

    # Pipeline snapshot — frozen layer config at job start (deterministic execution)
    pipeline_snapshot = Column(PortableJSON, nullable=True)

    # Model used for this job
    model_name = Column(String(200), nullable=True)

    # Replay provenance — links replay jobs back to their source
    replay_source_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Regression baseline flag — admin marks golden jobs for regression testing
    is_regression_baseline = Column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)  # Soft delete

    # Relationships
    cases = relationship("JobCase", back_populates="job", cascade="all, delete-orphan")
    metrics = relationship("LayerMetric", back_populates="job", cascade="all, delete-orphan")
    user = relationship("User", back_populates="jobs")

    # Indexes
    __table_args__ = (
        Index("ix_jobs_created_at", "created_at"),
        Index("ix_jobs_status_deleted", "status", "deleted_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "job_id": str(self.id),
            "status": self.status.value,
            "case_count": self.case_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "source_type": self.source_type,
            "source_filename": self.source_filename,
            "model_name": self.model_name,
            "replay_source_id": str(self.replay_source_id) if self.replay_source_id else None,
            "is_regression_baseline": self.is_regression_baseline,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "completed_at": self.completed_at.isoformat() + "Z" if self.completed_at else None,
            "pipeline_snapshot": self.pipeline_snapshot,
        }


class JobCase(Base):
    """
    JobCase model - represents a single case within a job.

    Stores input text and all layer outputs.
    """

    __tablename__ = "job_cases"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Foreign key
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)

    # Case identifier (e.g., "Case #1")
    case_number = Column(Integer, nullable=False)
    case_label = Column(String(100), nullable=True)

    # Source file (for multi-file uploads)
    source_file = Column(String(255), nullable=True)

    # Status
    status = Column(
        SQLEnum(CaseStatus),
        default=CaseStatus.QUEUED,
        nullable=False,
        index=True,
    )

    # Input
    input_text = Column(Text, nullable=False)

    # Layer outputs (JSONB on PostgreSQL, JSON on SQLite)
    layer1_output = Column(PortableJSON, nullable=True)
    layer2_output = Column(PortableJSON, nullable=True)
    layer3_output = Column(PortableJSON, nullable=True)

    # Dynamic layer outputs (stores outputs for custom user layers)
    extra_layer_outputs = Column(PortableJSON, nullable=True)  # { "layer_name": {...output...} }

    # Raw LLM outputs (preserve streaming text for review)
    layer1_raw_output = Column(Text, nullable=True)
    layer2_raw_output = Column(Text, nullable=True)
    layer3_raw_output = Column(Text, nullable=True)
    extra_layer_raw_outputs = Column(PortableJSON, nullable=True)  # { "layer_name": "raw text" }

    # Final results
    final_verdict = Column(String(50), nullable=True)
    final_cci = Column(Float, nullable=True)

    # Processing info
    total_duration_ms = Column(Integer, nullable=True)
    total_tokens_input = Column(Integer, nullable=True)
    total_tokens_output = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    # Versioning
    prompt_version = Column(String(20), nullable=True)
    model_version = Column(String(100), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

    # Relationships
    job = relationship("Job", back_populates="cases")

    # Indexes
    __table_args__ = (
        Index("ix_job_cases_job_status", "job_id", "status"),
    )

    def to_dict(self, include_outputs: bool = True) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
            "case_id": str(self.id),
            "case_number": self.case_number,
            "case_label": self.case_label,
            "source_file": self.source_file,
            "status": self.status.value,
            "input_text": self.input_text,
            "final_verdict": self.final_verdict,
            "final_cci": self.final_cci,
            "total_duration_ms": self.total_duration_ms,
            "error_message": self.error_message,
            "processed_at": self.processed_at.isoformat() + "Z" if self.processed_at else None,
        }

        if include_outputs:
            result.update({
                "layer1_output": self.layer1_output,
                "layer2_output": self.layer2_output,
                "layer3_output": self.layer3_output,
                "extra_layer_outputs": self.extra_layer_outputs,
                "layer1_raw_output": self.layer1_raw_output,
                "layer2_raw_output": self.layer2_raw_output,
                "layer3_raw_output": self.layer3_raw_output,
                "extra_layer_raw_outputs": self.extra_layer_raw_outputs,
                "prompt_version": self.prompt_version,
                "model_version": self.model_version,
                "total_tokens_input": self.total_tokens_input,
                "total_tokens_output": self.total_tokens_output,
            })

        return result


class AuditLog(Base):
    """
    AuditLog model - immutable audit trail.

    Records all significant operations for compliance.
    """

    __tablename__ = "audit_logs"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Request tracking
    request_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # References
    job_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    case_id = Column(UUID(as_uuid=True), nullable=True)

    # Action
    action = Column(String(100), nullable=False, index=True)
    layer = Column(String(20), nullable=True)  # CTP, CIE, CCC

    # Details
    details = Column(PortableJSON, nullable=True)

    # Processing info
    duration_ms = Column(Integer, nullable=True)
    tokens_input = Column(Integer, nullable=True)
    tokens_output = Column(Integer, nullable=True)

    # Versioning
    prompt_version = Column(String(20), nullable=True)
    model_version = Column(String(100), nullable=True)

    # Timestamp (immutable)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Indexes
    __table_args__ = (
        Index("ix_audit_logs_job_action", "job_id", "action"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": str(self.id),
            "request_id": str(self.request_id) if self.request_id else None,
            "job_id": str(self.job_id) if self.job_id else None,
            "case_id": str(self.case_id) if self.case_id else None,
            "action": self.action,
            "layer": self.layer,
            "details": self.details,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp.isoformat() + "Z" if self.timestamp else None,
        }


class PromptTemplate(Base):
    """
    PromptTemplate model - stores prompt templates per layer.

    Supports both built-in layers (layer1_ctp, layer2_cie, layer3_ccc)
    and user-created custom layers. Each layer can have multiple versions.
    """

    __tablename__ = "prompt_templates"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Layer identification
    layer_name = Column(String(50), nullable=False, index=True)  # e.g. 'layer1_ctp' or custom name
    label = Column(String(200), nullable=True)  # Human-friendly label

    # Prompt content
    content = Column(Text, nullable=False)

    # Versioning
    version = Column(String(20), default="custom")
    is_active = Column(Boolean, default=True, nullable=False)

    # Custom layer support
    is_builtin = Column(Boolean, default=True, nullable=False)  # False for user-created layers
    display_order = Column(Integer, default=99, nullable=False)  # Ordering in the UI
    is_default_prompt = Column(Boolean, default=False, nullable=False)  # Whether this is the default for the layer

    # Active version tracking (NULL = use current content, else use version content)
    active_version_id = Column(UUID(as_uuid=True), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    versions = relationship("PromptVersion", back_populates="template", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_prompt_templates_layer_active", "layer_name", "is_active"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": str(self.id),
            "layer_name": self.layer_name,
            "label": self.label,
            "content": self.content,
            "version": self.version,
            "is_active": self.is_active,
            "is_builtin": self.is_builtin,
            "display_order": self.display_order,
            "is_default_prompt": self.is_default_prompt,
            "active_version_id": str(self.active_version_id) if self.active_version_id else None,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "version_count": len(self.versions) if self.versions else 0,
        }


class PromptVersion(Base):
    """
    PromptVersion model - stores historical versions of prompt templates.

    Keeps last N versions for rollback capability.
    """

    __tablename__ = "prompt_versions"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Foreign key to template
    template_id = Column(UUID(as_uuid=True), ForeignKey("prompt_templates.id"), nullable=False, index=True)

    # Version content snapshot
    content = Column(Text, nullable=False)
    version_label = Column(String(50), nullable=True)  # e.g., "v1.3", "backup-2024-02"

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)  # For future user tracking

    # Relationship
    template = relationship("PromptTemplate", back_populates="versions")

    __table_args__ = (
        Index("ix_prompt_versions_template_created", "template_id", "created_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": str(self.id),
            "template_id": str(self.template_id),
            "content": self.content,
            "version_label": self.version_label,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class LayerMetric(Base):
    """
    Per-layer execution metrics.

    One row per layer per case per job.  Written once at layer_complete —
    never during token streaming, so it does not slow the pipeline.
    """

    __tablename__ = "layer_metrics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)
    case_number = Column(Integer, nullable=False)
    layer_id = Column(String(100), nullable=False)
    duration_ms = Column(Integer, nullable=False, default=0)
    success = Column(Boolean, nullable=False, default=True)
    error_type = Column(String(50), nullable=True)  # LLM_ERROR, PARSE_ERROR, TIMEOUT, SCHEMA_ERROR, UNKNOWN
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    job = relationship("Job", back_populates="metrics")

    __table_args__ = (
        Index("ix_layer_metrics_job_layer", "job_id", "layer_id"),
        Index("ix_layer_metrics_layer", "layer_id"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "case_number": self.case_number,
            "layer_id": self.layer_id,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class SavedCase(Base):
    """
    SavedCase model — immutable snapshot of a completed pipeline case.

    Stores a deep copy of the full results at save time. The snapshot MUST
    remain frozen even if the pipeline or prompts change later.

    Security: users can only access their own saved cases.
    """

    __tablename__ = "saved_cases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    job_id = Column(UUID(as_uuid=True), nullable=True)
    case_id = Column(UUID(as_uuid=True), nullable=True)
    name = Column(String(300), nullable=False)
    input_text = Column(Text, nullable=False)
    results_snapshot = Column(PortableJSON, nullable=True)  # Immutable deep copy
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)  # Soft delete only

    # Relationships
    user = relationship("User")

    # Indexes are created by migration SQL with IF NOT EXISTS (see main.py _run_schema_migrations)

    def to_dict(self, include_preview: bool = True) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "job_id": str(self.job_id) if self.job_id else None,
            "case_id": str(self.case_id) if self.case_id else None,
            "name": self.name,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
        if include_preview and self.input_text:
            result["input_preview"] = self.input_text[:100] + ("…" if len(self.input_text) > 100 else "")
        return result

    def to_full_dict(self) -> dict[str, Any]:
        """Full response including input_text and results_snapshot."""
        d = self.to_dict(include_preview=False)
        d["input_text"] = self.input_text
        d["results_snapshot"] = self.results_snapshot
        return d

