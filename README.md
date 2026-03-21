# SNAP-AI — Clinical NLP Platform

**Secure On-Premise Complication Analysis for German Surgical Discharge Summaries**

SNAP-AI is a production-grade clinical NLP pipeline that extracts postoperative complications from German discharge summaries, assigns Clavien–Dindo grades, and computes the Comprehensive Complication Index (CCI®).

## Architecture

```
┌──────────────────────────────────────────────────┐
│                 Cloudflare Tunnel                 │
│            (HTTPS → snap-ai.domain.com)           │
└──────────────────┬───────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────┐
│              Nginx (Frontend Container)           │
│         Serves SPA + reverse proxies /api         │
│               Port 80 (internal)                  │
├───────────────────────────────────────────────────┤
│                                                   │
│  ┌─────────────┐  ┌────────────┐  ┌────────────┐ │
│  │  FastAPI     │  │  Celery    │  │  PostgreSQL│ │
│  │  Backend     │◄─┤  Worker    │  │  Database  │ │
│  │  :8000       │  │            │  │            │ │
│  └──────┬───────┘  └─────┬──────┘  └────────────┘ │
│         │                │                         │
│  ┌──────▼────────────────▼──────┐  ┌────────────┐ │
│  │            Redis             │  │   Prompts   │ │
│  │       (Message Broker)       │  │ (Mounted)   │ │
│  └──────────────────────────────┘  └────────────┘ │
│                                                   │
│           Docker Compose Network                  │
└───────────────────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────┐
│              vLLM (Host Process)                  │
│         GPU Model Serving — Manual Start          │
│       http://host.docker.internal:8000            │
└───────────────────────────────────────────────────┘
```

## Components

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Backend | FastAPI | REST API, auth, job management |
| Worker | Celery | Async pipeline execution |
| Database | PostgreSQL 15 | Jobs, users, audit logs |
| Broker | Redis 7 | Task queue + pub/sub |
| Frontend | HTML/JS + Nginx | SPA with SSE streaming |
| Model | vLLM | GPU-accelerated inference |
| Tunnel | Cloudflare | Secure HTTPS access |

## Quick Start (Development)

```bash
# Clone and configure
cp .env.example .env
# Edit .env — set ADMIN_PASSWORD and JWT_SECRET

# Start all services
docker compose up -d --build

# Access at http://localhost:8080
# Login: admin / <ADMIN_PASSWORD from .env>
```

## Production Deployment (DGX)

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for step-by-step instructions.

### Start/Stop vLLM Model

```bash
# Start (from project root on DGX host)
./start-vllm.sh

# Stop
pkill -f vllm
```

### Start/Stop Application Stack

```bash
# Start
docker compose -f docker-compose.prod.yml up -d --build

# Stop
docker compose -f docker-compose.prod.yml down

# Restart worker only (after code changes)
docker compose -f docker-compose.prod.yml restart worker
```

### Access the System

- **Production**: `https://snap-ai.yourdomain.com` (via Cloudflare Tunnel)
- **Development**: `http://localhost:8080`
- **API Docs**: `/docs` (Swagger UI)

## Pipeline Layers

1. **Layer 1 — CTP** (Clinical Text Pre-Processor): Extracts and cleans the postoperative course text
2. **Layer 2 — CIE** (Complication Info Extraction): Identifies complications and assigns Clavien–Dindo grades
3. **Layer 3 — CCC** (Clinical Consistency Challenger): Audits and corrects Layer 2 output, recalculates CCI

## Authentication

JWT-based authentication with bcrypt password hashing. See [docs/AUTH.md](docs/AUTH.md).

## Documentation

- [DEPLOYMENT.md](docs/DEPLOYMENT.md) — Production deployment guide
- [AUTH.md](docs/AUTH.md) — Authentication system
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — Common issues and fixes
- [MODELS.md](docs/MODELS.md) — Model selection guide

## Project Structure

```
snap-ai/
├── backend/           # FastAPI + Celery backend
│   ├── app/
│   │   ├── api/routes/ # API endpoints
│   │   ├── db/         # SQLAlchemy models
│   │   ├── llm/        # LLM client abstraction
│   │   ├── pipeline/   # NLP pipeline logic
│   │   └── workers/    # Celery tasks
│   ├── tests/          # pytest test suite
│   └── create_admin.py # Admin user creation script
├── frontend/          # Vite SPA + Nginx
│   ├── src/            # main.js + styles.css
│   ├── index.html      # Application shell
│   └── nginx.conf      # Reverse proxy config
├── prompts/           # Prompt templates (v1.4)
├── cloudflared/       # Tunnel configuration
├── docs/              # Documentation
└── docker-compose.*.yml
```
