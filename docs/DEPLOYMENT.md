# SNAP-AI Production Deployment Guide

## Prerequisites

- DGX server with NVIDIA GPUs (A100/H100)
- Docker Engine 24+ with Compose v2
- NVIDIA Container Toolkit (for vLLM)
- `cloudflared` CLI installed
- Cloudflare account with a domain

## Step 1: Clone and Configure

```bash
git clone <repository-url> snap-ai
cd snap-ai

# Create production environment
cp .env.example .env.production
```

Edit `.env.production`:
```bash
ENVIRONMENT=production
LLM_BACKEND=vllm
VLLM_HOST=http://host.docker.internal:8000
VLLM_MODEL=openai/gpt-oss-120b

# REQUIRED — generate unique values
JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
ADMIN_PASSWORD=<strong-admin-password>
POSTGRES_PASSWORD=<strong-db-password>
REDIS_PASSWORD=<strong-redis-password>
```

## Step 2: Start vLLM Model Server

vLLM runs on the host (not inside Docker) for direct GPU access:

```bash
# Start vLLM
./start-vllm.sh

# Verify it's running
curl http://localhost:8000/v1/models
```

### GPU Selection

```bash
# Use specific GPUs (e.g., last 4 on an 8-GPU server)
export CUDA_VISIBLE_DEVICES=4,5,6,7
./start-vllm.sh
```

### Stop vLLM

```bash
pkill -f vllm
```

## Step 3: Set Up Cloudflare Tunnel

One-time setup for permanent HTTPS access:

```bash
# Install cloudflared
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb

# Login to Cloudflare
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create snap-ai

# Route DNS (replace with your domain)
cloudflared tunnel route dns snap-ai snap-ai.yourdomain.com

# Copy credentials into project
mkdir -p cloudflared
cp ~/.cloudflared/*.json cloudflared/credentials.json
```

Edit `cloudflared/config.yml`:
```yaml
tunnel: <TUNNEL_ID>  # from 'cloudflared tunnel create' output
credentials-file: /etc/cloudflared/credentials.json

ingress:
  - hostname: snap-ai.yourdomain.com
    service: http://frontend:80
  - service: http_status:404
```

## Step 4: Start Application Stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Verify all containers are running:
```bash
docker compose -f docker-compose.prod.yml ps
```

Expected output:
```
snapai-backend      running (healthy)
snapai-worker       running (healthy)
snapai-postgres     running (healthy)
snapai-redis        running (healthy)
snapai-frontend     running
snapai-cloudflared  running
```

## Step 5: Verify Access

```bash
# Check backend health
curl http://localhost:8000/health

# Check readiness (DB + Redis + LLM)
curl http://localhost:8000/health/ready

# Access via tunnel
curl https://snap-ai.yourdomain.com/health
```

Login at `https://snap-ai.yourdomain.com` with admin credentials.

## Restart Procedures

### Full Restart

```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d --build
```

### Worker Only (after code changes)

```bash
docker compose -f docker-compose.prod.yml restart worker
```

### vLLM Only

```bash
pkill -f vllm
./start-vllm.sh
```

### Database Persistence

PostgreSQL data is stored in a Docker volume (`postgres_data`). It persists across restarts. To reset:

```bash
docker compose -f docker-compose.prod.yml down -v  # WARNING: destroys all data
```

## Resource Usage

| Component | CPU | RAM | GPU |
|-----------|-----|-----|-----|
| Backend | 1 core | 512MB | — |
| Worker | 1 core | 1GB | — |
| PostgreSQL | 1 core | 512MB | — |
| Redis | 0.5 core | 2GB max | — |
| Frontend | 0.5 core | 128MB | — |
| vLLM | 4+ cores | 16GB+ | 1–8 GPUs |

## Security Notes

- No ports are exposed externally — all traffic goes through Cloudflare Tunnel
- Backend is only accessible within the Docker network
- JWT secrets must be unique per deployment
- `cloudflared/credentials.json` must never be committed to git
