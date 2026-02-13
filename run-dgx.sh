#!/bin/bash
# ===========================================
# SNAP-AI — Local GPU Deployment (One Command)
# ===========================================
# Deploys the full stack on a local GPU machine (DGX / workstation).
#
#   vLLM  ➜  runs on HOST (direct GPU access)
#   App   ➜  runs in Docker (backend, frontend, db, redis, worker)
#
# Usage:
#   chmod +x run-dgx.sh
#   ./run-dgx.sh              # full deploy (build + start vLLM + start app)
#   ./run-dgx.sh --app-only   # skip vLLM, just (re)start Docker services
#   ./run-dgx.sh --stop       # stop everything
# ===========================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---- Handle --stop ----
if [ "${1:-}" = "--stop" ]; then
    echo -e "${YELLOW}Stopping SNAP-AI...${NC}"
    $COMPOSE down
    pkill -f 'vllm serve' 2>/dev/null && echo "  vLLM stopped" || echo "  vLLM was not running"
    echo -e "${GREEN}All services stopped.${NC}"
    exit 0
fi

echo ""
echo "============================================"
echo -e "${CYAN}  SNAP-AI — Local GPU Deployment${NC}"
echo "============================================"
echo ""

# ============================================
# Step 1: Check prerequisites
# ============================================
echo -e "${YELLOW}[1/6] Checking prerequisites...${NC}"

if ! command -v docker &> /dev/null; then
    echo -e "${RED}ERROR: Docker not installed${NC}"
    exit 1
fi
echo "  ✓ Docker"

if ! docker compose version &> /dev/null; then
    echo -e "${RED}ERROR: Docker Compose not installed${NC}"
    exit 1
fi
echo "  ✓ Docker Compose"

if command -v nvidia-smi &> /dev/null; then
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)
    echo "  ✓ ${GPU_COUNT} GPU(s) detected"
else
    echo -e "${YELLOW}  ⚠ nvidia-smi not found (vLLM needs GPUs)${NC}"
fi

# ============================================
# Step 2: Create .env if missing
# ============================================
echo ""
echo -e "${YELLOW}[2/6] Checking environment...${NC}"

if [ ! -f .env ]; then
    if [ -f .env.production ]; then
        cp .env.production .env
        echo "  ✓ Created .env from .env.production"
    else
        echo -e "${YELLOW}  No .env file — creating with defaults...${NC}"
        cat > .env << 'ENVEOF'
ENVIRONMENT=production
POSTGRES_DB=snapai
POSTGRES_USER=snapai
POSTGRES_PASSWORD=snapai_prod_2026
REDIS_PASSWORD=changeme
LLM_BACKEND=vllm
VLLM_HOST=http://host.docker.internal:8000
VLLM_MODEL=openai/gpt-oss-120b
LLM_MAX_TOKENS=8192
LLM_TIMEOUT=900
VLLM_GPU_DEVICES=0
VLLM_MAX_MODEL_LEN=8192
HF_TOKEN=
JWT_SECRET=snapai-local-secret-change-me
ADMIN_PASSWORD=Pancreas123
FRONTEND_PORT=8080
LOG_LEVEL=INFO
ENVEOF
        echo "  ✓ Created .env with defaults"
        echo -e "${YELLOW}  ⚠ Edit .env to set HF_TOKEN if model is gated${NC}"
    fi
else
    echo "  ✓ .env exists"
fi

# Source .env
set -a
source .env 2>/dev/null || true
set +a

# ============================================
# Step 3: Build Docker images
# ============================================
echo ""
echo -e "${YELLOW}[3/6] Building Docker images...${NC}"

$COMPOSE build backend frontend worker
echo "  ✓ Images built"

# ============================================
# Step 4: Start infrastructure
# ============================================
echo ""
echo -e "${YELLOW}[4/6] Starting database & Redis...${NC}"

$COMPOSE up -d postgres redis
echo "  Waiting for DB to be ready..."
sleep 8

# Run alembic migrations (creates tables properly)
echo "  Running database migrations..."
$COMPOSE run --rm backend python -c "
from app.db import Base, engine
Base.metadata.create_all(bind=engine, checkfirst=True)
print('Tables created/verified')
" 2>/dev/null || echo "  (tables will be created on first start)"

echo "  ✓ Infrastructure ready"

# ============================================
# Step 5: Start vLLM on host
# ============================================
if [ "${1:-}" != "--app-only" ]; then
    echo ""
    echo -e "${YELLOW}[5/6] Starting vLLM on host GPU...${NC}"

    if curl -sf http://localhost:8000/v1/models > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓ vLLM is already running${NC}"
        curl -s http://localhost:8000/v1/models | python3 -m json.tool 2>/dev/null | head -5 || true
    else
        if [ -f "$SCRIPT_DIR/start-vllm.sh" ]; then
            chmod +x "$SCRIPT_DIR/start-vllm.sh"
            echo "  Starting vLLM server in background..."
            echo "  Model: ${VLLM_MODEL:-openai/gpt-oss-120b}"
            echo "  GPU: ${VLLM_GPU_DEVICES:-0}"
            echo "  First run downloads the model (~60-70GB). Be patient."
            echo ""
            nohup "$SCRIPT_DIR/start-vllm.sh" > "$SCRIPT_DIR/vllm.log" 2>&1 &
            VLLM_PID=$!
            echo "  vLLM PID: $VLLM_PID"
            echo "  Monitor: tail -f $SCRIPT_DIR/vllm.log"
            echo ""
            echo "  Waiting for vLLM to become ready (this may take minutes on first run)..."
            for i in $(seq 1 60); do
                if curl -sf http://localhost:8000/v1/models > /dev/null 2>&1; then
                    echo -e "  ${GREEN}✓ vLLM is ready!${NC}"
                    break
                fi
                sleep 10
                echo "  ... still waiting ($((i*10))s elapsed)"
            done
            if ! curl -sf http://localhost:8000/v1/models > /dev/null 2>&1; then
                echo -e "  ${YELLOW}⚠ vLLM not ready yet. It may still be downloading the model.${NC}"
                echo "  Monitor progress: tail -f $SCRIPT_DIR/vllm.log"
                echo "  The app will work once vLLM finishes loading."
            fi
        else
            echo -e "  ${YELLOW}⚠ start-vllm.sh not found — start vLLM manually${NC}"
        fi
    fi
else
    echo ""
    echo -e "${YELLOW}[5/6] Skipping vLLM (--app-only mode)${NC}"
fi

# ============================================
# Step 6: Start application services
# ============================================
echo ""
echo -e "${YELLOW}[6/6] Starting application...${NC}"

$COMPOSE up -d backend worker frontend
echo "  Waiting for services..."
sleep 5

echo ""
echo "============================================"
echo -e "${GREEN}  SNAP-AI is running!${NC}"
echo "============================================"
echo ""
echo "  Services:"
$COMPOSE ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || $COMPOSE ps
echo ""
echo -e "  ${CYAN}Open browser:${NC}  http://localhost:${FRONTEND_PORT:-8080}"
echo -e "  ${CYAN}Login:${NC}         admin / ${ADMIN_PASSWORD:-Pancreas123}"
echo ""
echo "  Useful commands:"
echo "    View logs:     $COMPOSE logs -f"
echo "    Backend logs:  $COMPOSE logs -f backend"
echo "    vLLM logs:     tail -f vllm.log"
echo "    Stop all:      ./run-dgx.sh --stop"
echo "    Restart app:   ./run-dgx.sh --app-only"
echo ""
echo "============================================"
