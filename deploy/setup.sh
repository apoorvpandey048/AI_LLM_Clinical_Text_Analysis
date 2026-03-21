#!/bin/bash
# =============================================================
# SNAP-AI — DGX Setup (NO SUDO REQUIRED)
# =============================================================
# Fixes disk quota by moving Docker + HF cache to /raid.
# Works with rootless Docker. Run ONCE after cloning.
#
# Usage: bash deploy/setup.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "============================================"
echo "  SNAP-AI — DGX Setup (no sudo)"
echo "============================================"
echo "  User:    $(whoami)"
echo "  Home:    $HOME"
echo "  Project: ${PROJECT_DIR}"
echo ""

# ---- 1. Move Docker data-root to /raid ----
# Rootless Docker uses ~/.config/docker/daemon.json (no sudo needed)
DOCKER_DATA="/raid/s3cache/${USER}/docker-data"
DOCKER_CONFIG_DIR="${HOME}/.config/docker"
DOCKER_DAEMON="${DOCKER_CONFIG_DIR}/daemon.json"

echo "[1/5] Moving Docker storage → /raid"

if [ -d "/raid" ]; then
    mkdir -p "$DOCKER_DATA"
    mkdir -p "$DOCKER_CONFIG_DIR"

    if [ -f "$DOCKER_DAEMON" ] && grep -q "data-root" "$DOCKER_DAEMON" 2>/dev/null; then
        CURRENT_ROOT=$(python3 -c "import json; print(json.load(open('$DOCKER_DAEMON')).get('data-root',''))" 2>/dev/null || echo "")
        if [ "$CURRENT_ROOT" = "$DOCKER_DATA" ]; then
            echo "  ✓ Already configured: ${DOCKER_DATA}"
        else
            echo "  Updating data-root from: ${CURRENT_ROOT}"
            python3 -c "
import json
with open('$DOCKER_DAEMON') as f: cfg = json.load(f)
cfg['data-root'] = '$DOCKER_DATA'
with open('$DOCKER_DAEMON', 'w') as f: json.dump(cfg, f, indent=2)
"
            echo "  ✓ Updated ${DOCKER_DAEMON}"
            echo "  ⚠  Restart Docker: systemctl --user restart docker"
        fi
    else
        cat > "$DOCKER_DAEMON" <<DEOF
{
  "data-root": "${DOCKER_DATA}",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
DEOF
        echo "  ✓ Created ${DOCKER_DAEMON}"
        echo "  ⚠  Restart Docker: systemctl --user restart docker"
    fi
else
    echo "  SKIP — /raid not found"
fi

# ---- 2. Fix HF cache — symlink ~/.cache/huggingface → /raid ----
echo ""
echo "[2/5] Fixing HuggingFace cache (reclaim ~28GB)"

RAID_HF="/raid/s3cache/${USER}/huggingface"
HOME_HF="${HOME}/.cache/huggingface"

if [ -d "/raid" ]; then
    mkdir -p "$RAID_HF"

    if [ -L "$HOME_HF" ]; then
        echo "  ✓ Already symlinked: ${HOME_HF} → $(readlink "$HOME_HF")"
    elif [ -d "$HOME_HF" ]; then
        # Real directory exists — merge into /raid then symlink
        echo "  Moving ~/.cache/huggingface → /raid (this may take a few minutes)..."
        rsync -a --ignore-existing "$HOME_HF/" "$RAID_HF/" 2>/dev/null || \
            cp -rn "$HOME_HF/"* "$RAID_HF/" 2>/dev/null || true
        rm -rf "$HOME_HF"
        ln -s "$RAID_HF" "$HOME_HF"
        echo "  ✓ Moved + symlinked. Freed ~28GB from home."
    else
        mkdir -p "$(dirname "$HOME_HF")"
        ln -s "$RAID_HF" "$HOME_HF"
        echo "  ✓ Created symlink: ${HOME_HF} → ${RAID_HF}"
    fi
else
    echo "  SKIP — /raid not found"
fi

# ---- 3. Create data directories ----
echo ""
echo "[3/5] Creating data directories"
mkdir -p "${PROJECT_DIR}/data/logs"
mkdir -p "${PROJECT_DIR}/data/uploads"
echo "  ✓ data/logs, data/uploads"

# ---- 4. Production .env ----
echo ""
echo "[4/5] Checking .env"
if [ ! -f "${PROJECT_DIR}/.env" ]; then
    if [ -f "${PROJECT_DIR}/.env.production" ]; then
        cp "${PROJECT_DIR}/.env.production" "${PROJECT_DIR}/.env"
        echo "  ✓ Copied .env.production → .env"
        echo "  ⚠  EDIT .env: verify JWT_SECRET, ADMIN_PASSWORD, VLLM_HOST"
    else
        echo "  ⚠  No .env found. Copy .env.example → .env and configure."
    fi
else
    echo "  ✓ .env already exists"
fi

# ---- 5. Cloudflare tunnel ----
echo ""
echo "[5/5] Checking Cloudflare tunnel"
mkdir -p "${PROJECT_DIR}/cloudflared"
if [ ! -f "${PROJECT_DIR}/cloudflared/credentials.json" ]; then
    echo "  ⚠  No credentials.json found."
    echo "     To set up:"
    echo "       cloudflared tunnel login"
    echo "       cloudflared tunnel create snap-ai"
    echo "       cloudflared tunnel route dns snap-ai snap-ai.yourdomain.com"
    echo "       cp ~/.cloudflared/*.json ${PROJECT_DIR}/cloudflared/credentials.json"
    echo "       nano ${PROJECT_DIR}/cloudflared/config.yml"
else
    echo "  ✓ credentials.json found"
fi

# ---- Summary ----
echo ""
echo "============================================"
echo "  Setup Complete"
echo "============================================"
echo ""

# Check quota after cleanup
HOME_USED=$(du -sh "$HOME" --max-depth=0 2>/dev/null | awk '{print $1}')
echo "  Home usage after setup: ${HOME_USED:-unknown}"
echo ""
echo "  IMPORTANT — Restart Docker to apply new data-root:"
echo ""
echo "    systemctl --user restart docker"
echo ""
echo "  Then deploy:"
echo ""
echo "    bash deploy/deploy.sh"
echo ""
