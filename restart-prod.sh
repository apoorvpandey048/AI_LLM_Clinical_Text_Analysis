#!/bin/bash
# ===========================================
# SNAP-AI: Full Clean Restart (Production)
#
# This script:
#   1. Stops all containers
#   2. Removes stale postgres volume (fresh DB)
#   3. Sets up .env from .env.production
#   4. Rebuilds ALL images
#   5. Starts everything in the correct order
#   6. Runs health checks
#   7. Shows you the login URL
#
# Usage:  chmod +x restart-prod.sh && ./restart-prod.sh
# ===========================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  SNAP-AI Full Clean Restart${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# ============================================
# Step 1: Stop everything
# ============================================
echo -e "${YELLOW}[1/7] Stopping all containers...${NC}"
$COMPOSE down --remove-orphans 2>/dev/null || true
echo -e "  ${GREEN}✓ All containers stopped${NC}"

# ============================================
# Step 2: Remove stale postgres volume
# ============================================
echo ""
echo -e "${YELLOW}[2/7] Removing stale database volume (clean slate)...${NC}"

# Find the actual volume name (prefixed with project name)
PG_VOL=$(docker volume ls --format '{{.Name}}' | grep -i postgres_data | head -1)
if [ -n "$PG_VOL" ]; then
    docker volume rm "$PG_VOL" 2>/dev/null && \
        echo -e "  ${GREEN}✓ Removed volume: $PG_VOL${NC}" || \
        echo -e "  ${YELLOW}⚠ Could not remove $PG_VOL (may not exist)${NC}"
else
    echo -e "  ${GREEN}✓ No stale postgres volume found${NC}"
fi

# Also remove redis volume to clear any stale password issues
REDIS_VOL=$(docker volume ls --format '{{.Name}}' | grep -i redis_data | head -1)
if [ -n "$REDIS_VOL" ]; then
    docker volume rm "$REDIS_VOL" 2>/dev/null && \
        echo -e "  ${GREEN}✓ Removed volume: $REDIS_VOL${NC}" || \
        echo -e "  ${YELLOW}⚠ Could not remove $REDIS_VOL${NC}"
fi

# ============================================
# Step 3: Setup .env
# ============================================
echo ""
echo -e "${YELLOW}[3/7] Setting up environment...${NC}"
if [ -f .env.production ]; then
    cp .env.production .env
    echo -e "  ${GREEN}✓ Copied .env.production → .env${NC}"
else
    echo -e "  ${RED}ERROR: .env.production not found${NC}"
    exit 1
fi

# Verify critical variables
source .env
echo "  DB password: ${POSTGRES_PASSWORD:0:4}****"
echo "  Redis password: ${REDIS_PASSWORD:0:4}****"
echo "  LLM backend: $LLM_BACKEND"
echo "  JWT secret: ${JWT_SECRET:0:8}****"

# ============================================
# Step 4: Rebuild ALL images
# ============================================
echo ""
echo -e "${YELLOW}[4/7] Rebuilding all Docker images...${NC}"
$COMPOSE build --no-cache backend frontend worker
echo -e "  ${GREEN}✓ All images rebuilt${NC}"

# ============================================
# Step 5: Start infrastructure (postgres, redis)
# ============================================
echo ""
echo -e "${YELLOW}[5/7] Starting database & Redis...${NC}"
$COMPOSE up -d postgres redis

echo "  Waiting for postgres to be healthy..."
for i in $(seq 1 30); do
    if docker inspect --format='{{.State.Health.Status}}' snapai-postgres 2>/dev/null | grep -q "healthy"; then
        echo -e "  ${GREEN}✓ PostgreSQL healthy${NC}"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo -e "  ${RED}ERROR: PostgreSQL did not become healthy in 30s${NC}"
        docker logs snapai-postgres --tail 20
        exit 1
    fi
    sleep 1
done

echo "  Waiting for Redis to be healthy..."
for i in $(seq 1 15); do
    if docker inspect --format='{{.State.Health.Status}}' snapai-redis 2>/dev/null | grep -q "healthy"; then
        echo -e "  ${GREEN}✓ Redis healthy${NC}"
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo -e "  ${RED}ERROR: Redis did not become healthy in 15s${NC}"
        docker logs snapai-redis --tail 20
        exit 1
    fi
    sleep 1
done

# ============================================
# Step 6: Start application services
# ============================================
echo ""
echo -e "${YELLOW}[6/7] Starting backend, worker, frontend...${NC}"
$COMPOSE up -d backend
echo "  Waiting for backend to be healthy..."
for i in $(seq 1 60); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' snapai-backend 2>/dev/null || echo "missing")
    if [ "$STATUS" = "healthy" ]; then
        echo -e "  ${GREEN}✓ Backend healthy${NC}"
        break
    fi
    if [ "$STATUS" = "unhealthy" ] || [ "$i" -eq 60 ]; then
        echo -e "  ${RED}Backend not healthy (status: $STATUS). Checking logs:${NC}"
        echo "  --- BACKEND LOGS (last 30 lines) ---"
        docker logs snapai-backend --tail 30
        echo "  --- END LOGS ---"
        echo -e "  ${YELLOW}Continuing anyway — backend may still be starting...${NC}"
        break
    fi
    sleep 1
done

$COMPOSE up -d worker frontend
echo -e "  ${GREEN}✓ Worker and frontend started${NC}"

# ============================================
# Step 7: Final status check
# ============================================
echo ""
echo -e "${YELLOW}[7/7] Final status check...${NC}"
sleep 3
echo ""
$COMPOSE ps
echo ""

# Quick connectivity test
echo -e "${CYAN}--- Quick Health Checks ---${NC}"
if curl -sf http://localhost:8080/ > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ Frontend reachable at http://localhost:8080${NC}"
else
    echo -e "  ${YELLOW}⚠ Frontend not responding yet (may still be starting)${NC}"
fi

if curl -sf http://localhost:8080/health > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ Backend API reachable via nginx proxy${NC}"
else
    echo -e "  ${YELLOW}⚠ Backend API not responding via proxy yet${NC}"
fi

# Direct backend check
BACKEND_IP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' snapai-backend 2>/dev/null)
if [ -n "$BACKEND_IP" ] && curl -sf "http://$BACKEND_IP:8000/health" > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ Backend API responding directly at $BACKEND_IP:8000${NC}"
else
    echo -e "  ${YELLOW}⚠ Backend not responding directly — check logs:${NC}"
    echo -e "     docker logs snapai-backend --tail 20"
fi

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""
echo "Access the app:"
echo "  URL: http://$(hostname -f):8080"
echo ""
echo "Login:"
echo "  Username: admin"
echo "  Password: (value of ADMIN_PASSWORD in .env)"
echo ""
echo "Useful commands:"
echo "  Backend logs:  docker logs -f snapai-backend"
echo "  Worker logs:   docker logs -f snapai-worker"
echo "  Frontend logs: docker logs -f snapai-frontend"
echo "  All status:    $COMPOSE ps"
echo ""
echo "vLLM (run separately on host):"
echo "  ./start-vllm.sh"
echo ""
