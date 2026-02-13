#!/bin/bash
# ===========================================
# Install vLLM + GPT-OSS-120B on DGX (no sudo required)
# ===========================================
# Uses explicit conda env paths (no 'conda activate' — works in scripts).
#
# Usage:
#   chmod +x setup-vllm.sh
#   ./setup-vllm.sh          # install + start
#   ./setup-vllm.sh --start  # skip install, just start
# ===========================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GPU_ID="${VLLM_GPU_DEVICES:-0}"
MODEL="openai/gpt-oss-120b"
MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
PORT=8000

CONDA_DIR="$HOME/miniconda3"
ENV_DIR="$CONDA_DIR/envs/vllm"
PYTHON="$ENV_DIR/bin/python"
PIP="$ENV_DIR/bin/pip"
VLLM_BIN="$ENV_DIR/bin/vllm"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---- Verify conda exists ----
if [ ! -d "$CONDA_DIR" ]; then
    echo -e "${RED}ERROR: miniconda3 not found at $CONDA_DIR${NC}"
    exit 1
fi
export PATH="$CONDA_DIR/bin:$PATH"
source "$CONDA_DIR/etc/profile.d/conda.sh"

# ---- Shared env vars ----
set_env_vars() {
    export CUDA_VISIBLE_DEVICES="$GPU_ID"
    export HF_HOME="/raid/s3cache/${USER}/huggingface"
    export HF_TOKEN=$(awk -F= '/^HF_TOKEN=/{print $2}' "$SCRIPT_DIR/.env.production" 2>/dev/null || echo "")
    export HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"
    export VLLM_ATTENTION_BACKEND="FLASH_ATTN"
    mkdir -p "$HF_HOME"
}

# ---- Handle --start flag ----
if [ "${1:-}" = "--start" ]; then
    echo -e "${CYAN}Starting vLLM on GPU ${GPU_ID}...${NC}"
    set_env_vars
    exec "$VLLM_BIN" serve "$MODEL" \
        --tensor-parallel-size 1 \
        --max-model-len "$MAX_MODEL_LEN" \
        --gpu-memory-utilization 0.92 \
        --dtype auto \
        --trust-remote-code \
        --enable-prefix-caching \
        --host 0.0.0.0 \
        --port "$PORT"
fi

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  vLLM + GPT-OSS-120B Setup${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# ---- Recreate conda env (clean Python 3.11) ----
# Delete old env to avoid stale packages / wrong Python version
if [ -d "$ENV_DIR" ]; then
    echo -e "${YELLOW}Removing old conda env 'vllm'...${NC}"
    conda env remove -y -n vllm
fi
echo -e "${YELLOW}Creating conda env 'vllm' with Python 3.11...${NC}"
conda create -y -n vllm python=3.11

# Verify correct Python
echo -e "  Python: $($PYTHON --version)"
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [ "$PY_VER" != "3.11" ]; then
    echo -e "${RED}ERROR: Expected Python 3.11 but got $PY_VER${NC}"
    exit 1
fi

set_env_vars

# ---- Step 1: Install PyTorch with CUDA 12.8 ----
echo ""
echo -e "${YELLOW}[1/3] Installing PyTorch with CUDA 12.8...${NC}"
"$PIP" install --upgrade pip -q
"$PIP" install torch --index-url https://download.pytorch.org/whl/cu128 -q
echo -e "  ${GREEN}✓ PyTorch installed: $($PYTHON -c 'import torch; print(torch.__version__)')${NC}"
echo -e "  CUDA available: $($PYTHON -c 'import torch; print(torch.cuda.is_available())')"

# ---- Step 2: Install vLLM (--no-deps to skip stale torch pin) ----
echo ""
echo -e "${YELLOW}[2/3] Installing vLLM 0.10.1+gptoss (no-deps)...${NC}"
"$PIP" install --no-deps --pre "vllm==0.10.1+gptoss" \
    --extra-index-url https://wheels.vllm.ai/gpt-oss/ -q
echo -e "  ${GREEN}✓ vLLM wheel installed${NC}"

# ---- Step 3: Install all other vLLM dependencies ----
echo ""
echo -e "${YELLOW}[3/3] Installing vLLM dependencies...${NC}"
"$PIP" install -q \
    regex cachetools psutil sentencepiece numpy "requests>=2.26.0" tqdm \
    blake3 py-cpuinfo "transformers>=4.53.2" "huggingface-hub[hf_xet]>=0.33.0" \
    "tokenizers>=0.21.1" protobuf "fastapi[standard]>=0.115.0" aiohttp \
    "openai>=1.87.0" "pydantic>=2.10" "prometheus_client>=0.18.0" pillow \
    "prometheus-fastapi-instrumentator>=7.0.0" "tiktoken>=0.6.0" \
    "lm-format-enforcer>=0.10.11,<0.11" "llguidance>=0.7.11,<0.8.0" \
    "outlines_core==0.2.10" "diskcache==5.6.3" "lark==1.2.2" "xgrammar==0.1.21" \
    "typing_extensions>=4.10" "filelock>=3.16.1" partial-json-parser "pyzmq>=25.0.0" \
    msgspec "gguf>=0.13.0" "mistral_common[audio,image]>=1.8.2" \
    "opencv-python-headless>=4.11.0" pyyaml einops "compressed-tensors==0.10.2" \
    "depyf==0.19.0" cloudpickle watchfiles python-json-logger scipy ninja \
    pybase64 cbor2 setproctitle mcp "numba==0.61.2" "ray[cgraph]>=2.48.0" \
    --extra-index-url https://download.pytorch.org/whl/nightly/cu128
echo -e "  ${GREEN}✓ All dependencies installed${NC}"

# ---- Verify ----
echo ""
echo -e "${CYAN}Verifying installation...${NC}"
"$PYTHON" -c "import vllm; print(f'  vLLM version: {vllm.__version__}')"
"$PYTHON" -c "import torch; print(f'  PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}')"

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Installation Complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "To start vLLM now (foreground):"
echo "  ./setup-vllm.sh --start"
echo ""
echo "To start in tmux (recommended):"
echo "  tmux kill-session -t vllm 2>/dev/null; tmux new -s vllm './setup-vllm.sh --start'"
echo ""
echo "GPU: $GPU_ID | Model: $MODEL | Port: $PORT"
echo ""
