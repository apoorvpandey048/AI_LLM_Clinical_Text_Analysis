# SNAP-AI Deployment Guide

Production deployment guide for the SNAP-AI Clinical NLP Pipeline.

## Prerequisites

- Docker and Docker Compose
- NVIDIA GPU with 48GB+ VRAM (for Qwen2.5-72B)
- 100GB+ disk space for model weights

## Quick Start

```bash
# Clone repository
git clone https://github.com/apoorvpandey048/AI_LLM_Clinical_Text_Analysis.git
cd AI_LLM_Clinical_Text_Analysis

# Copy environment file
cp .env.example .env

# Edit .env for production
nano .env
```

## Environment Configuration

### Minimal Production `.env`

```env
ENVIRONMENT=production
LLM_BACKEND=vllm

# vLLM Model (adjust based on GPU)
VLLM_MODEL=Qwen/Qwen2.5-72B-Instruct-AWQ

# Database (use strong password)
POSTGRES_PASSWORD=your_strong_password_here
```

### GPU Memory Requirements

| Model | VRAM Required |
|-------|---------------|
| Qwen2.5-7B-AWQ | 8GB |
| Qwen2.5-32B-AWQ | 24GB |
| Qwen2.5-72B-AWQ | 48GB |

## Deploy with Docker Compose

```bash
# Production deployment
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# View logs
docker compose logs -f

# Check health
curl http://localhost:8000/health/ready
```

## vLLM Server Setup

### Using Docker (Recommended)

The production compose file includes vLLM:

```yaml
vllm:
  image: vllm/vllm-openai:latest
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
  command: >
    --model Qwen/Qwen2.5-72B-Instruct-AWQ
    --quantization awq
    --max-model-len 8192
    --gpu-memory-utilization 0.90
```

### Manual vLLM Setup

```bash
# Install vLLM
pip install vllm

# Start server
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-72B-Instruct-AWQ \
    --quantization awq \
    --max-model-len 8192 \
    --port 8000
```

## Database Migrations

```bash
# Run migrations
docker compose exec backend alembic upgrade head

# Create new migration
docker compose exec backend alembic revision --autogenerate -m "description"
```

## Scaling Workers

Adjust Celery workers in `.env`:

```env
CELERY_WORKERS=4
```

Or scale manually:

```bash
docker compose up -d --scale celery-worker=4
```

## Monitoring

### Health Endpoints

- `GET /health` - Basic health check
- `GET /health/ready` - Full readiness check (DB, Redis, LLM)

### Logs

```bash
# All logs
docker compose logs -f

# Backend only
docker compose logs -f backend

# Worker only  
docker compose logs -f celery-worker
```

## Security Checklist

- [ ] Change default database password
- [ ] Configure firewall (only expose port 80/443)
- [ ] Set up HTTPS with reverse proxy
- [ ] Enable audit logging
- [ ] Configure backup schedule

## Backup

```bash
# Database backup
docker compose exec postgres pg_dump -U snapai snapai > backup.sql

# Restore
docker compose exec -T postgres psql -U snapai snapai < backup.sql
```

## Troubleshooting

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for common issues.
