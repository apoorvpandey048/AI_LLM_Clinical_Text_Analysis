#!/bin/bash
# =============================================================
# SNAP-AI — Health Check
# =============================================================
# Quick check of all services. Use in cron or manual checks.
#
# Usage: bash deploy/health-check.sh
# Exit code: 0 = all healthy, 1 = issues found
# =============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

ERRORS=0

echo "SNAP-AI Health Check — $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================"

# ---- 1. Docker containers ----
echo ""
echo "[Docker Containers]"
for svc in snapai-backend snapai-worker snapai-postgres snapai-redis snapai-frontend snapai-cloudflared; do
    STATUS=$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "missing")
    HEALTH=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$svc" 2>/dev/null || echo "n/a")
    if [ "$STATUS" = "running" ]; then
        echo "  ✓ ${svc}: ${STATUS} (health: ${HEALTH})"
    else
        echo "  ✗ ${svc}: ${STATUS}"
        ERRORS=$((ERRORS + 1))
    fi
done

# ---- 2. Backend API ----
echo ""
echo "[Backend API]"
if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "  ✓ /health: OK"
else
    echo "  ✗ /health: FAIL"
    ERRORS=$((ERRORS + 1))
fi

# ---- 3. vLLM ----
echo ""
echo "[vLLM Model Server]"
if curl -sf http://localhost:8000/v1/models > /dev/null 2>&1; then
    # vLLM on same port as backend — check via docker network won't work.
    # vLLM runs on host, backend on Docker. Check the host-side port.
    echo "  (Port 8000 is in use — check manually if this is backend or vLLM)"
fi

# Check the typical vLLM port
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_PID=$(pgrep -f "vllm serve" 2>/dev/null || echo "")
if [ -n "$VLLM_PID" ]; then
    echo "  ✓ vLLM process running (PID: ${VLLM_PID})"
else
    echo "  ✗ vLLM process not found"
    echo "    Start: sudo systemctl start snapai-vllm"
    ERRORS=$((ERRORS + 1))
fi

# ---- 4. Disk usage ----
echo ""
echo "[Disk Usage]"
HOME_USAGE=$(df -h "$HOME" 2>/dev/null | tail -1 | awk '{print $5}')
echo "  Home:   ${HOME_USAGE:-unknown}"
if [ -d "/raid" ]; then
    RAID_USAGE=$(df -h /raid 2>/dev/null | tail -1 | awk '{print $5}')
    echo "  /raid:  ${RAID_USAGE:-unknown}"
fi
DOCKER_USAGE=$(docker system df 2>/dev/null | head -5 || echo "  unavailable")
echo "  Docker:"
echo "$DOCKER_USAGE" | sed 's/^/    /'

# ---- 5. GPU ----
echo ""
echo "[GPU Status]"
if command -v nvidia-smi &> /dev/null; then
    nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | \
    while IFS=',' read -r idx name mem_used mem_total gpu_util; do
        echo "  GPU ${idx}: ${name} — ${mem_used}/${mem_total} MiB (${gpu_util}% util)"
    done
else
    echo "  nvidia-smi not found"
fi

# ---- Summary ----
echo ""
echo "================================================"
if [ $ERRORS -eq 0 ]; then
    echo "Status: ALL HEALTHY ✓"
    exit 0
else
    echo "Status: ${ERRORS} ISSUE(S) FOUND ✗"
    exit 1
fi
