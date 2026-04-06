"""
SNAP-AI FastAPI Application

Main entry point for the clinical NLP pipeline API.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.utils import setup_logging, get_logger
from app.db import Base
from app.db import session as db_session

# Initialize logging
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    import asyncio
    settings = get_settings()

    # SECURITY: Block startup if JWT_SECRET is not set in production
    if settings.jwt_secret.startswith("CHANGE-ME"):
        if settings.environment == "production":
            raise RuntimeError(
                "JWT_SECRET must be set in environment for production. "
                "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        else:
            logger.warning("jwt_secret_is_placeholder", hint="Set JWT_SECRET env var before deploying")

    # Wait for database to be ready (up to 30s)
    for attempt in range(1, 7):
        try:
            Base.metadata.create_all(bind=db_session.engine, checkfirst=True)
            logger.info("database_tables_created")
            break
        except Exception as e:
            logger.warning(
                "database_not_ready",
                attempt=attempt,
                error=str(e),
            )
            if attempt == 6:
                logger.error("database_connection_failed_after_retries")
                raise
            await asyncio.sleep(5)

    # Create default admin user if not exists
    try:
        _create_default_admin()
    except Exception as e:
        logger.error("create_admin_failed_at_startup", error=str(e))

    # Run schema migrations for any new columns
    try:
        _run_schema_migrations()
    except Exception as e:
        logger.error("schema_migration_failed", error=str(e))

    logger.info(
        "starting_application",
        environment=settings.environment,
        llm_backend=settings.llm_backend,
        llm_model=settings.llm_model,
    )
    yield
    logger.info("shutting_down_application")


def _create_default_admin():
    """Create the default admin user if it doesn't exist."""
    from app.db.models import User, UserRole
    from passlib.context import CryptContext

    settings = get_settings()
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    db = db_session.SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "admin").first()
        if not existing:
            admin = User(
                username="admin",
                name="SNAP-AI Administrator",
                password_hash=pwd_context.hash(settings.admin_password),
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(admin)
            db.commit()
            logger.info("default_admin_created", username="admin")
        else:
            logger.info("default_admin_exists")
    except Exception as e:
        logger.error("create_admin_failed", error=str(e))
        db.rollback()
    finally:
        db.close()


def _run_schema_migrations():
    """Add any columns that exist in models but not yet in the database.

    Uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS so it's safe to run
    on every startup.
    """
    from sqlalchemy import text

    migrations = [
        # prompt_templates — custom layer support
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS is_builtin BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS display_order INTEGER NOT NULL DEFAULT 99",
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS is_default_prompt BOOLEAN NOT NULL DEFAULT FALSE",
        # jobs — ownership + lifecycle tracking
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_id UUID NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR(255) NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) NULL DEFAULT 'text'",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_filename VARCHAR(500) NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completed_count INTEGER NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_count INTEGER NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE",
        # job_cases — full result storage
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS case_label VARCHAR(200) NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS input_text TEXT NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer1_output JSONB NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer2_output JSONB NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer3_output JSONB NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer1_raw_output TEXT NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer2_raw_output TEXT NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer3_raw_output TEXT NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS extra_layer_outputs JSONB NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS extra_layer_raw_outputs JSONB NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS final_verdict VARCHAR(100) NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS final_cci INTEGER NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS total_duration_ms INTEGER NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS total_tokens_input INTEGER NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS total_tokens_output INTEGER NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS error_message TEXT NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS prompt_version VARCHAR(50) NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS model_version VARCHAR(200) NULL",
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP NULL",
        # job_cases — source file tracking for multi-file uploads
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS source_file VARCHAR(255) NULL",
        # jobs — pipeline snapshot for deterministic execution
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pipeline_snapshot JSONB NULL",
        # job_cases — per-layer stream capture for download fidelity
        "ALTER TABLE job_cases ADD COLUMN IF NOT EXISTS layer_stream_data JSONB NULL",
        # jobs — model name + replay provenance (Stage 4)
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS model_name VARCHAR(200) NULL",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS replay_source_id UUID NULL",
        # jobs — regression baseline flag
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS is_regression_baseline BOOLEAN NOT NULL DEFAULT FALSE",
        # jobs — soft delete (replaces is_deleted column)
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL",
        # prompt_templates — active version for multi-version prompts
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS active_version_id UUID NULL",
        # saved_cases table (created by create_all, but ensure columns exist)
        """CREATE TABLE IF NOT EXISTS saved_cases (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id),
            job_id UUID NULL,
            case_id UUID NULL,
            name VARCHAR(300) NOT NULL,
            input_text TEXT NOT NULL,
            results_snapshot JSONB NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NULL,
            deleted_at TIMESTAMP NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_saved_cases_user_id ON saved_cases (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_saved_cases_user_deleted ON saved_cases (user_id, deleted_at)",
    ]

    db = db_session.SessionLocal()
    try:
        for sql in migrations:
            db.execute(text(sql))
        db.commit()
        logger.info("schema_migrations_applied", count=len(migrations))
    except Exception as e:
        logger.error("schema_migration_error", error=str(e))
        db.rollback()
        raise
    finally:
        db.close()

    # --- Enum value additions (must run outside a transaction) ---
    # ALTER TYPE ... ADD VALUE cannot be run inside a transaction block in PostgreSQL.
    # We use a raw AUTOCOMMIT connection for these.
    try:
        engine = db_session.engine
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Add 'cancelled' to jobstatus enum if not already present
            exists = conn.execute(text(
                "SELECT 1 FROM pg_enum "
                "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
                "WHERE pg_type.typname = 'jobstatus' AND pg_enum.enumlabel = 'cancelled'"
            )).fetchone()
            if not exists:
                conn.execute(text("ALTER TYPE jobstatus ADD VALUE 'cancelled'"))
                logger.info("enum_value_added", type="jobstatus", value="cancelled")

            # Add 'stopping' to jobstatus enum if not already present
            exists_stopping = conn.execute(text(
                "SELECT 1 FROM pg_enum "
                "JOIN pg_type ON pg_enum.enumtypid = pg_type.oid "
                "WHERE pg_type.typname = 'jobstatus' AND pg_enum.enumlabel = 'stopping'"
            )).fetchone()
            if not exists_stopping:
                conn.execute(text("ALTER TYPE jobstatus ADD VALUE 'stopping'"))
                logger.info("enum_value_added", type="jobstatus", value="stopping")
    except Exception as e:
        # Non-fatal: if enum already has the value or type name differs, log and continue
        logger.warning("enum_migration_warning", error=str(e))


# Create FastAPI app
app = FastAPI(
    title="SNAP-AI",
    description="Clinical NLP Pipeline for German Discharge Summaries",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware — restrict to tunnel origin in production
_cors_settings = get_settings()
if _cors_settings.environment == "production" and _cors_settings.cors_origins == "*":
    # In production, default to allowing only same-origin requests
    cors_origins = []
    logger.warning("cors_wildcard_in_production", hint="Set CORS_ORIGINS to your domain")
else:
    cors_origins = [o.strip() for o in _cors_settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=("*" not in cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add unique request ID and audit logging for each request."""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    # Log request
    logger.info(
        "request_started",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )

    response = await call_next(request)

    # Add request ID to response headers
    response.headers["X-Request-ID"] = request_id

    # Audit logging — capture user, action, resource for mutating requests
    user_id = getattr(request.state, "user_id", None)
    username = getattr(request.state, "username", None)
    path = request.url.path
    method = request.method

    is_mutating = method in ("POST", "PUT", "DELETE", "PATCH")
    is_api = path.startswith("/api/v1/")
    skip_paths = ("/api/v1/auth/", "/health", "/docs")
    should_audit = is_mutating and is_api and not any(path.startswith(s) for s in skip_paths)

    if should_audit:
        logger.info(
            "audit_action",
            request_id=request_id,
            user_id=user_id,
            username=username,
            method=method,
            path=path,
            status_code=response.status_code,
        )
    else:
        logger.info(
            "request_completed",
            request_id=request_id,
            status_code=response.status_code,
        )
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Authentication middleware.
    Protects all /api/v1/ routes except /auth/, /health, and /api/v1/info.
    """
    path = request.url.path

    # Public routes that don't need auth
    # Note: /api/v1/stream/ is public because EventSource API cannot send
    # Authorization headers. Job UUIDs are unguessable and serve as tokens.
    public_paths = [
        "/health",
        "/api/v1/auth/",
        "/api/v1/info",
        "/api/v1/system/health",
        "/api/v1/stream/",
        "/api/v1/logs/stream",
        "/docs",
        "/openapi.json",
    ]

    is_public = any(path.startswith(p) for p in public_paths)

    if is_public or not path.startswith("/api/v1/"):
        return await call_next(request)

    # Check JWT token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": {"code": "UNAUTHORIZED", "message": "Authentication required"},
                "data": None,
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            },
        )

    from app.api.routes.auth import verify_token
    token = auth_header.split(" ", 1)[1]
    payload = verify_token(token)

    if not payload:
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": {"code": "TOKEN_INVALID", "message": "Invalid or expired token"},
                "data": None,
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            },
        )

    # Store user info in request state for downstream use
    request.state.user_id = payload.get("sub")
    request.state.username = payload.get("username")
    request.state.user_role = payload.get("role")

    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions with standardized response."""
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": f"HTTP_{exc.status_code}",
                "message": exc.detail,
            },
            "meta": {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "request_id": request_id,
            },
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    import traceback
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "unhandled_exception",
        request_id=request_id,
        path=request.url.path,
        method=request.method,
        error=str(exc),
        traceback=traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred",
            },
            "meta": {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "request_id": request_id,
            },
        },
    )


