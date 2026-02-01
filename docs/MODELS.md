# SNAP-AI Model Guide

This guide provides detailed information on available LLM models for SNAP-AI.

## Quick Reference

### Model Switching

1. Edit `.env` file:
```bash
VLLM_MODEL=Qwen/Qwen2.5-72B-Instruct  # Change this line
```

2. Restart services:
```bash
docker-compose down && docker-compose up -d
```

That's it!

---

## Recommended Models (8x A100 GPUs - 640GB VRAM)

### Tier 1: Best Quality

| Model | HuggingFace ID | VRAM | License | Notes |
|-------|----------------|------|---------|-------|
| **Qwen2.5-72B-Instruct** | `Qwen/Qwen2.5-72B-Instruct` | ~140GB | Apache 2.0 | ⭐ Recommended - Best for clinical text |
| **Llama-3.1-70B-Instruct** | `meta-llama/Llama-3.1-70B-Instruct` | ~140GB | Llama 3.1 | Strong reasoning |
| **DeepSeek-V2.5** | `deepseek-ai/DeepSeek-V2.5` | ~160GB | MIT | Excellent for complex reasoning |
| **Mixtral-8x22B-Instruct** | `mistralai/Mixtral-8x22B-Instruct-v0.1` | ~100GB | Apache 2.0 | Fast MoE architecture |

### Tier 2: Balanced Performance

| Model | HuggingFace ID | VRAM | License | Notes |
|-------|----------------|------|---------|-------|
| **Qwen2.5-32B-Instruct** | `Qwen/Qwen2.5-32B-Instruct` | ~64GB | Apache 2.0 | Faster, still accurate |
| **Gemma-2-27B-IT** | `google/gemma-2-27b-it` | ~55GB | Gemma | Good for German |
| **Llama-3.1-8B-Instruct** | `meta-llama/Llama-3.1-8B-Instruct` | ~16GB | Llama 3.1 | Development testing |

### Quantized Models (Lower VRAM)

| Model | HuggingFace ID | VRAM | Notes |
|-------|----------------|------|-------|
| **Qwen2.5-72B-Instruct-AWQ** | `Qwen/Qwen2.5-72B-Instruct-AWQ` | ~36GB | 4-bit quantized |
| **Llama-2-70B-Chat-AWQ** | `TheBloke/Llama-2-70B-Chat-AWQ` | ~36GB | 4-bit quantized |

---

## Development Models (Ollama)

For local development with limited VRAM:

| Model | Ollama Name | VRAM | Notes |
|-------|-------------|------|-------|
| **Qwen2.5-7B** | `qwen2.5:7b-instruct-q4_K_M` | ~4.5GB | Default dev model |
| **Qwen2.5-14B** | `qwen2.5:14b-instruct-q4_K_M` | ~9GB | Better quality |
| **Llama-3.1-8B** | `llama3.1:8b-instruct-q4_K_M` | ~5GB | Alternative |
| **Mistral-7B** | `mistral:7b-instruct-q4_K_M` | ~4.5GB | Fast inference |

---

## NOT Available for Self-Hosting

| Model | Provider | Reason |
|-------|----------|--------|
| Claude 3/3.5 | Anthropic | API-only, no weights available |
| GPT-4/GPT-4o | OpenAI | API-only, no weights available |
| Gemini Pro | Google | API-only, no weights available |

These models require paid API subscriptions and cannot run on local hardware.

---

## Configuration Examples

### Best Quality (Production)
```bash
LLM_BACKEND=vllm
VLLM_MODEL=Qwen/Qwen2.5-72B-Instruct
LLM_TEMPERATURE=0.0
```

### Fast Processing
```bash
LLM_BACKEND=vllm
VLLM_MODEL=Qwen/Qwen2.5-32B-Instruct
LLM_TEMPERATURE=0.0
```

### Development/Testing
```bash
LLM_BACKEND=ollama
OLLAMA_MODEL=qwen2.5:7b-instruct-q4_K_M
LLM_TEMPERATURE=0.0
```

---

## Model Download

### vLLM (Automatic)
vLLM automatically downloads models from HuggingFace on first run.

### Ollama (Manual)
```bash
# Pull model
docker exec snapai-ollama ollama pull qwen2.5:7b-instruct-q4_K_M

# List available models
docker exec snapai-ollama ollama list
```
