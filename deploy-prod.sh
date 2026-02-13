#!/bin/bash
# ===========================================
# SNAP-AI Production Deployment Script
# For DGX-02 A100-40GB with GPT-OSS-120B (single GPU)
#
# Model weights are stored on /raid/s3cache/ to bypass
# the 60GB home directory quota.
# ===========================================
set -e

echo "============================================"
echo "  SNAP-AI Production Deployment"
echo "  GPT-OSS-120B on DGX A100"
echo "============================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ============================================
# Step 1: Check Prerequisites
# ============================================
echo -e "${YELLOW}[1/7] Checking prerequisites...${NC}"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}ERROR: Docker not installed${NC}"
    exit 1
fi
echo "  ✓ Docker installed"

# Check Docker Compose
if ! docker compose version &> /dev/null; then
    echo -e "${RED}ERROR: Docker Compose not installed${NC}"
    exit 1
fi
echo "  ✓ Docker Compose installed"

# Check NVIDIA Docker runtime
if ! docker info 2>/dev/null | grep -q "nvidia"; then
    echo -e "${YELLOW}  ⚠ NVIDIA Container Toolkit may not be installed${NC}"
    echo "    Install: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
fi

# Check GPUs
if command -v nvidia-smi &> /dev/null; then
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
    echo "  ✓ ${GPU_COUNT} GPU(s) detected"
    nvidia-smi --query-gpu=index,name,memory.free --format=csv,noheader
else
    echo -e "${RED}ERROR: nvidia-smi not found${NC}"
    exit 1
fi

# ============================================
# Step 2: Setup Environment
# ============================================
echo ""
echo -e "${YELLOW}[2/7] Setting up environment...${NC}"

if [ ! -f .env ]; then
    if [ -f .env.production ]; then
        cp .env.production .env
        echo "  ✓ Created .env from .env.production"
        echo -e "${YELLOW}  ⚠ IMPORTANT: Edit .env to set:${NC}"
        echo "    - JWT_SECRET (random string)"
        echo "    - ADMIN_PASSWORD"
        echo "    - HF_TOKEN (if model is gated)"
        echo "    - CLOUDFLARE_TUNNEL_TOKEN (for permanent URL)"
    else
        echo -e "${RED}ERROR: No .env or .env.production found${NC}"
        exit 1
    fi
else
    echo "  ✓ .env file exists"
fi

# ============================================
# Step 3: Check Disk Space & Setup Model Cache
# ============================================
echo ""
echo -e "${YELLOW}[3/7] Checking disk space & model cache...${NC}"

# Create HuggingFace cache on /raid/s3cache to bypass home quota
HF_CACHE="/raid/s3cache/${USER}/huggingface"
if [ -d "/raid/s3cache" ]; then
    mkdir -p "$HF_CACHE" 2>/dev/null || true
    if [ -w "$HF_CACHE" ]; then
        echo "  ✓ HuggingFace cache: $HF_CACHE (on /raid, bypasses home quota)"
        RAID_AVAIL=$(df -BG /raid | tail -1 | awk '{print $4}' | tr -d 'G')
        echo "  Available on /raid: ${RAID_AVAIL}GB"
    else
        echo -e "${RED}  ERROR: Cannot write to $HF_CACHE${NC}"
        echo "  Run: mkdir -p $HF_CACHE"
        exit 1
    fi
else
    echo -e "${YELLOW}  ⚠ /raid/s3cache not found — model will use home dir (check quota!)${NC}"
fi

AVAIL_GB=$(df -BG "$HOME" | tail -1 | awk '{print $4}' | tr -d 'G')
echo "  Home dir available: ${AVAIL_GB}GB"

# Check user quota
QUOTA_USED=$(quota -s 2>/dev/null | grep '/dev/md0' | awk '{print $2}' || echo "unknown")
QUOTA_LIMIT=$(quota -s 2>/dev/null | grep '/dev/md0' | awk '{print $3}' || echo "unknown")
echo "  Disk quota: ${QUOTA_USED} / ${QUOTA_LIMIT}"

if [ "$AVAIL_GB" -lt 15 ] 2>/dev/null; then
    echo -e "${YELLOW}  ⚠ Less than 15GB free in home. Docker images need ~12-15GB.${NC}"
    echo "  Run: docker system prune -a -f"
fi

# ============================================
# Step 4: Build Docker Images
# ============================================
echo ""
echo -e "${YELLOW}[4/7] Building Docker images...${NC}"

docker compose -f docker-compose.yml -f docker-compose.prod.yml build --no-cache backend frontend worker vllm

echo "  \u2713 Backend, Frontend, Worker, vLLM images built"

# ============================================
# Step 5: Start Infrastructure
# ============================================
echo ""
echo -e "${YELLOW}[5/7] Starting infrastructure (DB, Redis)...${NC}"

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis
echo "  Waiting for services to be healthy..."
sleep 10

# Run database migration
echo "  Running database migrations..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend alembic upgrade head 2>/dev/null || echo "  (migrations may already be applied)"

echo "  ✓ Infrastructure ready"

# ============================================
# Step 6: Start Application
# ============================================
echo ""
echo -e "${YELLOW}[6/7] Starting application services...${NC}"

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d backend worker frontend

echo "  ✓ Backend, Worker, Frontend started"
echo "  waiting 5s for services to initialize..."
sleep 5

# ============================================
# Step 7: Start vLLM (Model Download)
# ============================================
echo ""
echo -e "${YELLOW}[7/7] Starting vLLM with GPT-OSS-120B...${NC}"
echo "  This will download the model (~60-70GB) on first run."
echo "  Model cache: /raid/s3cache/${USER}/huggingface (bypasses quota)"
echo "  Monitor progress: docker logs -f snapai-vllm"
echo ""

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d vllm

echo ""
echo "============================================"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo "============================================"
echo ""
echo "Services running:"
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "Access:"
echo "  Local:  http://localhost:8080"
echo "  Tunnel: https://snapai.com (after Cloudflare setup)"
echo ""
echo "Default admin login:"
echo "  Username: admin"
echo "  Password: (check ADMIN_PASSWORD in .env)"
echo ""
echo "Monitor vLLM model download:"
echo "  docker logs -f snapai-vllm"
echo ""
echo "View all logs:"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f"
echo ""
echo "============================================"
