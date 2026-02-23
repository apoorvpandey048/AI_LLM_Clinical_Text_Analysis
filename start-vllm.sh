#!/bin/bash
# ===========================================
# Start vLLM on host (outside Docker)
# ===========================================
# Runs vLLM directly on the host for native GPU access.
# The Docker containers (backend, worker) connect to this
# server via host.docker.internal:8000.
#
# Usage:
#   ./start-vllm.sh                   # foreground (see logs)
#   ./start-vllm.sh &                 # background
#   nohup ./start-vllm.sh > vllm.log 2>&1 &   # persistent
#
# Environment (set in .env or export before running):
#   VLLM_GPU_DEVICES   — comma-separated GPU IDs  (default: 2)
#   VLLM_TP_SIZE       — tensor-parallel size      (default: 1)
#   VLLM_QUANTIZATION  — quantization method       (default: mxfp4)
#   VLLM_MAX_MODEL_LEN — max context length        (default: 8192)
#   HF_TOKEN           — HuggingFace auth token
# ===========================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Configuration (override via env / .env) ----
# Source .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env" 2>/dev/null; set +a
fi

GPU_ID="${VLLM_GPU_DEVICES:-2}"
TP_SIZE="${VLLM_TP_SIZE:-1}"
MODEL="${VLLM_MODEL:-openai/gpt-oss-120b}"
MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
QUANTIZATION="${VLLM_QUANTIZATION:-mxfp4}"
PORT=8000
# Configurable runtime knobs (override via env or .env)
# - VLLM_GPU_MEMORY_UTILIZATION: fraction of GPU memory to reserve for vllm (0.0-1.0)
# - VLLM_MAX_NUM_SEQS: optional --max-num-seqs value to reduce sampler warmup
# - VLLM_OFFLOAD_DIR: directory for offload/cache
# - VLLM_PYTORCH_EXPANDABLE: if "true", set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.95}"
VLLM_MAX_NUM_SEQS="${VLLM_MAX_NUM_SEQS:-}"
VLLM_OFFLOAD_DIR="${VLLM_OFFLOAD_DIR:-}${SCRIPT_DIR}/.cache/vllm_offload"
VLLM_PYTORCH_EXPANDABLE="${VLLM_PYTORCH_EXPANDABLE:-false}"

# ---- HuggingFace cache ----
# Prefer /raid (large shared storage), fallback to project dir
if [ -d "/raid" ] && mkdir -p "/raid/s3cache/${USER}/huggingface" 2>/dev/null; then
    export HF_HOME="/raid/s3cache/${USER}/huggingface"
else
    export HF_HOME="${SCRIPT_DIR}/.cache/huggingface"
    mkdir -p "$HF_HOME"
fi

# vLLM offload dir (for CPU weight offload if needed)
OFFLOAD_DIR="$VLLM_OFFLOAD_DIR"
mkdir -p "$OFFLOAD_DIR"

export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN:-}"
export CUDA_VISIBLE_DEVICES="${GPU_ID}"
export VLLM_ATTENTION_BACKEND="FLASH_ATTN"

echo "============================================"
echo "  vLLM Server — GPT-OSS-120B"
echo "============================================"
echo "  GPU(s):       ${GPU_ID}"
echo "  TP size:      ${TP_SIZE}"
echo "  Quantization: ${QUANTIZATION}"
echo "  Model:        ${MODEL}"
echo "  Max context:  ${MAX_MODEL_LEN}"
echo "  Port:         ${PORT}"
echo "  HF cache:     ${HF_HOME}"
echo "  Offload dir:  ${OFFLOAD_DIR}"
echo "  GPU memory util: ${VLLM_GPU_MEMORY_UTILIZATION}"
if [ -n "$VLLM_MAX_NUM_SEQS" ]; then echo "  Max num seqs:  ${VLLM_MAX_NUM_SEQS}"; fi
echo "============================================"

# ---- Create venv (use uv if available, else pip) ----
VENV_DIR="${SCRIPT_DIR}/.vllm-venv"

install_with_uv() {
    echo "  Using uv (fast install)..."
    uv venv "$VENV_DIR" --python python3
    source "$VENV_DIR/bin/activate"
    uv pip install --upgrade pip
    uv pip install --prerelease=allow \
        "vllm>=0.8" \
        --extra-index-url https://wheels.vllm.ai/gpt-oss/ \
        --extra-index-url https://download.pytorch.org/whl/nightly/cu128
}

install_with_pip() {
    echo "  Using pip..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip
    pip install --pre \
        "vllm>=0.8" \
        --extra-index-url https://wheels.vllm.ai/gpt-oss/ \
        --extra-index-url https://download.pytorch.org/whl/nightly/cu128
}

if [ ! -d "$VENV_DIR" ]; then
    echo ""
    echo "Creating Python virtual environment..."
    if command -v uv &> /dev/null; then
        install_with_uv
    else
        install_with_pip
    fi
    echo "✓ vLLM installed"
else
    source "$VENV_DIR/bin/activate"
    echo "  ✓ Using existing venv at ${VENV_DIR}"
fi

echo ""
echo "Starting vLLM server..."
echo "First run will download the model (~60-70GB). Be patient."
echo ""

# Build vllm serve command
VLLM_ARGS=(
    "$MODEL"
    --tensor-parallel-size "$TP_SIZE"
    --max-model-len "$MAX_MODEL_LEN"
    --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION"
    --dtype auto
    --trust-remote-code
    --enforce-eager
    --host 0.0.0.0
    --port "$PORT"
)

# Add quantization if set
if [ -n "$QUANTIZATION" ] && [ "$QUANTIZATION" != "none" ]; then
    VLLM_ARGS+=(--quantization "$QUANTIZATION")
fi

# Add optional max-num-seqs to reduce sampler warmup memory (if provided)
if [ -n "$VLLM_MAX_NUM_SEQS" ]; then
    VLLM_ARGS+=(--max-num-seqs "$VLLM_MAX_NUM_SEQS")
fi

# Optionally set PyTorch allocation policy to reduce fragmentation
if [ "$VLLM_PYTORCH_EXPANDABLE" = "true" ] || [ "$VLLM_PYTORCH_EXPANDABLE" = "1" ]; then
    export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"
    echo "Exported PYTORCH_CUDA_ALLOC_CONF=$PYTORCH_CUDA_ALLOC_CONF"
fi

exec vllm serve "${VLLM_ARGS[@]}"
