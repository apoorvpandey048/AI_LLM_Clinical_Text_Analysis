# SNAP-AI Remote Deployment Guide

## Prerequisites

| Requirement | Minimum |
|-------------|---------|
| OS | Ubuntu 22.04 LTS |
| GPUs | 1× NVIDIA GPU (A100/H100 recommended) |
| RAM | 32 GB |
| Disk | 100 GB SSD |
| Network | Public IP or domain name |

## Quick Start (Automated)

```bash
# Clone the repository
git clone https://github.com/your-org/snap-ai.git
cd snap-ai

# Run the deployment script
chmod +x deploy.sh
sudo ./deploy.sh
```

The script will:
1. Install NVIDIA drivers (if not present)
2. Install Docker + Docker Compose
3. Install NVIDIA Container Toolkit
4. Configure firewall (SSH, HTTP, HTTPS)
5. Generate `.env` with secure passwords
6. Build and start all services

## Manual Setup

### Step 1: Install NVIDIA Drivers

```bash
sudo apt update
sudo apt install -y nvidia-driver-535 nvidia-utils-535
sudo reboot
# Verify:
nvidia-smi
```

### Step 2: Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable docker
```

### Step 3: Install NVIDIA Container Toolkit

```bash
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Step 4: Configure Environment

```bash
cp .env.example .env
# Edit .env with your settings:
nano .env
```

Key settings to update:
- `POSTGRES_PASSWORD` — use a strong random password
- `OLLAMA_MODEL` — set to your desired model (see Model Selection below)
- `REDIS_PASSWORD` — use a strong random password

### Step 5: Deploy

```bash
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d
```

### Step 6: Pull a Model

```bash
# Clinical Camel 70B (recommended for clinical research)
docker exec snapai-ollama ollama pull clinical-camel:70b

# Or Me-LLaMA 70B Chat
docker exec snapai-ollama ollama pull me-llama:70b-chat

# Or BioMistral 7B (lighter option)
docker exec snapai-ollama ollama pull biomistral:7b
```

## Model Selection

| Model | Size | VRAM | Best For |
|-------|------|------|----------|
| **Clinical Camel 70B** | ~40 GB | 48 GB+ | Clinical research (surpasses GPT-3.5 on medical benchmarks) |
| **Clinical Camel 13B** | ~8 GB | 10 GB+ | Efficient clinical analysis |
| **Me-LLaMA 70B Chat** | ~40 GB | 48 GB+ | Medical QA, clinical NER, NLI |
| **Me-LLaMA 13B Chat** | ~8 GB | 10 GB+ | Smaller medical LLM |
| **BioMistral 7B** | ~4 GB | 6 GB+ | Biomedical text processing |
| **gpt-oss-120b** | ~70 GB | 80 GB+ | Large-scale general + medical |

> **Tip**: With 8× A100s (640 GB VRAM total), you can run the 70B models comfortably.

## SSL/HTTPS Setup

### Option A: Let's Encrypt (free, automated)

```bash
# Install certbot
sudo apt install -y certbot

# Get certificate (stop frontend first)
docker compose -f docker-compose.yml -f docker-compose.remote.yml stop frontend
sudo certbot certonly --standalone -d your-domain.com

# Copy certs to project
mkdir -p ssl
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ssl/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem ssl/

# Restart with SSL
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d frontend
```

### Option B: Self-signed (for testing)

```bash
mkdir -p ssl
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ssl/privkey.pem -out ssl/fullchain.pem \
  -subj "/CN=snapai.local"
```

Then update `nginx.conf` to add the SSL server block:
```nginx
server {
    listen 443 ssl;
    ssl_certificate /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
    # ... copy the rest of the config from the port 80 block
}
```

## Operations

### Update deployment
```bash
sudo ./deploy.sh --update
```

### View logs
```bash
# All services
docker compose -f docker-compose.yml -f docker-compose.remote.yml logs -f

# Specific service
docker compose -f docker-compose.yml -f docker-compose.remote.yml logs -f worker
```

### Monitor GPU usage
```bash
watch -n 1 nvidia-smi
```

### Scale workers
```bash
# Run 4 worker containers in parallel
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d --scale worker=4
```

### Backup database
```bash
docker exec snapai-postgres pg_dump -U snapai snapai > backup_$(date +%Y%m%d).sql
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Internet                       │
└─────────┬───────────────────────────────────────┘
          │ :80/:443
┌─────────▼───────────────────────────────────────┐
│          Nginx (Frontend + Reverse Proxy)        │
│  ┌──────────┐  ┌────────────────────────────┐   │
│  │ Static   │  │ /api/* → Backend:8000      │   │
│  │ Files    │  │ /api/v1/stream/* → SSE     │   │
│  └──────────┘  └────────────────────────────┘   │
└─────────┬───────────────────────────────────────┘
          │
┌─────────▼──────────┐    ┌────────────────────┐
│  FastAPI Backend   │    │  Celery Workers    │
│  (API + SSE)       │◄──►│  (concurrency=8)   │
└────┬──────┬────────┘    └────┬──────┬────────┘
     │      │                  │      │
┌────▼──┐ ┌─▼────┐       ┌───▼──┐  ┌▼────────┐
│Postgres│ │Redis │       │Redis │  │ Ollama  │
│  DB    │ │Broker│       │PubSub│  │ (GPUs)  │
└────────┘ └──────┘       └──────┘  └─────────┘
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| GPU not detected | Run `nvidia-smi` to verify drivers; restart Docker |
| Model too large | Use a quantized variant (e.g., `q4_K_M`) or smaller model |
| SSE disconnects | Check nginx buffering is off; increase `proxy_read_timeout` |
| Redis connection refused | Verify `REDIS_PASSWORD` matches in `.env` and compose |
| Celery tasks stuck | Restart worker: `docker restart snapai-worker` |
