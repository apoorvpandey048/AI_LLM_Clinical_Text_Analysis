#!/bin/bash
# =============================================================
# SNAP-AI — Deploy / Redeploy
# =============================================================
# Builds and starts the full stack. Safe to run repeatedly.
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
    echo "✗ .env not found. Run: cp .env.production .env && edit it"
    exit 1
fi

# Check required env vars
source .env 2>/dev/null || true
if [ -z "${JWT_SECRET:-}" ] || echo "$JWT_SECRET" | grep -qi "change-me"; then
    echo "✗ JWT_SECRET not set or still placeholder. Edit .env"
    exit 1
fi
if [ -z "${ADMIN_PASSWORD:-}" ] || echo "$ADMIN_PASSWORD" | grep -qi "change-me"; then
    echo "✗ ADMIN_PASSWORD not set or still placeholder. Edit .env"
    exit 1
fi

echo "[1/5] Preflight checks passed"

# ---- Clean old Docker resources if disk is tight ----
echo "[2/5] Cleaning unused Docker resources"
docker system prune -f --volumes 2>/dev/null || true
echo "  ✓ Pruned"

# ---- Check vLLM ----
echo "[3/5] Checking vLLM status"
VLLM_HOST="${VLLM_HOST:-http://host.docker.internal:8000}"
# Try localhost too (host process)
VLLM_LOCAL="http://localhost:8000"
if curl -sf "${VLLM_LOCAL}/v1/models" > /dev/null 2>&1; then
    MODELS=$(curl -sf "${VLLM_LOCAL}/v1/models" | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['id'] for m in d.get('data',[])))" 2>/dev/null || echo "unknown")
    echo "  ✓ vLLM online — models: ${MODELS}"
else
    echo "  ⚠  vLLM not responding on localhost:8000"
    echo "     Start with: sudo systemctl start snapai-vllm"
    echo "     Or manually: nohup ./start-vllm.sh > data/logs/vllm.log 2>&1 &"
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

echo "[5/5] Waiting for services to be healthy..."
sleep 10

# Health checks
BACKEND_OK=false
for i in $(seq 1 12); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        BACKEND_OK=true
        break
    fi
    echo "  Waiting for backend... (${i}/12)"
    sleep 5
done

echo ""
echo "============================================"
echo "  Deployment Status"
echo "============================================"

# Container status
docker compose -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null || \
    docker compose -f docker-compose.prod.yml ps

echo ""
if $BACKEND_OK; then
    echo "  ✓ Backend: healthy"
    # Full readiness
    READY=$(curl -sf http://localhost:8000/health/ready 2>/dev/null || echo '{}')
    echo "  Readiness: ${READY}" | head -c 200
    echo ""
else
    echo "  ✗ Backend: not responding after 60s"
    echo "    Check: docker logs snapai-backend"
fi

echo ""
echo "  Access: https://snap-ai.yourdomain.com (via Cloudflare)"
echo "  Logs:   docker compose -f docker-compose.prod.yml logs -f"
echo "============================================"
