"""
Stream Manager for real-time LLM token broadcasting.

Uses Redis Pub/Sub to broadcast tokens from Celery workers
to the SSE endpoint for real-time display in the UI.
"""

import json
from typing import AsyncGenerator

import redis.asyncio as aioredis
import redis as sync_redis

from app.config import get_settings
from app.utils import get_logger

logger = get_logger(__name__)


def get_channel_name(job_id: str) -> str:
    """Get the Redis pub/sub channel name for a job."""
    return f"snapai:stream:{job_id}"


class StreamPublisher:
    """
    Synchronous publisher for use in Celery workers.

    Publishes token events to Redis pub/sub channels.
    """

    def __init__(self):
        settings = get_settings()
        self._redis = sync_redis.from_url(settings.redis_url, decode_responses=True)

    def publish_token(
        self,
        job_id: str,
        case_number: int,
        layer: str,
        token: str,
    ) -> None:
        """Publish a single token to the stream."""
        event = {
            "type": "token",
            "job_id": job_id,
            "case_number": case_number,
            "layer": layer,
            "token": token,
        }
        channel = get_channel_name(job_id)
        self._redis.publish(channel, json.dumps(event))

    def publish_layer_start(
        self,
        job_id: str,
        case_number: int,
        layer: str,
    ) -> None:
        """Publish a layer start event."""
        event = {
            "type": "layer_start",
            "job_id": job_id,
            "case_number": case_number,
            "layer": layer,
        }
        channel = get_channel_name(job_id)
        self._redis.publish(channel, json.dumps(event))

    def publish_layer_complete(
        self,
        job_id: str,
        case_number: int,
        layer: str,
        success: bool,
        duration_ms: int = 0,
    ) -> None:
        """Publish a layer completion event."""
        event = {
            "type": "layer_complete",
            "job_id": job_id,
            "case_number": case_number,
            "layer": layer,
            "success": success,
            "duration_ms": duration_ms,
        }
        channel = get_channel_name(job_id)
        self._redis.publish(channel, json.dumps(event))

    def publish_case_complete(
        self,
        job_id: str,
        case_number: int,
        success: bool,
        verdict: str | None = None,
        cci: float | None = None,
    ) -> None:
        """Publish a case completion event."""
        event = {
            "type": "case_complete",
            "job_id": job_id,
            "case_number": case_number,
            "success": success,
            "verdict": verdict,
            "cci": cci,
        }
        channel = get_channel_name(job_id)
        self._redis.publish(channel, json.dumps(event))

    def close(self):
        """Close the Redis connection."""
        self._redis.close()


class StreamSubscriber:
    """
    Async subscriber for the SSE endpoint.

    Subscribes to Redis pub/sub channels and yields events.
    """

    def __init__(self):
        settings = get_settings()
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def subscribe(
        self,
        job_id: str,
        timeout: int = 3600,
    ) -> AsyncGenerator[dict, None]:
        """
        Subscribe to stream events for a job.

        Yields event dicts as they arrive via Redis pub/sub.

        Args:
            job_id: Job ID to subscribe to
            timeout: Maximum time to listen (seconds)

        Yields:
            Event dicts with type, token, layer, etc.
        """
        channel_name = get_channel_name(job_id)
        pubsub = self._redis.pubsub()

        try:
            await pubsub.subscribe(channel_name)
            logger.info("stream_subscribed", job_id=job_id, channel=channel_name)

            import asyncio
            start_time = asyncio.get_event_loop().time()

            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    logger.info("stream_timeout", job_id=job_id)
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if message and message["type"] == "message":
                    try:
                        event = json.loads(message["data"])
                        yield event

                        # Stop on job-level completion
                        if event.get("type") == "job_complete":
                            break
                    except json.JSONDecodeError:
                        continue
                else:
                    # Yield heartbeat to keep SSE alive
                    yield {"type": "heartbeat"}

        except Exception as e:
            logger.error("stream_subscribe_error", job_id=job_id, error=str(e))
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.close()

    async def close(self):
        """Close the Redis connection."""
        await self._redis.close()
