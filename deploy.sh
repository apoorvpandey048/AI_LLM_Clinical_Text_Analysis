#!/usr/bin/env bash
# ==============================================================================
# SNAP-AI Remote Deployment Script
# Deploys SNAP-AI on Ubuntu server with NVIDIA GPUs
#
# Usage:
#   chmod +x deploy.sh
#   sudo ./deploy.sh            # Full setup (first time)
#   sudo ./deploy.sh --update   # Update only (pull + restart)
#   ./deploy.sh --setup-only    # Just create .env and start (no sudo needed)
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${GREEN}[SNAP-AI]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }

# ==============================================================================
# Pre-flight checks
# ==============================================================================

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (sudo ./deploy.sh)"
    fi
}

check_gpu() {
    if command -v nvidia-smi &> /dev/null; then
        log "NVIDIA driver detected:"
        nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    else
        warn "nvidia-smi not found. Installing NVIDIA drivers..."
        install_nvidia_drivers
    fi
}

detect_free_gpu() {
    # Find GPU with lowest memory usage (most free VRAM)
    if ! command -v nvidia-smi &> /dev/null; then
        echo ""
        return
    fi

    # Get GPU index with least memory used
    local best_gpu=""
    local best_free=0

    while IFS=, read -r idx total used; do
        idx=$(echo "$idx" | xargs)
        total=$(echo "$total" | xargs | sed 's/ MiB//')
        used=$(echo "$used" | xargs | sed 's/ MiB//')
        local free=$((total - used))

        if [[ $free -gt $best_free ]]; then
            best_free=$free
            best_gpu=$idx
        fi
    done < <(nvidia-smi --query-gpu=index,memory.total,memory.used --format=csv,noheader)

    if [[ -n "$best_gpu" ]]; then
        info "Best available GPU: #${best_gpu} (${best_free} MiB free)"
        echo "$best_gpu"
    else
        echo ""
    fi
}

recommend_model() {
    local vram_mb=$1
    if [[ $vram_mb -ge 35000 ]]; then
        echo "qwen2.5:32b-instruct-q4_K_M"
        info "Recommended model: qwen2.5:32b-instruct-q4_K_M (~20GB, fits ${vram_mb}MB)"
    elif [[ $vram_mb -ge 15000 ]]; then
        echo "qwen2.5:14b-instruct-q4_K_M"
        info "Recommended model: qwen2.5:14b-instruct-q4_K_M (~9GB, fits ${vram_mb}MB)"
    else
        echo "qwen2.5:7b-instruct-q4_K_M"
        info "Recommended model: qwen2.5:7b-instruct-q4_K_M (~4.5GB, fits ${vram_mb}MB)"
    fi
}

# ==============================================================================
# Install dependencies
# ==============================================================================

install_nvidia_drivers() {
    log "Installing NVIDIA drivers..."
    apt-get update
    apt-get install -y linux-headers-$(uname -r)
    apt-get install -y nvidia-driver-535 nvidia-utils-535
    log "NVIDIA drivers installed. Reboot required if this is the first install."
}

install_docker() {
    if command -v docker &> /dev/null; then
        log "Docker already installed: $(docker --version)"
        return
    fi

    log "Installing Docker..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    systemctl enable docker
    systemctl start docker
    log "Docker installed successfully"
}

install_nvidia_container_toolkit() {
    if dpkg -l | grep -q nvidia-container-toolkit; then
        log "NVIDIA Container Toolkit already installed"
        return
    fi

    log "Installing NVIDIA Container Toolkit..."
    distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

    apt-get update
    apt-get install -y nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker

    log "NVIDIA Container Toolkit installed"
}

# ==============================================================================
# Configure firewall
# ==============================================================================

setup_firewall() {
    log "Configuring firewall..."
    ufw allow OpenSSH
    ufw allow 80/tcp    # HTTP
    ufw allow 443/tcp   # HTTPS
    ufw --force enable
    log "Firewall configured: SSH, HTTP, HTTPS allowed"
}

# ==============================================================================
# Setup .env file
# ==============================================================================

