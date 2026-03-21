#!/bin/bash
# =============================================================
# SNAP-AI — Stop Everything (NO SUDO)
# =============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Stopping SNAP-AI..."

echo "[1/2] Stopping Docker stack..."
docker compose -f docker-compose.prod.yml down 2>/dev/null || true
echo "  ✓ Docker stack stopped"

echo "[2/2] Stopping vLLM..."
VLLM_PID=$(pgrep -f "vllm.entrypoints|vllm serve" 2>/dev/null | head -1 || echo "")
if [ -n "$VLLM_PID" ]; then
    kill "$VLLM_PID" 2>/dev/null || true
    sleep 2
    # Force kill if still running
    kill -9 "$VLLM_PID" 2>/dev/null || true
    echo "  ✓ vLLM stopped (was PID: ${VLLM_PID})"
else
    echo "  vLLM was not running"
fi

echo ""
echo "✓ All services stopped. Data preserved."
echo "  Restart: bash deploy/deploy.sh"
