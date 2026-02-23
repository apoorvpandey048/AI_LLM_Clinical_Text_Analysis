#!/usr/bin/env bash
# Saved vLLM startup configuration -- successful run (for future reference)
# DO NOT run this automatically unless you understand the envs and GPUs used.
# Timestamp: 2026-02-23

export HF_HOME=/raid/s3cache/vincent.ochs/huggingface
export VLLM_CACHE_ROOT=/raid/s3cache/vincent.ochs/vllm
export CUDA_VISIBLE_DEVICES=0,1,3,6

mkdir -p "$HF_HOME" "$VLLM_CACHE_ROOT" /raid/s3cache/vincent.ochs/vllm/offload

# vLLM command that worked (started API on http://0.0.0.0:8000)
vllm serve openai/gpt-oss-120b \
  --tensor-parallel-size 4 \
  --max-model-len 8192 \
  --quantization mxfp4 \
  --gpu-memory-utilization 0.72 \
  --max-num-seqs 8 \
  --enforce-eager \
  --dtype auto \
  --host 0.0.0.0 \
  --port 8000

# Example: run in background and log
# nohup bash ./vllm_startup_saved.sh > vllm.log 2>&1 &

# Notes:
# - Adjust `CUDA_VISIBLE_DEVICES` to the GPUs you can use.
# - If you need lower memory reservation, reduce --gpu-memory-utilization
#   (e.g. 0.25 or 0.15) and/or reduce --max-num-seqs.
# - The file is stored for reproducibility/history only.
