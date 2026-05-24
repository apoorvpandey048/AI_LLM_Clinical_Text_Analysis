# CANONICAL_RUNTIME.md

**Status:** AUTHORITATIVE for the `feature/excel-batch-fanout` line of work.
**Decided:** 2026-05-24 · **Owner:** Agent A (Runtime & Infrastructure)
**Scope:** Defines the ONE runtime that all Excel-batch work targets. Every other
deployment path in the repo is declared **secondary** or **deprecated** below.

---

## 1. The decision (TL;DR)

| Question | Canonical answer |
|---|---|
| Compose invocation | `docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml up -d --build` |
| What that resolves to | `docker-compose.dev.yml` (base, via `include:`) **+** `docker-compose.local-gpu.yml` (overlay) |
| LLM backend | **vLLM**, `GPT-OSS-120B`, **running on the host GPU OUTSIDE Docker** |
| vLLM launch | `./start-vllm.sh` (host) — see §5 for the port caveat |
| How backend/worker reach vLLM | `host.docker.internal` via `extra_hosts: host-gateway`, URL from `.env` `VLLM_HOST` |
| Worker launch | `celery -A app.workers.celery_app worker --concurrency=1 --max-tasks-per-child=50` (baked into the overlay; changed in Phase 3 — see §6) |
| Frontend URL | `http://localhost:8080` (login `admin` / `ADMIN_PASSWORD` from `.env`) |
| Backend (dev debug) | `http://localhost:8081` (dev base maps host `8081 -> container 8000`) |
| Config source of truth | `.env` (auto-read by `docker compose`); overrides every compose default |

> **Why local-gpu and not the others:** the PRIMARY OBJECTIVE and the GPU-SAFETY
> section of the brief both state the vLLM server runs on a local GPU outside
> Docker. `docker-compose.local-gpu.yml` is the *only* overlay that (a) neutralizes
> Ollama, (b) sets `LLM_BACKEND=vllm`, and (c) wires `host.docker.internal` to the
> host GPU. It is therefore THE runtime. See §3 for the full disposition table.

---

## 2. Boot sequence (the only supported path)

```bash
# 0. From the repo root: /mnt/c/Users/Apoor/n/snap-ai
cd /mnt/c/Users/Apoor/n/snap-ai

# 1. Start vLLM on the host GPU (outside Docker). Foreground to watch logs:
./start-vllm.sh
#    ...or persistent:
nohup ./start-vllm.sh > vllm.log 2>&1 &
#    Wait until it logs "Application startup complete" and GET /v1/models returns 200.

# 2. Boot the stack (Docker):
docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml up -d --build

# 3. Open the app:
#    http://localhost:8080   (login: admin / <ADMIN_PASSWORD from .env>)

# 4. Tail logs while a batch runs:
docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml logs -f worker backend
```

**Stop:**
```bash
docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml down
pkill -f 'vllm serve'   # stop host vLLM
```

**After changing worker code (tasks.py / pipeline / ingestion):** Celery does **not**
hot-reload. Restart the worker:
```bash
docker compose -f docker-compose.yml -f docker-compose.local-gpu.yml restart worker
```
(The backend API *does* hot-reload under the dev base via `--reload`, mounted `./backend/app`.)

---

## 3. Compose file disposition

| File | Disposition | Reason |
|---|---|---|
| `docker-compose.local-gpu.yml` | ✅ **CANONICAL (overlay)** | Host-GPU vLLM, Ollama disabled, `host.docker.internal` wiring. The target runtime. |
| `docker-compose.dev.yml` | 🟡 **CANONICAL BASE + secondary** | Used as the base layer under the overlay. Standalone (`-f docker-compose.dev.yml` only) it boots an **Ollama** dev stack — keep ONLY for no-GPU smoke tests / frontend work. Not for batch. |
| `docker-compose.yml` | 🟡 **Alias** | Just `include: docker-compose.dev.yml`. Exists so `-f docker-compose.yml` = the base. Do not extend it. |
| `docker-compose.prod.yml` | ⛔ **DEPRECATED for this work** | Standalone DGX/Cloudflare prod stack (bundles `cloudflared`). Overlaps local-gpu but adds tunnel + read-only mounts. Out of scope; do not target. |
| `docker-compose.remote.yml` | ⛔ **DEPRECATED for this work** | Runs **Ollama in Docker with GPU reservation** — contradicts "vLLM on host." Do not target. |

**Rule:** all Phase 2–5 changes that touch runtime config land in `docker-compose.local-gpu.yml`
(and `.env` / `.env.example`). We do **not** keep prod/remote in sync — they are explicitly
out of scope and marked deprecated *in their header comments* during Phase 3 (we do **not**
delete them; see RISK_REGISTER R-12).

---

## 4. Deploy / helper scripts disposition

All of these are **obsolete or out-of-scope** for the canonical local-GPU runtime. We do
**not** delete them (they hold deployment history); we mark the obsolete ones with a
deprecation banner in Phase 3.