def create_response(
    success: bool,
    data: Any = None,
    error: dict | None = None,
    request_id: str | None = None,
) -> dict:
    """
    Create standardized API response.

    Args:
        success: Whether the request succeeded
        data: Response data (if successful)
        error: Error details (if failed)
        request_id: Request tracking ID

    Returns:
        Standardized response dictionary
    """
    return {
        "success": success,
        "data": data,
        "error": error,
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id,
        },
    }


# ============================================
# Include API Routers
# ============================================

from app.api.routes.upload import router as upload_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.settings import router as settings_router
from app.api.routes.stream import router as stream_router
from app.api.routes.prompts import router as prompts_router
from app.api.routes.models import router as models_router
from app.api.routes.system import router as system_router
from app.api.routes.auth import router as auth_router
from app.api.routes.logs import router as logs_router
from app.api.routes.saved_cases import router as saved_cases_router
from app.api.routes.downloads import router as downloads_router

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(jobs_router)
app.include_router(settings_router)
app.include_router(stream_router)
app.include_router(prompts_router)
app.include_router(models_router)
app.include_router(system_router)
app.include_router(logs_router)
app.include_router(saved_cases_router)
app.include_router(downloads_router)


# ============================================
# Health Check Endpoints
# ============================================


@app.get("/health", tags=["Health"])
async def health_check():
    """Basic health check endpoint."""
    return {"status": "healthy"}


