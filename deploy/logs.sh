#!/bin/bash
# =============================================================
# SNAP-AI — View Logs
# =============================================================
# Tail logs from all services.
#
# Usage:
#   bash deploy/logs.sh             # all services
#   bash deploy/logs.sh backend     # specific service
#   bash deploy/logs.sh vllm        # vLLM host process
# =============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SERVICE="${1:-all}"

case "$SERVICE" in
    vllm)
        echo "=== vLLM Logs ==="
        if [ -f "data/logs/vllm.log" ]; then
            tail -100f data/logs/vllm.log
        else
            echo "No vLLM log file. Check: journalctl -u snapai-vllm -f"
            journalctl -u snapai-vllm -f 2>/dev/null || echo "journalctl not available"
        fi
        ;;
    backend)
        docker logs snapai-backend -f --tail 100
        ;;
    worker)
        docker logs snapai-worker -f --tail 100
        ;;
    postgres)
        docker logs snapai-postgres -f --tail 50
        ;;
    redis)
        docker logs snapai-redis -f --tail 50
        ;;
    tunnel|cloudflared)
        docker logs snapai-cloudflared -f --tail 50
        ;;
    all)
        docker compose -f docker-compose.prod.yml logs -f --tail 50
        ;;
    *)
        echo "Unknown service: $SERVICE"
        echo "Options: all, backend, worker, postgres, redis, vllm, tunnel"
        exit 1
        ;;
esac
