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
    settings = get_settings()

    # Create database tables
    Base.metadata.create_all(bind=db_session.engine)
    logger.info("database_tables_created")

    logger.info(
        "starting_application",
        environment=settings.environment,
        llm_backend=settings.llm_backend,
        llm_model=settings.llm_model,
    )
    yield
    logger.info("shutting_down_application")


# Create FastAPI app
app = FastAPI(
    title="SNAP-AI",
    description="Clinical NLP Pipeline for German Discharge Summaries",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware (allow all origins for remote access)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add unique request ID to each request for tracking."""
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

    logger.info(
        "request_completed",
        request_id=request_id,
        status_code=response.status_code,
    )

    return response


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
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "unhandled_exception",
        request_id=request_id,
        error=str(exc),
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

app.include_router(upload_router)
app.include_router(jobs_router)
app.include_router(settings_router)
app.include_router(stream_router)
app.include_router(prompts_router)
app.include_router(models_router)
app.include_router(system_router)


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
        from app.db import SessionLocal
        db = SessionLocal()
        db.execute("SELECT 1")
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

    # Check LLM
    llm_status = "pending"
    try:
        from app.llm import OllamaClient
        client = OllamaClient()
        import asyncio
        loop = asyncio.new_event_loop()
        healthy = loop.run_until_complete(client.health_check())
        loop.close()
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