@app.get("/health/ready", tags=["Health"])
async def readiness_check():
    """
    Readiness check - verifies all dependencies are available.

    Returns service status for each dependency.
    """
    settings = get_settings()

    # Check database
    db_status = "healthy"
    try:
        from sqlalchemy import text
        from app.db import SessionLocal
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
    except Exception:
        db_status = "unhealthy"

    # Check Redis
    redis_status = "pending"
    try:
        import redis
        r = redis.from_url(settings.redis_url)
        r.ping()
        redis_status = "healthy"
    except Exception:
        redis_status = "unhealthy"

    # Check LLM (auto-selects backend based on config)
    llm_status = "pending"
    try:
        from app.llm import get_llm_client
        client = get_llm_client()
        healthy = await client.health_check()
        llm_status = "healthy" if healthy else "unhealthy"
    except Exception:
        llm_status = "unhealthy"

    overall = "ready" if all(
        s == "healthy" for s in [db_status, redis_status, llm_status]
    ) else "degraded"

    return {
        "status": overall,
        "services": {
            "api": "healthy",
            "database": db_status,
            "redis": redis_status,
            "llm": llm_status,
        },
        "config": {
            "environment": settings.environment,
            "llm_backend": settings.llm_backend,
            "llm_model": settings.llm_model,
        },
    }


@app.get("/health/vllm", tags=["Health"])
async def vllm_health_check():
    """
    Check vLLM model server availability.

    Returns model info if online, error details if offline.
    Frontend uses this to display "Model offline" banner.
    """
    settings = get_settings()
    vllm_url = f"{settings.vllm_host}/v1/models"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(vllm_url)
            resp.raise_for_status()
            models_data = resp.json()
            model_ids = [m.get("id", "unknown") for m in models_data.get("data", [])]
            return {
                "status": "online",
                "host": settings.vllm_host,
                "models": model_ids,
            }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "offline",
                "host": settings.vllm_host,
                "error": str(e),
                "hint": "Start the GPU model service: ./start-vllm.sh",
            },
        )


# ============================================
# API Info Endpoint
# ============================================


@app.get("/api/v1/info", tags=["Info"])
async def api_info(request: Request):
    """Get API information and configuration."""
    settings = get_settings()

    return create_response(
        success=True,
        data={
            "name": "SNAP-AI",
            "version": "1.0.0",
            "description": "Clinical NLP Pipeline for German Discharge Summaries",
            "environment": settings.environment,
            "llm": {
                "backend": settings.llm_backend,
                "model": settings.llm_model,
            },
            "prompt_version": settings.prompt_version,
        },
        request_id=getattr(request.state, "request_id", None),
    )
