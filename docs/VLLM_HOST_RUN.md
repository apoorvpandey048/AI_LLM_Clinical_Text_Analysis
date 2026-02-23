# Host-run vLLM (quick guide)

Purpose: reproduce the working host vLLM startup used on DGX and make the Dockerized backend/frontend reach it.

- **Saved startup script:** [vllm_startup_saved.sh](vllm_startup_saved.sh) — use this exact script to reproduce the command that worked (includes HF/TMP envs and CUDA_VISIBLE_DEVICES).
- **Start (recommended):**
  - Ensure RAID cache/offload/tmp exist and are writable (example used: `/raid/s3cache/<user>`).
  - Run: `./vllm_startup_saved.sh` from the repo root.

- **Environment (manual run):**
  - Set HF/TMP to RAID to avoid home quota issues:
    - `export HF_HOME=/raid/s3cache/$USER/huggingface`
    - `export VLLM_CACHE_ROOT=/raid/s3cache/$USER/vllm`
    - `export TMPDIR=/raid/s3cache/$USER/tmp`
  - Choose GPUs and memory knobs:
    - `export CUDA_VISIBLE_DEVICES=0,1,3,6`
    - `export VLLM_GPU_MEMORY_UTILIZATION=0.72`
    - `export VLLM_MAX_NUM_SEQS=8`
  - Then run `./start-vllm.sh --start` (script respects the above env vars).

- **Docker integration:**
  - Containers expect to reach vLLM via `VLLM_HOST`. When vLLM runs on the host, set `VLLM_HOST=http://host.docker.internal:8000` in your compose/overlays so services target the host API.
  - The backend loads this from settings; see [backend/app/config.py](backend/app/config.py) for defaults and how `vllm_host` is used.

- **Troubleshooting:**
  - If you see sampler warmup OOMs, try lowering `VLLM_GPU_MEMORY_UTILIZATION`, reducing `VLLM_MAX_NUM_SEQS`, or choosing a different GPU set via `CUDA_VISIBLE_DEVICES`.
  - Always confirm the APIServer is listening on `0.0.0.0:8000` before starting containers.

If you want, I can also add a `docker-compose.vllm-container.yml` example showing a containerized vLLM with RAID bind-mounts and GPU access.
