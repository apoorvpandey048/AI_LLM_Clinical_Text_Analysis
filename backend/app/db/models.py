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
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship

import enum

Base = declarative_base()


class JobStatus(str, enum.Enum):
    """Job processing status."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class CaseStatus(str, enum.Enum):
    """Individual case processing status."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(Base):
    """
    Job model - represents a batch processing job.

    A job contains one or more cases to be processed.
    """

    __tablename__ = "jobs"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

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

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)  # Soft delete

    # Relationships
    cases = relationship("JobCase", back_populates="job", cascade="all, delete-orphan")

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
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "completed_at": self.completed_at.isoformat() + "Z" if self.completed_at else None,
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

    # Status
    status = Column(
        SQLEnum(CaseStatus),
        default=CaseStatus.QUEUED,
        nullable=False,
        index=True,
    )

    # Input
    input_text = Column(Text, nullable=False)

    # Layer outputs (JSONB for efficient querying)
    layer1_output = Column(JSONB, nullable=True)
    layer2_output = Column(JSONB, nullable=True)
    layer3_output = Column(JSONB, nullable=True)

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
            "status": self.status.value,
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
    details = Column(JSONB, nullable=True)

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
    PromptTemplate model - stores custom prompt templates per layer.

    Allows doctors to customize the prompts used for each pipeline layer.
    """

    __tablename__ = "prompt_templates"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Layer identification
    layer_name = Column(String(50), nullable=False, index=True)  # e.g. 'layer1_ctp'
    label = Column(String(200), nullable=True)  # Human-friendly label

    # Prompt content
    content = Column(Text, nullable=False)

    # Versioning
    version = Column(String(20), default="custom")
    is_active = Column(Boolean, default=True, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
