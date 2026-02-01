# SNAP-AI Troubleshooting Guide

Common issues and solutions for SNAP-AI deployment.

## LLM Connection Issues

### vLLM Not Responding

**Symptoms:**
- `/health/ready` shows LLM as "unhealthy"
- Requests timeout

**Solutions:**

1. Check vLLM container:
```bash
docker compose logs vllm
```

2. Verify GPU availability:
```bash
docker compose exec vllm nvidia-smi
```

3. Check model is loaded:
```bash
curl http://localhost:8000/v1/models
```

### Ollama Connection Refused

**Symptoms:**
- "Connection refused" errors in development

**Solutions:**

1. Ensure Ollama is running:
```bash
ollama serve
```

2. Pull the model:
```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
```

3. Check host configuration:
```env
OLLAMA_HOST=http://host.docker.internal:11434
```

## Database Issues

### Connection Refused

**Symptoms:**
- Backend fails to start
- "Connection refused" in logs

**Solutions:**

1. Wait for PostgreSQL to be ready:
```bash
docker compose logs postgres
```

2. Check database exists:
```bash
docker compose exec postgres psql -U snapai -l
```

3. Run migrations:
```bash
docker compose exec backend alembic upgrade head
```

### Migration Errors

**Solutions:**

1. Check current version:
```bash
docker compose exec backend alembic current
```

2. Reset migrations (development only):
```bash
docker compose exec backend alembic downgrade base
docker compose exec backend alembic upgrade head
```

## Redis/Celery Issues

### Tasks Not Processing

**Symptoms:**
- Jobs stay in "queued" status
- No worker activity

**Solutions:**

1. Check worker logs:
```bash
docker compose logs celery-worker
```

2. Verify Redis connection:
```bash
docker compose exec redis redis-cli ping
```

3. Restart workers:
```bash
docker compose restart celery-worker
```

### Task Timeout

**Symptoms:**
- Tasks fail after 10 minutes

**Solutions:**

Increase timeout in `.env`:
```env
LLM_TIMEOUT=1200  # 20 minutes
```

## Frontend Issues

### API Not Reachable

**Symptoms:**
- Network errors in browser
- CORS errors

**Solutions:**

1. Check backend is running:
```bash
curl http://localhost:8000/health
```

2. Verify CORS settings in `main.py`

3. Check nginx proxy config (production)

## File Upload Issues

### DOCX Not Parsing

**Symptoms:**
- "Failed to extract text" error

**Solutions:**

1. Verify file is valid DOCX:
```bash
unzip -t document.docx
```

2. Check file size limit:
```env
MAX_UPLOAD_SIZE_MB=100
```

## Performance Issues

### Slow Processing

**Causes:**
- GPU memory pressure
- Model too large for GPU

**Solutions:**

1. Use smaller model:
```env
VLLM_MODEL=Qwen/Qwen2.5-32B-Instruct-AWQ
```

2. Reduce max tokens:
```env
LLM_MAX_TOKENS=2048
```

3. Increase GPU memory utilization:
```yaml
command: --gpu-memory-utilization 0.95
```

### Out of Memory

**Solutions:**

1. Reduce batch size
2. Use quantized model (AWQ)
3. Enable tensor parallelism for multi-GPU

## Getting Help

1. Check logs: `docker compose logs -f`
2. Verify config: `docker compose config`
3. Test endpoints: `curl http://localhost:8000/health/ready`

## Contact

For persistent issues, contact the development team.