setup_env() {
    if [[ -f .env ]]; then
        log ".env file already exists (skipping)"
        return
    fi

    log "Creating .env from template..."

    # Generate secure passwords
    POSTGRES_PW=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)
    REDIS_PW=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)

    # Detect best model for available GPU
    local gpu_id=$(detect_free_gpu)
    local model="qwen2.5:14b-instruct-q4_K_M"

    if [[ -n "$gpu_id" ]]; then
        local vram
        vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "$gpu_id" | xargs)
        model=$(recommend_model "$vram")
    fi

    cat > .env <<EOF
# SNAP-AI Production Configuration
# Generated by deploy.sh on $(date -Iseconds)

ENVIRONMENT=production

# Database
POSTGRES_DB=snapai
POSTGRES_USER=snapai
POSTGRES_PASSWORD=${POSTGRES_PW}

# Redis
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=${REDIS_PW}

# LLM
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=${model}
LLM_BACKEND=ollama
LLM_TEMPERATURE=0.0
LLM_MAX_TOKENS=4096

# GPU (set to specific GPU index if sharing)
NVIDIA_VISIBLE_DEVICES=${gpu_id:-all}

# Logging
LOG_LEVEL=INFO
EOF

    chmod 600 .env
    log ".env created with secure auto-generated passwords"
    log "  Model: ${model}"
    log "  GPU:   ${gpu_id:-all}"
    warn "Review .env if you want to change the model or GPU allocation"
}

# ==============================================================================
# Deploy
# ==============================================================================

deploy() {
    log "Deploying SNAP-AI..."

    docker compose -f docker-compose.yml -f docker-compose.remote.yml build --parallel
    docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d

    log "Waiting for services to start..."
    sleep 15

    # Check service health
    log "Service status:"
    docker compose -f docker-compose.yml -f docker-compose.remote.yml ps

    # Verify GPU access
    if docker exec snapai-ollama nvidia-smi &> /dev/null; then
        log "✅ GPU access confirmed in Ollama container"
    else
        warn "⚠️  GPU access could not be verified in Ollama container"
    fi

    # Auto-pull the configured model
    local model=$(grep OLLAMA_MODEL .env 2>/dev/null | cut -d= -f2 || echo "qwen2.5:14b-instruct-q4_K_M")
    log "Pulling model: ${model} (this may take a few minutes)..."
    docker exec snapai-ollama ollama pull "${model}" || warn "Model pull failed — you can do it later"

    # Health check
    sleep 5
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        log "✅ Backend API is healthy"
    else
        warn "Backend API not responding yet — it may still be starting up"
    fi

    local ip=$(hostname -I | awk '{print $1}')
    log ""
    log "============================================"
    log "✅ SNAP-AI deployed successfully!"
    log "============================================"
    log ""
    log "  🌐 Access the app at:  http://${ip}"
    log ""
    log "  📋 Share this link with the doctor:"
    log "     http://${ip}"
    log ""
    log "  📊 Monitor logs:     docker compose -f docker-compose.yml -f docker-compose.remote.yml logs -f"
    log "  🔧 Monitor GPU:      watch -n 1 nvidia-smi"
    log "  🔄 Update:           git pull && sudo ./deploy.sh --update"
    log ""
}

update() {
    log "Updating SNAP-AI..."
    git pull origin main
    docker compose -f docker-compose.yml -f docker-compose.remote.yml build --parallel
    docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d
    log "✅ Update complete!"
}

# ==============================================================================
# Quick setup (no sudo required — for shared DGX servers)
# ==============================================================================

setup_only() {
    log "======================================"
    log "SNAP-AI Quick Setup (no sudo)"
    log "======================================"

    # Check Docker is available
    if ! command -v docker &> /dev/null; then
        error "Docker not found. Ask your admin to install Docker and add you to the docker group."
    fi

    # Check nvidia GPU access
    if ! command -v nvidia-smi &> /dev/null; then
        warn "nvidia-smi not found — GPU access may not work"
    fi

    setup_env
    deploy
}

# ==============================================================================
# Main
# ==============================================================================

main() {
    case "${1:-}" in
        --update)
            update
            exit 0
            ;;
        --setup-only)
            setup_only
            exit 0
            ;;
        *)
            check_root
            log "====================================="
            log "SNAP-AI Remote Deployment"
            log "====================================="
            check_gpu
            install_docker
            install_nvidia_container_toolkit
            setup_firewall
            setup_env
            deploy
            ;;
    esac
}

main "$@"