| Script | Disposition |
|---|---|
| `start-vllm.sh` | ✅ **CANONICAL** — the host vLLM launcher (but see §5 port caveat). |
| `deploy.sh`, `deploy-prod.sh`, `deploy-dgx.sh`, `restart-prod.sh`, `run-dgx.sh` | ⛔ Deprecated — multi-path prod/DGX deploy orchestration; superseded by the single compose invocation. |
| `setup-vllm.sh`, `vllm_startup_saved.sh` | ⛔ Deprecated — older vLLM bootstrap variants; superseded by `start-vllm.sh`. |
| `tunnel.sh`, `cloudflare-setup.sh` | ⛔ Out of scope — public tunnel; not needed locally. |
| `celery_inspect.py`, `celery_shutdown.py`, `check_redis.py`, `post_restart_check.py` | 🟡 Keep — handy local ops one-offs; not part of boot. |
| `e2e_submit.py`, `e2e_case7.py`, `compare_parse.py` | 🟡 Keep — manual e2e probes; QA (Agent E) may reuse. |

---

## 5. ⚠️ Live drift to reconcile (Agent A, Phase 3)

These are real inconsistencies found in the working tree on 2026-05-24. They are **not**
fabricated by this work; documenting so they are fixed deliberately, not silently.

1. **vLLM port mismatch (HIGH).** `start-vllm.sh` hardcodes `PORT=8000` (line 37), but
   `.env` sets `VLLM_HOST=http://host.docker.internal:8005` (the last commit `a975a81`
   "change vllm port to 8005 to avoid address in use conflicts" updated `.env` but **not**
   the script). As-is, the backend dials `:8005` while vLLM listens on `:8000`.
   **Fix:** make `start-vllm.sh` honor a `VLLM_PORT` env (default `8000`) and set
   `VLLM_PORT=8005` + `VLLM_HOST=...:8005` consistently in `.env`/`.env.example`.
2. **Stale default host in overlay (MED).** `docker-compose.local-gpu.yml` defaults
   `VLLM_HOST` to a hardcoded remote IP `http://131.152.136.65:8000`. Harmless while `.env`
   overrides it, but misleading. **Fix:** change the overlay default to
   `http://host.docker.internal:8005`.
3. **`max_tokens` drift (LOW).** Overlay default `LLM_MAX_TOKENS=16384`; `.env` sets `4096`.
   `.env` wins. Confirm 4096 is intended for the 120B model (affects per-case latency/GPU).
4. **`REDIS_PASSWORD` empty in `.env` (LOW).** Both dev and overlay support empty (auth off);
   intentional for local. Leave as-is; do not introduce a password requirement locally.

---

## 6. Runtime changes this feature WILL make (and why)

These are introduced in Phase 3 (Agent A) and are listed here so the canonical runtime
doc stays the single source of truth. Full rationale in ARCHITECTURE_DIFF.md / IMPLEMENTATION_PLAN.md.

- **Queue topology:** introduce two Celery queues — `cases` (batch fan-out) and
  `interactive` (single-case / ad-hoc jobs) — so a 156-case batch cannot starve a clinician's
  one-off run. Default queue stays for the lightweight coordinator/finalize tasks.
- **Worker launch:** the canonical worker consumes `-Q cases,interactive,celery` with
  `--concurrency=2` (start value; tuned by Agent E against measured vLLM headroom on the
  single A100). `--max-tasks-per-child=50` is retained and now *actually* recycles memory,
  because each task is one case (not one 156-case batch).
- **Result backend:** case tasks become `ignore_result=True`; results live in Postgres only.
  Redis carries broker messages + pub/sub stream + light coordination, eliminating the
  multi-MB-per-job result bloat under the 2 GB `allkeys-lru` cap.
- **SSE longevity:** the hard `timeout=3600` (stream.py:42, stream_manager.py:172) is raised
  and made env-configurable so live streaming survives a multi-hour batch; DB polling remains
  the durable fallback.
- **GPU governor:** `--concurrency` is the primary throttle. An optional Redis semaphore caps
  *total* simultaneous vLLM requests (recall each case may fire 3 parallel L3 calls), sized to
  what the 40 GB A100 serves without latency collapse. Default OFF; enabled only if Phase 5
  measurements show oversubscription.

---

## 7. Developer quick-reference

```bash
# Canonical up / down / logs / worker-restart
ENV="-f docker-compose.yml -f docker-compose.local-gpu.yml"
docker compose $ENV up -d --build
docker compose $ENV logs -f worker
docker compose $ENV restart worker
docker compose $ENV down

# No-GPU smoke test (frontend/API only, Ollama or stub LLM) — NOT for batch:
docker compose -f docker-compose.dev.yml up -d --build   # backend at :8081, app at :8080

# Host vLLM
./start-vllm.sh            # foreground
pkill -f 'vllm serve'      # stop
```

**Assumption (labeled):** single host, single A100-40GB, vLLM already installable on the host
(venv/conda per `start-vllm.sh`). Multi-GPU / multi-node and the Cloudflare tunnel are out of
scope for this feature.
