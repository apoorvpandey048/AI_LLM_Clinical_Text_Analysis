# SNAP-AI Deployment Guide — DGX A100 + GPT-OSS-120B

## Quick Reference

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| Frontend | snapai-frontend | 8080 | Nginx SPA |
| Backend | snapai-backend | 8000 | FastAPI API |
| Worker | snapai-worker | — | Celery tasks |
| vLLM | snapai-vllm | 8000 (internal) | GPT-OSS-120B |
| PostgreSQL | snapai-postgres | 5432 | Database |
| Redis | snapai-redis | 6379 | Queue broker |
| Cloudflared | snapai-tunnel | — | HTTPS tunnel |

---

## Pre-Deployment Checklist

### 1. HuggingFace Token (if model is gated)
```bash
# Go to https://huggingface.co/settings/tokens
# Create a token with "Read" access
# Accept the model license at https://huggingface.co/openai/gpt-oss-120b
```

### 2. Copy Code to DGX
```bash
# From your local machine, SCP or git push, then on DGX:
cd ~/apoo/AI_LLM_Clinical_Text_Analysis
git pull   # or copy files manually
```

### 3. Configure .env
```bash
cd ~/apoo/AI_LLM_Clinical_Text_Analysis
cp .env.production .env

# Edit with your values:
nano .env
```

**Required edits in .env:**
```
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxx    # from step 1
JWT_SECRET=<random-64-char-string>        # run: openssl rand -hex 32
ADMIN_PASSWORD=<strong-password>          # admin login password
POSTGRES_PASSWORD=<strong-password>       # database password
REDIS_PASSWORD=<strong-password>          # redis password
```

**Optional (for Cloudflare tunnel):**
```
CLOUDFLARE_TUNNEL_TOKEN=<tunnel-token>    # see Cloudflare section below
```

---

## Deploy

### Option A: Automated (recommended)
```bash
chmod +x deploy-prod.sh
./deploy-prod.sh
```

### Option B: Manual Step-by-Step
```bash
# 1. Build images
docker compose -f docker-compose.yml -f docker-compose.prod.yml build --no-cache backend frontend worker

# 2. Start infra
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis
sleep 10

# 3. Run migrations
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend alembic upgrade head

# 4. Start app
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d backend worker frontend

# 5. Start vLLM (downloads model, takes 10-30 min first time)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d vllm
```

---

## Monitor

```bash
# All services status
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# vLLM model download progress
docker logs -f snapai-vllm

# Backend logs
docker logs -f snapai-backend

# Worker logs
docker logs -f snapai-worker

# GPU usage
nvidia-smi -l 2

# All logs combined
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f
```

---

## Verify

### 1. Health Check
```bash
curl http://localhost:8080/health
# Expected: {"status":"healthy"}
```

### 2. Readiness (after vLLM loads)
```bash
curl http://localhost:8080/health/ready
# Expected: all services "healthy"
```

### 3. Login
```bash
curl -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-ADMIN_PASSWORD>"}'
# Expected: {"success":true,"data":{"token":"eyJ...",...}}
```

### 4. Test vLLM directly
```bash
docker exec snapai-vllm curl -s http://localhost:8000/v1/models | head
# Expected: JSON with model list
```

---

## GPU Configuration

Default: GPUs 4 and 7 (most free memory on your DGX). To change:
```bash
# In .env:
VLLM_GPU_DEVICES=0,1    # use GPUs 0 and 1 instead
```

To check which GPUs have free memory:
```bash
nvidia-smi --query-gpu=index,name,memory.free --format=csv
```

---

## Cloudflare Tunnel (Permanent URL)

### Setup Steps
```bash
# 1. Install cloudflared on DGX (one-time)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# 2. Login (opens browser)
cloudflared login

# 3. Create tunnel
cloudflared tunnel create snapai
# Note the tunnel ID

# 4. Create a config pointing to frontend
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: snapai.com
    service: http://snapai-frontend:80
  - service: http_status:404
EOF

# 5. Create DNS record
cloudflared tunnel route dns snapai snapai.com

# 6. Get tunnel token for Docker
cloudflared tunnel token snapai
# Copy the token and set CLOUDFLARE_TUNNEL_TOKEN in .env

# 7. Restart to apply
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d cloudflared
```

### Alternative: Quick Tunnel (temporary URL, no domain needed)
```bash
# On DGX directly (not in Docker):
cloudflared tunnel --url http://localhost:8080
# Gives you a temporary *.trycloudflare.com URL
```

---

## Troubleshooting

### vLLM won't start
```bash
# Check GPU memory
nvidia-smi

# Check logs for OOM or download errors
docker logs snapai-vllm 2>&1 | tail -50

# If OOM, try reducing memory utilization in docker-compose.prod.yml:
#   --gpu-memory-utilization 0.85  (down from 0.92)

# If model download fails, check HF_TOKEN
docker exec snapai-vllm env | grep HUG
```

### Backend 500 errors
```bash
docker logs snapai-backend 2>&1 | tail -30
```

### Migration errors
```bash
# Run migrations manually
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend alembic upgrade head

# If schema conflicts, check current revision:
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend alembic current
```

### Reset everything
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v
# WARNING: -v deletes all data (database, uploads, model cache)
```

---

## User Management (Admin)

After logging in as admin:
1. Click **Admin** tab in the UI
2. See all registered users
3. **Approve** pending doctor accounts
4. **Reject** unauthorized accounts

New users flow:
1. Go to the SNAP-AI URL
2. Click "Sign Up" → enter username, name, password
3. Account is "pending" until admin approves
4. Admin approves → user can log in and process cases
