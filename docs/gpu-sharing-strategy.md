# GPU Sharing Strategy — SNAP-AI

## Problem

On shared GPU clusters (e.g., DGX A100), model servers like vLLM occupy GPU memory
permanently. Other researchers cannot use the GPUs while SNAP-AI holds them idle.

## Solution: Idle Timeout Model Server

```
Request arrives
→ check model server status
→ if not running → start vLLM subprocess
→ run inference
→ reset idle timer (10 minutes)
→ after 10 min idle → stop model server → free GPU
```

### Advantages

- **Avoids repeated model loading**: Consecutive requests within the timeout window
  reuse the already-loaded model — no delay.
- **Faster inference for bursts**: When doctors process multiple cases in a session,
  the model stays warm for the entire batch.
- **GPUs freed during idle**: After 10 minutes without requests, GPU memory is released
  for other researchers.

## Implementation Architecture

### ModelServerManager (Singleton)

```python
class ModelServerManager:
    """Manages on-demand vLLM process lifecycle."""

    def __init__(self, idle_timeout_seconds=600):
        self.process = None
        self.idle_timer = None
        self.idle_timeout = idle_timeout_seconds

    async def ensure_running(self):
        """Start vLLM if not already running."""
        if self.process is None or self.process.poll() is not None:
            self.process = subprocess.Popen([...])
            await self._wait_for_health()
        self._reset_timer()

    def _reset_timer(self):
        """Reset the idle timeout."""
        if self.idle_timer:
            self.idle_timer.cancel()
        self.idle_timer = threading.Timer(self.idle_timeout, self._stop_server)
        self.idle_timer.start()

    def _stop_server(self):
        """Gracefully stop vLLM server."""
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=30)
            self.process = None
```

### Integration Point

In `app/workers/tasks.py`, before LLM inference:

```python
manager = ModelServerManager()
await manager.ensure_running()
# ... proceed with inference ...
```

## GPU Memory Safety

The system already includes GPU memory monitoring via `nvidia-smi`:
- If any GPU exceeds 90% memory usage, new jobs are rejected
- UI shows: "GPU resources currently busy. Please retry shortly."

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `GPU_IDLE_TIMEOUT` | 600s (10 min) | Time before stopping idle model server |
| `GPU_MEMORY_THRESHOLD` | 0.90 | Memory usage ratio to reject new jobs |

## Status

This is an **architecture document** for future implementation. The GPU memory
safety check (reject jobs when memory >90%) is already implemented in the
`/api/v1/system/info` endpoint and enforced in the frontend.
