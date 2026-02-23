#!/bin/bash
# ===========================================
# DGX Deployment Script for SNAP-AI
# ===========================================
# Pulls latest code, creates .env, and starts Docker stack
# pointing at the host-run vLLM server.
#
# Prerequisites:
#   - vLLM must already be running on host (port 8000)
#   - Docker must be installed and running
#
# Usage:
#   chmod +x deploy-dgx.sh
#   ./deploy-dgx.sh
# ===========================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  SNAP-AI DGX Deployment${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# ---- Step 1: Fix home quota issues ----
echo -e "${YELLOW}[1/7] Fixing home quota (RAID symlinks)...${NC}"
RAID_BASE="/raid/s3cache/${USER}"
mkdir -p "${RAID_BASE}/docker-buildx" "${RAID_BASE}/tmp"

if [ ! -L "$HOME/.docker/buildx" ]; then
    rm -rf "$HOME/.docker/buildx" 2>/dev/null || true
    mkdir -p "$HOME/.docker"
    ln -s "${RAID_BASE}/docker-buildx" "$HOME/.docker/buildx"
    echo -e "  ${GREEN}✓ Docker buildx cache → RAID${NC}"
else
    echo -e "  ${GREEN}✓ Already symlinked${NC}"
fi

export TMPDIR="${RAID_BASE}/tmp"
echo -e "  ${GREEN}✓ TMPDIR=${TMPDIR}${NC}"

# ---- Step 2: Pull latest code ----
echo ""
echo -e "${YELLOW}[2/7] Pulling latest code...${NC}"
# Stash any local changes to avoid merge conflicts
if ! git diff --quiet 2>/dev/null; then
    echo -e "  ${YELLOW}Stashing local changes...${NC}"
    git stash
fi
git pull origin main
echo -e "  ${GREEN}✓ Code updated${NC}"

# ---- Step 3: Create .env if missing ----
echo ""
echo -e "${YELLOW}[3/7] Ensuring .env exists...${NC}"
if [ ! -f .env ]; then
    cat > .env << 'ENVEOF'
LLM_BACKEND=vllm
VLLM_HOST=http://host.docker.internal:8000
VLLM_MODEL=openai/gpt-oss-120b
ENVEOF
    echo -e "  ${GREEN}✓ Created .env${NC}"
else
    echo -e "  ${GREEN}✓ .env already exists${NC}"
fi

# ---- Step 4: Prune Docker to free space ----
echo ""
echo -e "${YELLOW}[4/7] Pruning Docker build cache...${NC}"
docker builder prune -f 2>/dev/null || true
echo -e "  ${GREEN}✓ Pruned${NC}"

# ---- Step 5: Stop old containers ----
echo ""
echo -e "${YELLOW}[5/7] Stopping old containers...${NC}"
docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml down --remove-orphans 2>/dev/null || true
# Kill stale ollama container if lingering
docker rm -f snapai-ollama 2>/dev/null || true
echo -e "  ${GREEN}✓ Old containers removed${NC}"

# ---- Step 6: Build and start ----
echo ""
echo -e "${YELLOW}[6/7] Building and starting Docker stack...${NC}"
docker compose \
    -f docker-compose.yml \
    -f docker-compose.local-gpu.yml \
    up -d --build
echo -e "  ${GREEN}✓ Stack started${NC}"

# ---- Step 7: Verify ----
echo ""
echo -e "${YELLOW}[7/7] Verifying...${NC}"
echo ""
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""

# Test vLLM connectivity from backend container
echo -e "${CYAN}Testing backend → vLLM connection...${NC}"
sleep 3
if docker exec snapai-backend curl -sf http://host.docker.internal:8000/v1/models > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ Backend can reach host vLLM${NC}"
else
    echo -e "  ${RED}✗ Backend cannot reach vLLM — is vLLM running on host?${NC}"
    echo -e "  ${YELLOW}  Start vLLM first: ./vllm_startup_saved.sh${NC}"
fi

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  Frontend:  ${CYAN}http://$(hostname -I | awk '{print $1}'):8080${NC}"
echo -e "  Backend:   ${CYAN}http://localhost:8000${NC} (inside Docker)"
echo -e "  Login:     admin / Pancreas123"
echo ""
echo -e "  Logs:      docker logs snapai-backend --tail 50"
echo -e "  Stop:      docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml down"
echo ""
