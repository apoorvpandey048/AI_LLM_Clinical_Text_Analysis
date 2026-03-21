# SNAP-AI Deploy Scripts (DGX — No Sudo)

## First-Time Setup (run ONCE)

```bash
# 1. Fix disk: move Docker + HF cache to /raid
bash deploy/setup.sh

# 2. IMPORTANT: restart rootless Docker to use new data-root
systemctl --user restart docker

# 3. Verify Docker moved
docker info | grep "Docker Root Dir"
# Should show: /raid/s3cache/vincent.ochs/docker-data

# 4. Old Docker data can be deleted to free home quota:
# rm -rf ~/.local/share/docker   (only after verifying step 3)

# 5. Edit .env if needed
nano .env
```

## Deploy App Stack

```bash
bash deploy/deploy.sh
```

## Start vLLM (separate terminal)

```bash
conda activate vllm
cd ~/apoo    # or wherever snap-ai is
nohup bash start-vllm.sh > data/logs/vllm.log 2>&1 &
tail -f data/logs/vllm.log   # watch startup
```

## Daily Operations

```bash
bash deploy/health-check.sh          # full status
bash deploy/logs.sh                  # all Docker logs
bash deploy/logs.sh backend          # specific service
bash deploy/logs.sh vllm             # vLLM host logs
bash deploy/stop.sh                  # stop everything
bash deploy/cleanup.sh               # free Docker disk
```

## After Code Changes

```bash
cd ~/apoo
git pull
bash deploy/deploy.sh               # rebuilds + restarts
```

## File Map

| File | Purpose |
|------|---------|
| `deploy/setup.sh` | One-time: Docker→/raid, HF cache symlink |
| `deploy/deploy.sh` | Build + start Docker stack |
| `deploy/stop.sh` | Stop Docker + vLLM |
| `deploy/health-check.sh` | Check all services + disk |
| `deploy/cleanup.sh` | Docker disk cleanup |
| `deploy/logs.sh [service]` | View service logs |
| `start-vllm.sh` | vLLM server (run with conda) |
| `.env` | All config |
