#!/bin/bash
# =============================================================
# SNAP-AI — Stop Everything
# =============================================================
# Stops the Docker stack and vLLM. Data is preserved.
#
# Usage: bash deploy/stop.sh
# =============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Stopping SNAP-AI..."

echo "[1/2] Stopping Docker stack..."
docker compose -f docker-compose.prod.yml down 2>/dev/null || true
echo "  ✓ Docker stack stopped"

echo "[2/2] Stopping vLLM..."
if systemctl is-active --quiet snapai-vllm 2>/dev/null; then
    sudo systemctl stop snapai-vllm
    echo "  ✓ vLLM stopped (systemd)"
else
    pkill -f "vllm serve" 2>/dev/null && echo "  ✓ vLLM process killed" || echo "  vLLM not running"
fi

echo ""
echo "✓ All services stopped. Data preserved."
echo "  Restart: bash deploy/deploy.sh"
