# SNAP-AI Deploy Scripts

## First-Time Setup (run once)

```bash
# 1. One-time setup: Docker storage on /raid, systemd services
sudo bash deploy/setup.sh

# 2. Edit configuration
nano .env    # Set JWT_SECRET, ADMIN_PASSWORD, POSTGRES_PASSWORD, HF_TOKEN

# 3. Set up Cloudflare tunnel
cloudflared tunnel login
cloudflared tunnel create snap-ai
cloudflared tunnel route dns snap-ai snap-ai.yourdomain.com
cp ~/.cloudflared/*.json cloudflared/credentials.json
nano cloudflared/config.yml   # Fill in TUNNEL_ID and hostname
```

## Deploy / Redeploy

```bash
bash deploy/deploy.sh
```

## Daily Operations

```bash
# Check health of everything
bash deploy/health-check.sh

# View logs
bash deploy/logs.sh              # all services
bash deploy/logs.sh backend      # specific service
bash deploy/logs.sh vllm         # vLLM host logs

# Stop everything (data preserved)
bash deploy/stop.sh

# Clean Docker disk space
bash deploy/cleanup.sh
```

## Systemd Controls (restart-safe)

```bash
# vLLM (GPU model server)
sudo systemctl start snapai-vllm
sudo systemctl stop snapai-vllm
sudo systemctl status snapai-vllm
journalctl -u snapai-vllm -f

# App stack (Docker Compose)
sudo systemctl start snapai-app
sudo systemctl stop snapai-app
sudo systemctl reload snapai-app    # rebuild + restart
```

Both services auto-start on reboot.

## File Map

| File | Purpose |
|------|---------|
| `deploy/setup.sh` | One-time DGX setup (sudo) |
| `deploy/deploy.sh` | Build + start stack |
| `deploy/stop.sh` | Stop stack + vLLM |
| `deploy/health-check.sh` | Check all services |
| `deploy/cleanup.sh` | Docker disk cleanup |
| `deploy/logs.sh` | View service logs |
| `start-vllm.sh` | vLLM server (used by systemd) |
| `.env` | All config (secrets, GPU, model) |
| `cloudflared/config.yml` | Tunnel routing |
| `cloudflared/credentials.json` | Tunnel credentials (gitignored) |
