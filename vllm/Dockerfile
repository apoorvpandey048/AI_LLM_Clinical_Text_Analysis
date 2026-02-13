# ===========================================
# Custom vLLM build with GPT-OSS-120B support
# ===========================================
# GPT-OSS-120B requires the special vLLM gptoss build
# that supports MXFP4 quantization and harmony response format.
# Ref: https://huggingface.co/openai/gpt-oss-120b#vllm
#
FROM vllm/vllm-openai:latest

# Install the gpt-oss specific vLLM build on top of the base image
# This adds MXFP4 quantization support and harmony response format handling
RUN pip install --no-cache-dir --pre \
    "vllm==0.10.1+gptoss" \
    --extra-index-url https://wheels.vllm.ai/gpt-oss/ \
    --extra-index-url https://download.pytorch.org/whl/nightly/cu128 \
    || echo "Note: If the gptoss build is already included in vllm:latest, this is a no-op"

# Healthcheck to verify vLLM is serving
HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=20 \
    CMD curl -sf http://localhost:8000/v1/models || exit 1
