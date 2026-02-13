#!/bin/bash
# ===========================================
# Start vLLM on host (outside Docker)
# ===========================================
# Rootless Docker on DGX cannot pass GPUs to containers.
# This script runs vLLM directly on the host, which has
# native GPU access. The Docker containers connect to it
# via host.docker.internal:8000.
#
# Usage: ./start-vllm.sh        (foreground, see logs)
#        ./start-vllm.sh &      (background)
#        nohup ./start-vllm.sh > vllm.log 2>&1 &  (persistent)
# ===========================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# GPU to use (default: GPU 6 which has most free memory)
GPU_ID="${VLLM_GPU_DEVICES:-6}"
MODEL="openai/gpt-oss-120b"
MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
PORT=8000

# HuggingFace cache on /raid to bypass home quota
export HF_HOME="/raid/s3cache/${USER}/huggingface"
export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN:-}"
export CUDA_VISIBLE_DEVICES="${GPU_ID}"
export VLLM_ATTENTION_BACKEND="FLASH_ATTN"

echo "============================================"
echo "  vLLM Server — GPT-OSS-120B"
echo "============================================"
echo "  GPU:        ${GPU_ID}"
echo "  Model:      ${MODEL}"
echo "  Max tokens: ${MAX_MODEL_LEN}"
echo "  Port:       ${PORT}"
echo "  HF cache:   ${HF_HOME}"
echo "============================================"

# Create venv if it doesn't exist
VENV_DIR="${SCRIPT_DIR}/.vllm-venv"
if [ ! -d "$VENV_DIR" ]; then
    echo ""
    echo "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"

    echo "Installing vLLM with GPT-OSS support..."
    pip install --upgrade pip
    pip install --pre \
        "vllm==0.10.1+gptoss" \
        --extra-index-url https://wheels.vllm.ai/gpt-oss/ \
        --extra-index-url https://download.pytorch.org/whl/nightly/cu128

    echo "✓ vLLM installed"
else
    source "$VENV_DIR/bin/activate"
fi

echo ""
echo "Starting vLLM server..."
echo "First run will download the model (~60-70GB). Be patient."
echo ""

exec vllm serve "$MODEL" \
    --tensor-parallel-size 1 \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization 0.92 \
    --dtype auto \
    --trust-remote-code \
    --enable-prefix-caching \
    --host 0.0.0.0 \
    --port "$PORT"
