"""
Logs API routes.

Provides access to application logs for debugging and monitoring.
Supports both polling and SSE live streaming.
"""

import asyncio
import json
import logging
import os
import subprocess
from collections import deque
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Query
from fastapi.responses import StreamingResponse

from app.config import get_settings

# NOTE: import get_logger AFTER setting up the handler below
settings = get_settings()

router = APIRouter(prefix="/api/v1/logs", tags=["Logs"])

# In-memory log buffer (last 1000 entries)
_log_buffer: deque = deque(maxlen=1000)


class InMemoryLogHandler(logging.Handler):
    """
    Python logging handler that captures log records into the in-memory buffer.
    This ensures the logs page actually has data to show.
    """

    def emit(self, record):
        try:
            entry = {
                "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
                "level": record.levelname,
                "message": self.format(record)[:500],
                "component": getattr(record, "name", "app").replace("app.", ""),
            }
            _log_buffer.append(entry)
        except Exception:
            pass


# Install the handler on the root logger so ALL app logs are captured
_log_handler = InMemoryLogHandler()
_log_handler.setLevel(logging.INFO)
_log_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_handler)
# Also capture structlog output (which goes through PrintLogger → stdout)
logging.getLogger("structlog").addHandler(_log_handler)

from app.utils import get_logger
logger = get_logger(__name__)


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


def add_log_entry(level: str, message: str, component: str = "app", details: dict = None):
    """Add a log entry to the in-memory buffer."""
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "message": message,
        "component": component,
        "details": details,
    }
    _log_buffer.append(entry)
    return entry


@router.get("")
async def get_logs(
    request: Request,
    level: Optional[str] = Query(None, description="Filter by level: INFO, WARNING, ERROR"),
    component: Optional[str] = Query(None, description="Filter by component"),
    limit: int = Query(100, ge=1, le=1000),
    search: Optional[str] = Query(None, description="Search in log messages"),
):
    """
    Get recent application logs.
    
    Also fetches recent Docker container logs if available.
    """
    request_id = getattr(request.state, "request_id", None)

    # Start with in-memory logs
    logs = list(_log_buffer)

    # Try to get docker logs for richer information
    try:
        for container in ["snapai-backend", "snapai-worker", "snapai-vllm"]:
            result = subprocess.run(
                ["docker", "logs", "--tail", "50", "--timestamps", container],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout:
                for line in result.stdout.strip().split("\n")[-20:]:
                    if line.strip():
                        log_level = "INFO"
                        if "error" in line.lower() or "ERROR" in line:
                            log_level = "ERROR"
                        elif "warn" in line.lower() or "WARNING" in line:
                            log_level = "WARNING"
                        logs.append({
                            "timestamp": datetime.utcnow().isoformat() + "Z",
                            "level": log_level,
                            "message": line.strip()[:500],
                            "component": container.replace("snapai-", ""),
                        })
    except Exception:
        pass  # Docker may not be available

    # Apply filters
    if level:
        logs = [l for l in logs if l.get("level", "").upper() == level.upper()]
    if component:
        logs = [l for l in logs if component.lower() in l.get("component", "").lower()]
    if search:
        logs = [l for l in logs if search.lower() in l.get("message", "").lower()]

    # Sort by timestamp descending and limit
    logs = sorted(logs, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]

    return create_response(
        success=True,
        data={"logs": logs, "total": len(logs)},
        request_id=request_id,
    )


@router.get("/stream")
async def stream_logs(request: Request):
    """
    Live log stream via Server-Sent Events (SSE).
    
    Streams new log entries in real-time.
    """
    async def event_generator():
        last_count = len(_log_buffer)
        
        # Send initial heartbeat
        yield f"data: {json.dumps({'type': 'connected', 'message': 'Log stream connected'})}\n\n"

        while True:
            try:
                current_count = len(_log_buffer)
                if current_count > last_count:
                    # Send new log entries
                    new_logs = list(_log_buffer)[last_count - current_count:]
                    for log_entry in new_logs:
                        yield f"data: {json.dumps({'type': 'log', **log_entry})}\n\n"
                    last_count = current_count

                # Send heartbeat every 10 seconds
                yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.utcnow().isoformat() + 'Z'})}\n\n"
                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/docker/{container}")
async def get_docker_logs(
    container: str,
    request: Request,
    tail: int = Query(100, ge=1, le=500),
):
    """Get logs from a specific Docker container."""
    request_id = getattr(request.state, "request_id", None)
    
    allowed_containers = ["snapai-backend", "snapai-worker", "snapai-vllm", "snapai-postgres", "snapai-redis"]
    full_name = f"snapai-{container}" if not container.startswith("snapai-") else container
    
    if full_name not in allowed_containers:
        return create_response(
            success=False,
            error={"message": f"Container not allowed. Use: {', '.join(allowed_containers)}"},
            request_id=request_id,
        )

    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), "--timestamps", full_name],
            capture_output=True, text=True, timeout=10,
        )
        
        lines = []
        output = result.stdout + result.stderr
        for line in output.strip().split("\n"):
            if line.strip():
                lines.append(line.strip())

        return create_response(
            success=True,
            data={"container": full_name, "lines": lines, "count": len(lines)},
            request_id=request_id,
        )
    except subprocess.TimeoutExpired:
        return create_response(
            success=False,
            error={"message": "Timeout fetching container logs"},
            request_id=request_id,
        )
    except FileNotFoundError:
        return create_response(
            success=False,
            error={"message": "Docker CLI not available"},
            request_id=request_id,
        )
