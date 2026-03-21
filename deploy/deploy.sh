#!/bin/bash
# =============================================================
# SNAP-AI — Deploy / Redeploy (NO SUDO)
# =============================================================
# Builds and starts the full stack. Safe to run repeatedly.
# Does NOT start vLLM — that's a separate step.
#
# Usage: bash deploy/deploy.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "============================================"
echo "  SNAP-AI — Deploy"
echo "============================================"

# ---- Preflight checks ----
if [ ! -f ".env" ]; then
    echo "✗ .env not found. Run: cp .env.production .env && nano .env"
    exit 1
fi

source .env 2>/dev/null || true
if [ -z "${JWT_SECRET:-}" ] || echo "$JWT_SECRET" | grep -qi "change-me"; then
    echo "✗ JWT_SECRET not set or still placeholder. Edit .env"
    exit 1
fi
if [ -z "${ADMIN_PASSWORD:-}" ] || echo "$ADMIN_PASSWORD" | grep -qi "change-me"; then
    echo "✗ ADMIN_PASSWORD not set or still placeholder. Edit .env"
    exit 1
fi

echo "[1/5] ✓ Preflight checks passed"

# ---- Check Docker is rootless and data-root is on /raid ----
echo "[2/5] Checking Docker"
DOCKER_ROOT=$(docker info 2>/dev/null | grep "Docker Root Dir" | awk -F': ' '{print $2}')
echo "  Docker Root: ${DOCKER_ROOT:-unknown}"
if echo "$DOCKER_ROOT" | grep -q "/home/"; then
    echo "  ⚠  Docker data is still in /home — run: bash deploy/setup.sh"
    echo "     Then: systemctl --user restart docker"
fi

# Prune unused images to save space
echo "  Pruning unused Docker resources..."
docker system prune -f 2>/dev/null || true

# ---- Check vLLM ----
echo "[3/5] Checking vLLM"
VLLM_PID=$(pgrep -f "vllm.entrypoints|vllm serve" 2>/dev/null | head -1 || echo "")
if [ -n "$VLLM_PID" ]; then
    echo "  ✓ vLLM running (PID: ${VLLM_PID})"
    # Verify it's responding
    if curl -sf http://localhost:8000/v1/models > /dev/null 2>&1; then
        MODELS=$(curl -sf http://localhost:8000/v1/models | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['id'] for m in d.get('data',[])))" 2>/dev/null || echo "unknown")
        echo "  ✓ Models: ${MODELS}"
    else
        echo "  ⚠  Process running but not responding yet (model may be loading)"
    fi
else
    echo "  ⚠  vLLM not running"
    echo "     Start in another terminal:"
    echo "       conda activate vllm"
    echo "       cd ${PROJECT_DIR}"
    echo "       nohup bash start-vllm.sh > data/logs/vllm.log 2>&1 &"
    echo ""
    read -p "  Continue deployment without vLLM? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# ---- Build and start ----
echo "[4/5] Building and starting Docker stack"
docker compose -f docker-compose.prod.yml up -d --build

echo "[5/5] Waiting for backend health..."
sleep 8

BACKEND_OK=false
for i in $(seq 1 15); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        BACKEND_OK=true
        break
    fi
    echo "  Waiting... (${i}/15)"
    sleep 4
done

echo ""
echo "============================================"
echo "  Deployment Status"
echo "============================================"
echo ""

docker compose -f docker-compose.prod.yml ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || \
    docker compose -f docker-compose.prod.yml ps

echo ""
if $BACKEND_OK; then
    echo "  ✓ Backend: healthy"
    READY=$(curl -sf http://localhost:8000/health/ready 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    svcs = d.get('services', {})
    for k,v in svcs.items(): print(f'    {k}: {v}')
except: print('    (could not parse)')
" 2>/dev/null)
    echo "$READY"
else
    echo "  ✗ Backend: not responding"
    echo "    Check: docker logs snapai-backend --tail 30"
fi

echo ""
echo "============================================"
