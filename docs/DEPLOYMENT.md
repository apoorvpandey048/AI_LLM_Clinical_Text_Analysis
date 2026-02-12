# SNAP-AI Deployment Guide

## Quick Start (DGX / Shared Server)

> **For Vincent's DGX server or any shared Linux server with NVIDIA GPUs**

```bash
# 1. SSH into the server
ssh vincent.ochs@dbe-dgx-a100-01.dbe.unibas.ch

# 2. Clone the repo (into your home directory)
cd ~
git clone https://github.com/apoorvpandey048/AI_LLM_Clinical_Text_Analysis.git snap-ai
cd snap-ai

# 3. Run the setup (no sudo needed)
chmod +x deploy.sh
./deploy.sh --setup-only
```

That's it! The script will:
1. Auto-detect the best available GPU
2. Generate `.env` with secure passwords and recommended model
3. Build and start all services (backend, frontend, Ollama, Redis, PostgreSQL)
4. Pull the LLM model automatically

**After deployment, share this link with the doctor:**
```
http://<server-ip>
```
The script prints the exact URL at the end.

---

## Prerequisites

| Requirement | Minimum |
|-------------|---------|
| OS | Ubuntu 22.04+ |
| GPU | 1× NVIDIA GPU with ≥10GB VRAM |
| RAM | 16 GB |
| Disk | 50 GB free |
| Docker | Docker + Docker Compose (already on DGX) |
| NVIDIA | Driver 525+ and Container Toolkit |

---

## Model Selection (A100-40GB)

| Model | VRAM Needed | Quality | Speed |
|-------|-------------|---------|-------|
| `qwen2.5:32b-instruct-q4_K_M` | ~20 GB | ★★★★★ | Medium |
| `qwen2.5:14b-instruct-q4_K_M` | ~9 GB | ★★★★ | Fast |
| `qwen2.5:7b-instruct-q4_K_M` | ~4.5 GB | ★★★ | Very Fast |
| `llama3.1:8b-instruct-q4_K_M` | ~5 GB | ★★★ | Very Fast |
| `mistral:7b-instruct-q4_K_M` | ~4.5 GB | ★★★ | Very Fast |

> **Tip:** The deploy script auto-selects based on available VRAM. You can change the model later:
> ```bash
> # Edit .env and change OLLAMA_MODEL=...
> nano .env
> # Then restart
> docker compose -f docker-compose.yml -f docker-compose.remote.yml restart
> ```

---

## Sharing with the Doctor

Once deployed, the doctor can access the application from **any browser** on any device connected to the same network (or internet if the server has a public IP).

### Option 1: Direct IP (easiest)
```
http://131.152.136.65
```
Share this link. The doctor opens it in Chrome/Firefox/Safari.

### Option 2: SSH Tunnel (if behind firewall)
If the server is not directly accessible from the internet, the doctor can use an SSH tunnel:
```bash
# On the doctor's machine (Mac/Linux)
ssh -L 8080:localhost:80 vincent.ochs@dbe-dgx-a100-01.dbe.unibas.ch
# Then open http://localhost:8080 in the browser
```

### Option 3: Domain Name
Ask your IT admin to point a domain (e.g., `snapai.your-university.ch`) to the server's IP.

---

## Manual Setup (if deploy.sh doesn't work)

### Step 1: Create `.env` file
```bash
cp .env.example .env
nano .env
# Set POSTGRES_PASSWORD to a strong random password
# Set REDIS_PASSWORD to a strong random password
# Set OLLAMA_MODEL to your preferred model (see table above)
# Set NVIDIA_VISIBLE_DEVICES to available GPU index (e.g. 7)
```

### Step 2: Deploy
```bash
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d --build
```

### Step 3: Pull a Model
```bash
docker exec snapai-ollama ollama pull qwen2.5:14b-instruct-q4_K_M
```

### Step 4: Verify
```bash
# Check all services are running
docker compose -f docker-compose.yml -f docker-compose.remote.yml ps

# Check backend health
curl http://localhost:8000/health

# Check GPU access in Ollama
docker exec snapai-ollama nvidia-smi
```

---

## GPU Selection on Shared Servers

On a shared DGX with multiple users, check which GPUs are free:
```bash
nvidia-smi
```

Look for a GPU with 0% utilization and low memory usage. Set it in `.env`:
```bash
# Use only GPU 7
NVIDIA_VISIBLE_DEVICES=7
```

Or set via environment variable before deploying:
```bash
NVIDIA_VISIBLE_DEVICES=7 docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d
```

---

## Operations

### View Logs
```bash
# All services
docker compose -f docker-compose.yml -f docker-compose.remote.yml logs -f

# Just the backend
docker compose -f docker-compose.yml -f docker-compose.remote.yml logs -f backend

# Just Ollama
docker compose -f docker-compose.yml -f docker-compose.remote.yml logs -f ollama
```

### Stop Everything
```bash
docker compose -f docker-compose.yml -f docker-compose.remote.yml down
```

### Update to Latest Code
```bash
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d --build
```

### Monitor GPU
```bash
watch -n 1 nvidia-smi
```

### Reset Everything
```bash
docker compose -f docker-compose.yml -f docker-compose.remote.yml down -v
rm .env
./deploy.sh --setup-only
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `POSTGRES_PASSWORD is missing` | Create `.env` file: `cp .env.example .env` and set passwords |
| GPU not detected in container | Set `NVIDIA_VISIBLE_DEVICES` in `.env` to the correct GPU index |
| Model too large for GPU | Use a smaller quantized model (see table above) |
| Redis connection refused | Make sure `REDIS_PASSWORD` in `.env` matches across services |
| Port 80 already in use | Change frontend port in `docker-compose.remote.yml` (e.g., `8080:80`) |
| Permission denied (Docker) | Ask admin to add you to `docker` group: `sudo usermod -aG docker $USER` |
| `git clone` fails via SFTP | Clone directly on the server via SSH, not through file manager |

---

## Architecture

```
┌────────────────────────────────────────────────────┐
│                  Doctor's Browser                   │
└──────────┬─────────────────────────────────────────┘
           │ HTTP :80
┌──────────▼─────────────────────────────────────────┐
│          Nginx (Frontend + Reverse Proxy)           │
│  ┌──────────┐  ┌─────────────────────────────┐     │
│  │ Static   │  │ /api/* → Backend:8000       │     │
│  │ Files    │  │ /api/v1/stream/* → SSE      │     │
│  └──────────┘  └─────────────────────────────┘     │
└──────────┬─────────────────────────────────────────┘
           │
┌──────────▼──────────┐    ┌─────────────────────┐
│  FastAPI Backend    │    │  Celery Workers     │
│  (API + SSE)        │◄──►│  (concurrency=2)    │
└────┬──────┬─────────┘    └────┬──────┬─────────┘
     │      │                   │      │
┌────▼──┐ ┌─▼────┐        ┌───▼──┐  ┌▼─────────┐
│Postgres│ │Redis │        │Redis │  │ Ollama   │
│  DB    │ │Broker│        │PubSub│  │ (1×GPU)  │
└────────┘ └──────┘        └──────┘  └──────────┘
```
