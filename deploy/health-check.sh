#!/bin/bash
# =============================================================
# SNAP-AI — Health Check (NO SUDO)
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
VLLM_PID=$(pgrep -f "vllm.entrypoints|vllm serve" 2>/dev/null | head -1 || echo "")
if [ -n "$VLLM_PID" ]; then
    echo "  ✓ Process running (PID: ${VLLM_PID})"
    # Check if model is loaded and responding
    source .env 2>/dev/null || true
    VLLM_PORT=8000
    if curl -sf "http://localhost:${VLLM_PORT}/v1/models" > /dev/null 2>&1; then
        echo "  ✓ API responding on port ${VLLM_PORT}"
    else
        echo "  ⚠  Process running but API not responding (still loading?)"
    fi
else
    echo "  ✗ vLLM process not found"
    echo "    Start: conda activate vllm && nohup bash start-vllm.sh > data/logs/vllm.log 2>&1 &"
    ERRORS=$((ERRORS + 1))
fi

# ---- 4. Disk usage ----
echo ""
echo "[Disk Usage]"
HOME_USED=$(du -sh "$HOME" --max-depth=0 2>/dev/null | awk '{print $1}')
HOME_QUOTA=$(quota -s 2>/dev/null | tail -1 | awk '{print $3}' || echo "unknown")
echo "  Home:   ${HOME_USED:-unknown} / ${HOME_QUOTA}"

if [ -d "/raid" ]; then
    RAID_AVAIL=$(df -h /raid 2>/dev/null | tail -1 | awk '{print $4}')
    echo "  /raid:  ${RAID_AVAIL:-unknown} available"
fi

# Check Docker root location
DOCKER_ROOT=$(docker info 2>/dev/null | grep "Docker Root Dir" | awk -F': ' '{print $2}')
DOCKER_SIZE=$(du -sh "$DOCKER_ROOT" 2>/dev/null | awk '{print $1}' || echo "unknown")
echo "  Docker: ${DOCKER_SIZE} at ${DOCKER_ROOT:-unknown}"
if echo "$DOCKER_ROOT" | grep -q "/home/"; then
    echo "  ⚠  Docker is in /home — wasting quota!"
    echo "     Fix: bash deploy/setup.sh && systemctl --user restart docker"
fi

# Check HF cache
if [ -L "${HOME}/.cache/huggingface" ]; then
    echo "  HF cache: symlinked to $(readlink "${HOME}/.cache/huggingface") ✓"
elif [ -d "${HOME}/.cache/huggingface" ]; then
    HF_SIZE=$(du -sh "${HOME}/.cache/huggingface" 2>/dev/null | awk '{print $1}')
    echo "  HF cache: ${HF_SIZE} IN HOME ⚠ (run setup.sh to fix)"
fi

# ---- 5. GPU ----
echo ""
echo "[GPU Status]"
if command -v nvidia-smi &> /dev/null; then
    nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | \
    while IFS=',' read -r idx name mem_used mem_total gpu_util; do
        marker=""
        if [ "$mem_used" -gt 1000 ] 2>/dev/null; then marker=" ← IN USE"; fi
        echo "  GPU ${idx}: ${name} — ${mem_used}/${mem_total} MiB (${gpu_util}% util)${marker}"
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
