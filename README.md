# SNAP-AI: Clinical NLP Pipeline

A secure, on-premise LLM pipeline for clinical text analysis of German discharge summaries. Extracts postoperative complications, assigns Clavien-Dindo grades, and computes the Comprehensive Complication Index (CCI®).

## 🏥 Overview

SNAP-AI processes anonymized clinical reports through three distinct layers:

1. **Layer 1 (CTP)**: Clinical Text Pre-Processor - de-identification, drug normalization
2. **Layer 2 (CIE)**: Complication Inference Engine - extract complications, assign CD grades, compute CCI
3. **Layer 3 (CCC)**: Clinical Consistency Challenger - audit and verify Layer 2 outputs

## 🚀 Quick Start

### Development (6GB GPU)

```bash
# Clone the repository
git clone https://github.com/apoorvpandey048/AI_LLM_Clinical_Text_Analysis.git
cd AI_LLM_Clinical_Text_Analysis

# Copy environment file
cp .env.example .env

# Start all services
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Pull the development model (first time only)
docker exec snapai-ollama ollama pull qwen2.5:7b-instruct-q4_K_M

# Access the web interface
open http://localhost:3000

# View API docs
open http://localhost:8000/docs
```

### Production (48GB GPU)

```bash
# Clone the repository
git clone https://github.com/apoorvpandey048/AI_LLM_Clinical_Text_Analysis.git
cd AI_LLM_Clinical_Text_Analysis

# Copy and configure environment file
cp .env.example .env
# Edit .env with production settings

# Start all services (vLLM will auto-download the model)
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Access the web interface
open http://localhost:3000
```

## 🏗️ Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Web Frontend  │────▶│   FastAPI API   │────▶│  Celery Worker  │
│   (Port 3000)   │     │   (Port 8000)   │     │                 │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                        ┌────────────────────────────────┼────────────────────────────────┐
                        │                                ▼                                │
                        │  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────────┐  │
                        │  │ Layer 1 │───▶│ Layer 2 │───▶│ Layer 3 │───▶│   Results   │  │
                        │  │   CTP   │    │   CIE   │    │   CCC   │    │   (JSON)    │  │
                        │  └────┬────┘    └────┬────┘    └────┬────┘    └─────────────┘  │
                        │       │              │              │                          │
                        │       └──────────────┴──────────────┘                          │
                        │                      │                                         │
                        │              ┌───────▼───────┐                                 │
                        │              │ Ollama/vLLM   │                                 │
                        │              │ (Qwen2.5)     │                                 │
                        │              └───────────────┘                                 │
                        │                    SNAP-AI Pipeline                            │
                        └────────────────────────────────────────────────────────────────┘
```

## 📁 Project Structure

```
snap-ai/
├── docker-compose.yml           # Base configuration
├── docker-compose.dev.yml       # Development (6GB GPU, Ollama)
├── docker-compose.prod.yml      # Production (48GB GPU, vLLM)
├── backend/
│   ├── app/
│   │   ├── api/                 # FastAPI routes
│   │   ├── pipeline/            # SNAP-AI layers
│   │   ├── llm/                 # LLM clients
│   │   ├── workers/             # Celery tasks
│   │   └── db/                  # Database models
├── frontend/                    # Web interface
├── prompts/                     # Versioned prompt templates
│   └── snapai/
│       ├── layer1_ctp_v1.3.md
│       ├── layer2_cie_v1.3.md
│       └── layer3_ccc_v1.3.md
└── data/
    └── sample_cases/            # Test cases
```

## 🔧 Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `ENVIRONMENT` | `development` or `production` | `development` |
| `LLM_BACKEND` | `ollama` or `vllm` | `ollama` |
| `OLLAMA_MODEL` | Model for development | `qwen2.5:7b-instruct-q4_K_M` |
| `VLLM_MODEL` | Model for production | `Qwen/Qwen2.5-72B-Instruct-AWQ` |
| `LLM_TEMPERATURE` | Inference temperature | `0.0` |

## 📊 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/upload` | Upload clinical report(s) |
| `GET` | `/api/jobs` | List all jobs |
| `GET` | `/api/jobs/{id}` | Get job status |
| `GET` | `/api/jobs/{id}/results` | Get job results |

## 🧪 Testing

```bash
# Run unit tests
docker exec snapai-backend pytest tests/ -v

# Run with sample cases
docker exec snapai-backend python -m scripts.validate_cases
```

## 📝 Model Information

### Development: Qwen2.5-7B-Instruct (Q4_K_M)
- **VRAM**: ~4.5GB
- **Speed**: ~30 tokens/sec
- **Use case**: Development, testing, iteration

### Production: Qwen2.5-72B-Instruct (AWQ)
- **VRAM**: ~42GB
- **Speed**: ~15 tokens/sec
- **Use case**: Production inference, clinical accuracy

## 🔒 Security

- **On-premise only**: No cloud dependencies
- **De-identification**: PHI removed in Layer 1
- **Audit trail**: All operations logged
- **Deterministic**: Temperature=0 for reproducibility

## 📄 License

[Specify your license]

## 🤝 Support

For questions or issues, please contact [your contact info].
