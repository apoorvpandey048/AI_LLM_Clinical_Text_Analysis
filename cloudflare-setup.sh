#!/bin/bash
# ===========================================
# Cloudflare Tunnel Setup for SNAP-AI
# ===========================================
# This creates a PERMANENT, stable URL for SNAP-AI.
# No more random URLs that change on restart.
#
# Prerequisites:
#   1. Create a FREE Cloudflare account at https://dash.cloudflare.com/sign-up
#   2. You do NOT need a domain — Cloudflare provides a free *.trycloudflare.com URL
#
# For a custom domain (optional):
#   - Add a domain to Cloudflare (free plan works)
#   - This script will configure DNS automatically

set -e

echo "============================================"
echo "  SNAP-AI Cloudflare Tunnel Setup"
echo "============================================"
echo ""

# Step 1: Install cloudflared
if ! command -v cloudflared &> /dev/null; then
    echo "[1/5] Installing cloudflared..."
    curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    sudo dpkg -i cloudflared.deb
    rm cloudflared.deb
    echo "✓ cloudflared installed"
else
    echo "[1/5] cloudflared already installed ✓"
fi

# Step 2: Login to Cloudflare
echo ""
echo "[2/5] Authenticating with Cloudflare..."
echo "A browser window will open. Log in to your Cloudflare account."
echo "If no browser opens, copy the URL shown and open it manually."
echo ""
cloudflared tunnel login

# Step 3: Create named tunnel
TUNNEL_NAME="snapai"
echo ""
echo "[3/5] Creating tunnel '${TUNNEL_NAME}'..."
cloudflared tunnel create ${TUNNEL_NAME}

# Get tunnel ID
TUNNEL_ID=$(cloudflared tunnel list | grep ${TUNNEL_NAME} | awk '{print $1}')
echo "✓ Tunnel created: ${TUNNEL_ID}"

# Step 4: Get tunnel token
echo ""
echo "[4/5] Generating tunnel token..."
TUNNEL_TOKEN=$(cloudflared tunnel token ${TUNNEL_NAME})
echo "✓ Token generated"

# Step 5: Create config
echo ""
echo "[5/5] Writing configuration..."

# Create cloudflared config directory
mkdir -p ~/.cloudflared

cat > ~/.cloudflared/config.yml << EOF
tunnel: ${TUNNEL_ID}
credentials-file: /root/.cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: snapai.com
    service: http://localhost:8080
  - hostname: www.snapai.com
    service: http://localhost:8080
  - service: http_status:404
EOF

# Add DNS records for snapai.com
echo ""
echo "Adding DNS records for snapai.com..."
cloudflared tunnel route dns ${TUNNEL_NAME} snapai.com
cloudflared tunnel route dns ${TUNNEL_NAME} www.snapai.com

echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "Tunnel ID:    ${TUNNEL_ID}"
echo "Tunnel Token: ${TUNNEL_TOKEN}"
echo ""
echo "IMPORTANT: Add this token to your .env file:"
echo ""
echo "  CLOUDFLARE_TUNNEL_TOKEN=${TUNNEL_TOKEN}"
echo ""
echo "Quick-start URL (no domain needed):"
echo "  cloudflared tunnel run ${TUNNEL_NAME}"
echo ""
echo "This gives you a URL like:"
echo "  https://<random>.trycloudflare.com"
echo ""
echo "Or with Docker (production):"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
echo ""
echo "============================================"
echo "  ALTERNATIVE: Quick tunnel (no account needed)"
echo "============================================"
echo ""
echo "If you just want a quick temporary stable URL:"
echo "  cloudflared tunnel --url http://localhost:8080"
echo ""
echo "This gives a random *.trycloudflare.com URL that"
echo "stays stable as long as the command is running."
echo "============================================"
