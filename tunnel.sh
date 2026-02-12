#!/usr/bin/env bash
# ==============================================================================
# SNAP-AI Public Tunnel
# Creates a free Cloudflare tunnel to expose the app to the internet.
# Share the generated URL with the doctor — works from any browser, anywhere.
#
# Usage:
#   chmod +x tunnel.sh
#   ./tunnel.sh
#
# The tunnel stays active as long as this script is running.
# Press Ctrl+C to stop.
# ==============================================================================

set -euo pipefail

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[SNAP-AI]${NC} $1"; }
info() { echo -e "${CYAN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

CLOUDFLARED_BIN="${HOME}/.local/bin/cloudflared"
APP_PORT="${FRONTEND_PORT:-8080}"

# Install cloudflared if not present
install_cloudflared() {
    if [[ -x "$CLOUDFLARED_BIN" ]]; then
        log "cloudflared already installed"
        return
    fi

    log "Installing cloudflared..."
    mkdir -p "$(dirname "$CLOUDFLARED_BIN")"
    curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
        -o "$CLOUDFLARED_BIN"
    chmod +x "$CLOUDFLARED_BIN"
    log "cloudflared installed to $CLOUDFLARED_BIN"
}

# Check if the app is running
check_app() {
    if curl -sf "http://localhost:${APP_PORT}" > /dev/null 2>&1; then
        log "App is running on port ${APP_PORT} ✅"
        return 0
    fi

    warn "App not reachable on port ${APP_PORT}"
    warn "Make sure containers are running:"
    warn "  docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo ""
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
}

# Start the tunnel
start_tunnel() {
    echo ""
    log "============================================"
    log "  Starting Cloudflare Tunnel"
    log "============================================"
    echo ""
    info "A public URL will appear below in a few seconds."
    info "Share that URL with the doctor — it works from any browser, anywhere."
    info "Press Ctrl+C to stop the tunnel."
    echo ""

    "$CLOUDFLARED_BIN" tunnel --url "http://localhost:${APP_PORT}" 2>&1 | while IFS= read -r line; do
        # Highlight the public URL when it appears
        if echo "$line" | grep -q "trycloudflare.com"; then
            echo ""
            echo -e "${GREEN}============================================${NC}"
            echo -e "${GREEN}  🌐 Share this URL with the doctor:${NC}"
            echo -e "${GREEN}  $line${NC}"
            echo -e "${GREEN}============================================${NC}"
            echo ""
        else
            echo "$line"
        fi
    done
}

# Main
install_cloudflared
check_app
start_tunnel
