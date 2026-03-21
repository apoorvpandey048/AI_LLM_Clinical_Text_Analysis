#!/bin/bash
# =============================================================
# SNAP-AI — One-Time DGX Setup
# =============================================================
# Run this ONCE after cloning the repo on the DGX server.
# It creates directories, sets up Docker storage on /raid,
# and installs the vLLM venv.
#
# Usage: sudo bash deploy/setup.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
USER_NAME="${SUDO_USER:-$USER}"

echo "============================================"
echo "  SNAP-AI — DGX Setup"
echo "============================================"

# ---- 1. Move Docker data root to /raid (solves home quota) ----
DOCKER_DATA="/raid/${USER_NAME}/docker-data"
DOCKER_DAEMON="/etc/docker/daemon.json"

if [ -d "/raid" ]; then
    echo "[1/6] Setting Docker data-root → ${DOCKER_DATA}"
    mkdir -p "$DOCKER_DATA"
    chown "$USER_NAME:$USER_NAME" "$DOCKER_DATA"

    if [ -f "$DOCKER_DAEMON" ]; then
        # Check if data-root is already set
        if grep -q "data-root" "$DOCKER_DAEMON" 2>/dev/null; then
            echo "  Docker data-root already configured, skipping."
        else
            # Merge into existing config
            python3 -c "
import json
with open('$DOCKER_DAEMON') as f: cfg = json.load(f)
cfg['data-root'] = '$DOCKER_DATA'
with open('$DOCKER_DAEMON', 'w') as f: json.dump(cfg, f, indent=2)
"
            echo "  Updated existing ${DOCKER_DAEMON}"
        fi
    else
        cat > "$DOCKER_DAEMON" <<EOF
{
  "data-root": "${DOCKER_DATA}",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
EOF
        echo "  Created ${DOCKER_DAEMON}"
    fi
    systemctl restart docker || true
    echo "  ✓ Docker restarted with data on /raid"
else
    echo "[1/6] SKIP — /raid not found, using default Docker storage"
fi

# ---- 2. Create HuggingFace cache on /raid ----
echo "[2/6] Creating HuggingFace cache directory"
HF_DIR="/raid/s3cache/${USER_NAME}/huggingface"
if [ -d "/raid" ]; then
    mkdir -p "$HF_DIR"
    chown -R "$USER_NAME:$USER_NAME" "$HF_DIR"
    echo "  ✓ HF cache → ${HF_DIR}"
else
    echo "  SKIP — /raid not found"
fi

# ---- 3. Create upload + log directories ----
echo "[3/6] Creating data directories"
mkdir -p "${PROJECT_DIR}/data/uploads"
mkdir -p "${PROJECT_DIR}/data/logs"
chown -R "$USER_NAME:$USER_NAME" "${PROJECT_DIR}/data"
echo "  ✓ data/uploads, data/logs"

# ---- 4. Production .env ----
echo "[4/6] Checking production .env"
if [ ! -f "${PROJECT_DIR}/.env" ]; then
    if [ -f "${PROJECT_DIR}/.env.production" ]; then
        cp "${PROJECT_DIR}/.env.production" "${PROJECT_DIR}/.env"
        echo "  ✓ Copied .env.production → .env"
        echo "  ⚠  EDIT .env NOW: set JWT_SECRET, ADMIN_PASSWORD, POSTGRES_PASSWORD"
    else
        echo "  ⚠  No .env found. Copy .env.example → .env and configure."
    fi
else
    echo "  ✓ .env already exists"
fi

# ---- 5. Cloudflare tunnel directory ----
echo "[5/6] Checking Cloudflare tunnel config"
mkdir -p "${PROJECT_DIR}/cloudflared"
if [ ! -f "${PROJECT_DIR}/cloudflared/credentials.json" ]; then
    echo "  ⚠  No credentials.json found."
    echo "     Run: cloudflared tunnel login && cloudflared tunnel create snap-ai"
    echo "     Then: cp ~/.cloudflared/*.json ${PROJECT_DIR}/cloudflared/credentials.json"
else
    echo "  ✓ credentials.json found"
fi

# ---- 6. Install systemd services ----
echo "[6/6] Installing systemd services"
SYSTEMD_DIR="/etc/systemd/system"

# vLLM service
cat > "${SYSTEMD_DIR}/snapai-vllm.service" <<EOF
[Unit]
Description=SNAP-AI vLLM Model Server
After=network.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/start-vllm.sh
Restart=on-failure
RestartSec=30
StandardOutput=append:${PROJECT_DIR}/data/logs/vllm.log
StandardError=append:${PROJECT_DIR}/data/logs/vllm.log
Environment=HOME=/home/${USER_NAME}

# GPU process — give it time to load model
TimeoutStartSec=600
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

# Docker Compose app service
cat > "${SYSTEMD_DIR}/snapai-app.service" <<EOF
[Unit]
Description=SNAP-AI Application Stack (Docker Compose)
Requires=docker.service
After=docker.service snapai-vllm.service
Wants=snapai-vllm.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=${USER_NAME}
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d --build
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down
ExecReload=/usr/bin/docker compose -f docker-compose.prod.yml up -d --build
StandardOutput=append:${PROJECT_DIR}/data/logs/app.log
StandardError=append:${PROJECT_DIR}/data/logs/app.log
Environment=HOME=/home/${USER_NAME}

# Give Docker time to pull/build
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable snapai-vllm.service
systemctl enable snapai-app.service

echo "  ✓ snapai-vllm.service installed + enabled"
echo "  ✓ snapai-app.service installed + enabled"

echo ""
echo "============================================"
echo "  Setup Complete"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env  (JWT_SECRET, ADMIN_PASSWORD, passwords)"
echo "  2. Edit cloudflared/config.yml  (tunnel ID, hostname)"
echo "  3. Run: bash deploy/deploy.sh"
echo ""
