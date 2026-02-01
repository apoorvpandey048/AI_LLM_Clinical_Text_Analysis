# SNAP-AI Setup Guide

## Prerequisites
- Docker & Docker Compose installed
- NVIDIA drivers + nvidia-container-toolkit
- (Optional) HuggingFace account for Llama models

---

## Step 1: Clone Repository
```bash
git clone https://github.com/apoorvpandey048/AI_LLM_Clinical_Text_Analysis.git
cd AI_LLM_Clinical_Text_Analysis
```

## Step 2: Configure Environment
```bash
cp .env.example .env
```

## Step 3: (Optional) Change Model
Edit `docker-compose.prod.yml` line 31:
```yaml
command: >
  --model Qwen/Qwen2.5-72B-Instruct-AWQ  # Change this
```

## Step 4: Start Services
```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```
> ⚠️ First run downloads model (~40GB) - takes 30-60 minutes

## Step 5: Check Status
```bash
docker-compose logs -f vllm  # Watch model download
```

## Step 6: Access Application
| Service | URL |
|---------|-----|
| Web UI | http://localhost:3000 |
| API Docs | http://localhost:8000/docs |

## Step 7: Upload Clinical Cases
1. Open http://localhost:3000
2. Upload `.docx` file or paste text
3. Click "Process Cases"
4. Export results as JSON

---

## Quick Commands
```bash
docker-compose ps          # Check status
docker-compose down        # Stop all
docker-compose logs -f     # View logs
```
