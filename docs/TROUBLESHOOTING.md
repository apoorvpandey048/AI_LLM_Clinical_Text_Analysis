# SNAP-AI Troubleshooting Guide

## vLLM Issues

### Model Not Starting

**Symptom**: `./start-vllm.sh` fails or model doesn't load.

**Fixes**:
1. Check GPU availability:
   ```bash
   nvidia-smi
   ```
2. Check if another vLLM process is running:
   ```bash
   ps aux | grep vllm
   pkill -f vllm  # kill existing process
   ```
3. Check CUDA_VISIBLE_DEVICES:
   ```bash
   echo $CUDA_VISIBLE_DEVICES
   # Set explicitly if needed:
   export CUDA_VISIBLE_DEVICES=0,1,2,3
   ```
4. Check disk space for model cache:
   ```bash
   df -h /raid/s3cache/$USER/huggingface
   ```

### "Model Not Found" Error

**Symptom**: Backend returns "Model Not Found" when processing cases.

**Fix**: Ensure `VLLM_MODEL` in `.env` matches the model being served:
```bash
# Check what vLLM is serving
curl http://localhost:8000/v1/models

# Update .env to match
VLLM_MODEL=<exact-model-name-from-above>
```

### vLLM Out of Memory

**Symptom**: CUDA OOM errors during inference.

**Fixes**:
- Reduce `--max-model-len` in `start-vllm.sh`
- Use a quantized model (AWQ)
- Allocate more GPUs: `--tensor-parallel-size 4`

## Docker Issues

### Container Won't Start

```bash
# Check logs for specific container
docker logs snapai-backend
docker logs snapai-worker
docker logs snapai-postgres

# Full stack logs
docker compose -f docker-compose.prod.yml logs --tail=50
```

### Disk Space / Quota Issues

**Symptom**: Docker build fails with "no space left on device".

**Fixes**:
```bash
# Clean unused Docker resources
docker system prune -a --volumes

# Check disk usage
docker system df

# Remove old images
docker image prune -a
```

### Database Connection Errors

**Symptom**: Backend logs show "database_not_ready" or connection refused.

**Fixes**:
1. Check if PostgreSQL is running:
   ```bash
   docker compose -f docker-compose.prod.yml ps postgres
   ```
2. Check PostgreSQL logs:
   ```bash
   docker logs snapai-postgres
   ```
3. Verify credentials match between backend and postgres in docker-compose

### Redis Connection Errors

**Symptom**: Celery worker can't connect to Redis.

**Fix**: Ensure `REDIS_PASSWORD` is the same in backend, worker, and redis services:
```bash
docker exec snapai-redis redis-cli -a <password> ping
# Should return: PONG
```

## GPU Issues

### nvidia-smi Not Found

**Fix**: Install NVIDIA drivers:
```bash
# Check driver version
cat /proc/driver/nvidia/version

# Install if missing (Ubuntu)
sudo apt install nvidia-driver-525
sudo reboot
```

### GPU Not Available in Docker

**Symptom**: Container can't see GPUs.

**Fix**: Install NVIDIA Container Toolkit:
```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit.gpg
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### GPU Memory Not Releasing

**Symptom**: `nvidia-smi` shows memory in use but no processes.

**Fix**:
```bash
# Kill zombie processes
sudo fuser -v /dev/nvidia*
sudo kill -9 <PID>
```

## Cloudflare Tunnel Issues

### Tunnel Not Connecting

**Symptom**: `snapai-cloudflared` container restarting.

**Fixes**:
1. Check tunnel logs:
   ```bash
   docker logs snapai-cloudflared
   ```
2. Verify `cloudflared/config.yml` has correct tunnel ID
3. Verify `cloudflared/credentials.json` exists and was generated for this tunnel
4. Test tunnel manually:
   ```bash
   cloudflared tunnel --config ./cloudflared/config.yml run
   ```

### Site Not Loading via Domain

**Fixes**:
1. Check DNS record exists:
   ```bash
   dig snap-ai.yourdomain.com
   ```
2. Verify frontend container is running:
   ```bash
   docker compose -f docker-compose.prod.yml ps frontend
   ```
3. Check if the hostname in `config.yml` matches the DNS record

## Application Issues

### Login Fails

**Symptom**: "Invalid username or password" even with correct credentials.

**Fixes**:
1. Verify admin user exists — check backend startup logs:
   ```bash
   docker logs snapai-backend | grep admin
   ```
2. Reset admin password:
   ```bash
   docker exec snapai-backend python create_admin.py \
     --username admin --password <new-password> --name "Admin"
   ```

### Jobs Stuck in "Processing"

**Symptom**: Job stays in processing state indefinitely.

**Fixes**:
1. Check worker logs:
   ```bash
   docker logs snapai-worker --tail=100
   ```
2. Check if vLLM is responding:
   ```bash
   curl http://localhost:8000/v1/models
   ```
3. Restart worker:
   ```bash
   docker compose -f docker-compose.prod.yml restart worker
   ```

### SSE Stream Not Connecting

**Symptom**: Frontend shows "Disconnected" during processing.

**Fix**: This is usually a nginx timeout. The default config handles this, but verify:
```bash
docker exec snapai-frontend cat /etc/nginx/conf.d/default.conf | grep proxy_read_timeout
# Should show: proxy_read_timeout 3600s
```
