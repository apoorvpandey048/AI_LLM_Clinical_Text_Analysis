# SNAP-AI: Implementation & Deployment Plan

**Date**: June 2025 (Updated)  
**Status**: Infrastructure Fixes Applied — Awaiting DGX Redeployment  
**Version**: 3.0

---

## Executive Summary

SNAP-AI is a clinical NLP pipeline that processes pathology text through three LLM layers (CTP → CIE → CCC) using GPT-OSS-120B on university DGX A100 GPUs. The codebase is feature-complete. This plan tracks deployment to DGX-02 and remaining polish items.

**Current State**: All critical infrastructure bugs have been fixed (commit `26a1617`). The app needs a clean redeploy on DGX-02 using the new `restart-prod.sh` script.

---

## Table of Contents

1. [Deployment Status & Next Steps](#1-deployment-status--next-steps)
2. [Infrastructure Fixes Applied](#2-infrastructure-fixes-applied)
3. [DGX Deployment Procedure](#3-dgx-deployment-procedure)
4. [Model Management & Dropdown Bug](#4-model-management--dropdown-bug)
5. [Streaming Output Preservation](#5-streaming-output-preservation)
6. [Worker Count Configuration](#6-worker-count-configuration)
7. [LLM Backend Switching (Ollama/vLLM)](#7-llm-backend-switching-ollamavllm)
8. [Route Coverage Verification](#8-route-coverage-verification)
9. [Prompt Versioning & Persistence](#9-prompt-versioning--persistence)
10. [Error Handling & UX Polish](#10-error-handling--ux-polish)
11. [Performance & Reliability](#11-performance--reliability)
12. [Implementation Checklist](#12-implementation-checklist)

---

## 1. Deployment Status & Next Steps

### What's Done

| Item | Status | Details |
|------|--------|---------|
| Full codebase (backend, frontend, pipeline, workers) | ✅ Complete | FastAPI + Celery + vanilla JS |
| 3-layer clinical NLP pipeline (CTP, CIE, CCC) | ✅ Complete | With GPT-OSS-120B prompts |
| Docker Compose multi-service stack | ✅ Complete | postgres, redis, backend, worker, frontend |
| Host-based vLLM approach (rootless Docker can't pass GPUs) | ✅ Complete | `start-vllm.sh` with CUDA_VISIBLE_DEVICES=6 |
| Codebase audit & 13+ bug fixes | ✅ Complete | Commit history on main branch |
| Frontend JSON parse error fix (502 handling) | ✅ Complete | Commit `9df3884` |
| Infrastructure crash-loop fixes (6 fixes) | ✅ Complete | Commit `26a1617` |
| One-command restart script | ✅ Complete | `restart-prod.sh` |
| **Push all fixes to GitHub** | ✅ Complete | All on `main` branch |

### What Remains

| Item | Priority | Status | Details |
|------|----------|--------|---------|
| **Pull latest on DGX-02 and redeploy** | 🔴 Critical | ❌ Not done | `git pull && ./restart-prod.sh` |
| **Start vLLM on DGX-02 host** | 🔴 Critical | ❌ Not done | `./start-vllm.sh` (needs GPU 6) |
| **Verify end-to-end pipeline** | 🔴 Critical | ❌ Not done | Login → upload → process → results |
| Configure Cloudflare tunnel | 🟡 Medium | ❌ Not done | For remote access via snapai.com |
| Feature polish (dropdown bug, streaming, etc.) | 🟢 Low | ❌ Not done | See sections 4-11 below |

---

## 2. Infrastructure Fixes Applied (Commit `26a1617`)

### Fix 1: Backend crash-loop — stale Postgres volume
- **Problem**: PostgreSQL ignores `POSTGRES_PASSWORD` env var after first initialization. Old volume had password `snapai_dev`, but `.env.production` uses `snapai_prod_2026`.
- **Fix**: `restart-prod.sh` removes the `snapai_postgres_data` volume before starting, forcing a fresh init. Backend `lifespan()` now retries DB connection 6 times × 5s.

### Fix 2: Frontend nginx crash on missing upstream
- **Problem**: nginx resolves `proxy_pass http://backend:8000` at startup. If backend DNS isn't available yet, nginx exits immediately with `host not found in upstream "backend"`.
- **Fix**: Changed `nginx.conf` to use Docker DNS resolver (`resolver 127.0.0.11`) with a variable-based `proxy_pass` (`set $backend_upstream ...`). nginx now resolves at request-time, not startup.

### Fix 3: Worker healthcheck always failing
- **Problem**: The Dockerfile HEALTHCHECK runs `curl localhost:8000/health`, which works for the backend HTTP server but always fails for the Celery worker (no HTTP server).
- **Fix**: `docker-compose.yml` now overrides the worker healthcheck with `celery -A app.workers.celery_app inspect ping`.

### Fix 4: Missing service dependencies
- **Problem**: `depends_on` in compose lacked health conditions. Services started before their dependencies were ready.
- **Fix**: Added `condition: service_healthy` for backend→postgres/redis, worker→postgres/redis, frontend→backend.

### Fix 5: Docker socket mount (rootless Docker)
- **Problem**: `/var/run/docker.sock:/var/run/docker.sock:ro` mount fails in rootless Docker (different socket path).
- **Fix**: Removed the bind mount. The `/api/v1/system/logs` endpoint (Docker log viewer) is non-critical and can be re-added later if needed.

### Fix 6: Wrong VLLM_HOST in .env.production
- **Problem**: `VLLM_HOST=http://vllm:8000` pointed to a Docker container that doesn't exist (vLLM runs on host).
- **Fix**: Changed to `VLLM_HOST=http://host.docker.internal:8000`.

---

## 3. DGX Deployment Procedure

### Prerequisites
- SSH access to DGX-02 (`dbe-sl21-02`)
- Repo cloned at `~/apoo/AI_LLM_Clinical_Text_Analysis`
- HuggingFace model cache at `/raid/s3cache/vincent.ochs/huggingface`

### Step 1: Start vLLM on host (run in a tmux/screen session)
```bash
cd ~/apoo/AI_LLM_Clinical_Text_Analysis
chmod +x start-vllm.sh
./start-vllm.sh
# Wait for "Uvicorn running on http://0.0.0.0:8000" message
# This may take 5-10 minutes for model loading
```

### Step 2: Deploy the app stack (in a separate terminal)
```bash
cd ~/apoo/AI_LLM_Clinical_Text_Analysis
git pull origin main
chmod +x restart-prod.sh
./restart-prod.sh
# Script handles: teardown → volume cleanup → rebuild → ordered startup → health checks
```

### Step 3: Verify
```bash
# Check all containers are healthy
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps

# Test backend health
curl http://localhost:8000/health

# Test frontend
curl -I http://localhost:80

# Check vLLM is responding
curl http://localhost:8000/v1/models
```

### Step 4: (Optional) Cloudflare Tunnel
```bash
# Set the tunnel token in .env.production first:
# CLOUDFLARE_TUNNEL_TOKEN=<your-token>
# Then restart cloudflared:
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d cloudflared
```

---

---

## 4. Model Management & Dropdown Bug

### Current State

**Files**: 
- `backend/app/api/routes/models.py` (248 lines)
- `frontend/src/main.js` (lines 845-1010)

**Architecture**:
- Backend fetches available models from Ollama (`GET /api/tags`)
- Active model stored in Redis (`snapai:active_model`)
- Frontend renders dropdown showing models with checkmarks (pulled) or download icons (unpulled)

### Identified Bug

**Problem**: Model dropdown shows unpulled models as "available" after user cancels pull confirmation dialog.

**Root Cause** (main.js lines 890-940):
```javascript
async function pullModel() {
    const select = document.getElementById('model-dropdown-select');
    const selectedModel = select?.value;
    // ... snip ...
    
    // User cancels here, but we still called loadModels() before confirm
    if (!confirm(`Download model "${selectedModel}"?`)) return;
```

The issue is that `renderModelDropdown()` marks any model as "available" if it exists in `state.availableModels`, but the dropdown options include both pulled and recommended (unpulled) models. When user selects an unpulled model and cancels the pull dialog, the UI doesn't reset the selection state.

### Implementation Tasks

- [ ] **Fix dropdown selection reset**: After cancel, reset `select.value` to current active model
- [ ] **Add visual distinction**: Unpulled models should show "Download required" badge
- [ ] **Prevent processing with unpulled model**: Validate model availability before processing

### Decision MCQs

**Q1: How should unpulled models appear in the dropdown?**

| Option | Description | Trade-off |
|--------|-------------|-----------|
| A | Gray disabled text with lock icon | Clear but might confuse users |
| B | Normal text with "⬇️ Download" suffix | ✅ **Recommended** - Clear action hint |
| C | Hidden from dropdown entirely | Cleaner but requires separate "Add Model" flow |
| D | Red text with warning | Too aggressive |

**Q2: What happens when user selects an unpulled model?**

| Option | Description |
|--------|-------------|
| A | Immediately show pull confirmation dialog |
| B | ✅ **Recommended** - Show inline "Download" button that replaces checkmark |
| C | Auto-start download without confirmation |
| D | Show toast warning and reset selection |

**Q3: Where should "Add New Model" action live?**

| Option | Description |
|--------|-------------|
| A | ✅ **Recommended** - Dropdown item at bottom: "+ Add model..." |
| B | Separate button next to dropdown |
| C | Only in Model Management modal |

---

## 5. Streaming Output Preservation

### Current State

**Files**:
- `backend/app/api/routes/stream.py` (215 lines)
- `frontend/src/main.js` (lines 288-430 - streaming logic)

**Architecture**:
- SSE streaming via EventSource
- Tokens buffered in `state.liveOutputBuffers` per layer
- Buffers **cleared on each case completion** via `resetLiveOutput()`
- Final results fetched from `/jobs/{id}/results` (no streaming data)

### Identified Gap

**Problem**: Streaming output is discarded after processing completes. Users cannot review the raw LLM responses or the "typewriter" experience.

**Affected code** (main.js lines 326-330):
```javascript
state.eventSource.addEventListener('case_complete', (e) => {
    // ...
    resetLiveOutput();  // <-- Clears all buffers!
    updateCaseProgress(data);
});
```

### Implementation Tasks

- [ ] **Preserve streaming buffers**: Don't reset on case_complete; accumulate per case
- [ ] **Store raw outputs in results**: Add raw LLM output to case results endpoint
- [ ] **Add "View Raw Output" tab**: New UI component in results view
- [ ] **Toggle formatted vs raw**: Let users switch between parsed JSON and raw text

### Decision MCQs

**Q1: How should preserved streaming output be accessed?**

| Option | Description |
|--------|-------------|
| A | ✅ **Recommended** - "Raw Output" tab in results card for each case |
| B | Single "View All Raw" button that opens modal |
| C | Separate page/section for raw outputs |
| D | Download as separate file |

**Q2: Should raw output be stored server-side or client-side only?**

| Option | Description | Trade-off |
|--------|-------------|-----------|
| A | Client-side only (in memory) | Simpler, but lost on page refresh |
| B | ✅ **Recommended** - Server-side in JobCase model | Persistent, adds storage cost |
| C | Optional server-side toggle | Complex to implement |

**Q3: What format for raw output display?**

| Option | Description |
|--------|-------------|
| A | Plain text with layer headers |
| B | ✅ **Recommended** - Syntax-highlighted markdown/text with copy button |
| C | JSON array of tokens |

### Schema Change Required

```python
# backend/app/db/models.py - JobCase model
class JobCase(Base):
    # ... existing fields ...
    layer1_raw_output: Mapped[str | None]  # New
    layer2_raw_output: Mapped[str | None]  # New
    layer3_raw_output: Mapped[str | None]  # New
```

---

## 6. Worker Count Configuration

### Current State

**Files**:
- `backend/app/config.py` (line ~55): `api_workers: int = 4`
- `backend/app/api/routes/system.py` (line ~80): Returns worker_count

**Architecture**:
- Worker count is static config via environment variable `API_WORKERS`
- System info endpoint attempts Celery inspect, falls back to config value
- No UI to modify workers

### Identified Gap

- Worker count is display-only in system info bar
- Cannot adjust concurrency at runtime
- Celery supports runtime concurrency adjustment via `celery control concurrency`

### Implementation Tasks

- [ ] **Add worker count adjustment endpoint**: `PUT /api/v1/system/workers`
- [ ] **Add Celery control integration**: Send concurrency control commands
- [ ] **Add UI control**: Slider or input in settings panel
- [ ] **Validate limits**: Min 1, max based on available resources

### Decision MCQs

**Q1: Should worker count be adjustable at runtime?**

| Option | Description |
|--------|-------------|
| A | ✅ **Recommended** - Yes, with limits (1-8) |
| B | No, require restart to change |
| C | Only allow decrease, not increase |

**Q2: Where should worker config UI live?**

| Option | Description |
|--------|-------------|
| A | System info bar (inline) |
| B | ✅ **Recommended** - Settings panel (separate page/modal) |
| C | Advanced settings hidden behind toggle |

**Q3: How to handle worker adjustment failures?**

| Option | Description |
|--------|-------------|
| A | Silent fallback to current value |
| B | ✅ **Recommended** - Toast error with explanation |
| C | Block UI until resolved |

### Implementation Note

Celery concurrency control:
```python
from celery.app.control import Control

def set_worker_concurrency(n: int):
    control = Control(celery_app)
    # This only works with prefork pool
    control.pool_grow(n)  # or pool_shrink
```

**Caveat**: This only works with prefork pool. If using gevent/eventlet, different approach needed.

---

## 7. LLM Backend Switching (Ollama/vLLM)

### Current State

**Files**:
- `backend/app/config.py`: `llm_backend: str = "ollama"`
- `backend/app/llm/client.py`: Factory returns OllamaClient or VLLMClient
- `docker-compose.yml` / `docker-compose.prod.yml`: Different services per env

**Architecture**:
- Backend type set via `LLM_BACKEND` env var at startup
- No runtime switching supported
- vLLM config completely separate from Ollama

### User Question: "Is this feature even useful?"

**Analysis**:
- **Development**: Users typically only use Ollama locally
- **Production**: Typically locked to vLLM for performance
- **Hybrid use case**: Rare - switching mid-session isn't common

**Recommendation**: This is LOW PRIORITY. The current environment-based switching is sufficient.

### Decision MCQ

**Q: Should runtime LLM backend switching be implemented?**

| Option | Description |
|--------|-------------|
| A | Yes, full runtime switching with UI | Complex, tests required for both |
| B | ✅ **Recommended** - No, keep env-based switching | Simpler, clearer deployment |
| C | Yes, but admin-only with confirmation | Middle ground |

**If proceeding with Option A** (not recommended):

- [ ] Add backend endpoint: `PUT /api/v1/system/llm-backend`
- [ ] Store selection in Redis
- [ ] Update client factory to check Redis first
- [ ] Add UI toggle in settings
- [ ] Add connection test button per backend

---

## 8. Route Coverage Verification

### Complete Route Audit

| Route | Method | Status | Notes |
|-------|--------|--------|-------|
| `/api/v1/upload` | POST | ✅ Working | File + text upload |
| `/api/v1/upload/text` | POST | ✅ Working | Text-only endpoint |
| `/api/v1/jobs` | GET | ✅ Working | List jobs with pagination |
| `/api/v1/jobs/{id}` | GET | ✅ Working | Job status + case list |
| `/api/v1/jobs/{id}/results` | GET | ✅ Working | Full results with outputs |
| `/api/v1/jobs/{id}/reprocess` | POST | ✅ Working | Re-run processing |
| `/api/v1/jobs/{id}` | DELETE | ✅ Working | Soft delete |
| `/api/v1/stream/{job_id}` | GET | ✅ Working | SSE streaming |
| `/api/v1/models` | GET | ✅ Working | List available models |
| `/api/v1/models/active` | GET/PUT | ✅ Working | Active model management |
| `/api/v1/models/pull` | POST | ✅ Working | Pull new model |
| `/api/v1/models` | DELETE | ✅ Working | Delete model |
| `/api/v1/prompts` | GET | ✅ Working | List prompt templates |
| `/api/v1/prompts/{layer}` | GET/PUT | ✅ Working | Get/Update prompt |
| `/api/v1/prompts/{layer}/reset` | POST | ✅ Working | Reset to default |
| `/api/v1/system/info` | GET | ✅ Working | System status |
| `/api/v1/system/health` | GET | ✅ Working | Health check |
| `/health` | GET | ✅ Working | Simple health |

### Missing Routes (Recommended)

| Route | Method | Purpose | Priority |
|-------|--------|---------|----------|
| `/api/v1/system/workers` | PUT | Adjust worker count | Medium |
| `/api/v1/models/{name}/info` | GET | Detailed model info | Low |
| `/api/v1/jobs/{id}/cancel` | POST | Cancel running job | High |
| `/api/v1/jobs/{id}/export` | GET | Export case as DOCX | Medium |

### Implementation Tasks

- [ ] **Add job cancellation**: `/jobs/{id}/cancel` with Celery revoke
- [ ] **Add export endpoint**: Generate DOCX report for job
- [ ] **Add model details**: Return full Ollama model info

---

## 9. Prompt Versioning & Persistence

### Current State

**Files**:
- `backend/app/api/routes/prompts.py` (256 lines)
- `backend/app/db/models.py`: `PromptTemplate` model
- `prompts/snapai/*.md`: Default templates

**Architecture**:
- Defaults loaded from markdown files on disk
- Custom prompts stored in DB with `is_active` flag
- Versioning via `version`, `label`, `updated_at` fields
- Frontend caches prompts in `state.promptData`

### Verification Checklist

| Feature | Status | Notes |
|---------|--------|-------|
| Load defaults from files | ✅ Working | Falls back to file content |
| Save custom to DB | ✅ Working | Via PUT endpoint |
| Version tracking | ✅ Working | Stored in DB |
| Reset to default | ✅ Working | Deletes DB record |
| UI dirty state | ✅ Working | Shows save button on change |
| Cross-tab sync | ⚠️ Partial | Caches locally, no real-time sync |

### Identified Gaps

1. **No version history**: Only current version stored, no rollback
2. **No import/export**: Can't backup custom prompts easily
3. **No validation**: No schema/format validation on prompts

### Decision MCQs

**Q1: Should prompt version history be added?**

| Option | Description |
|--------|-------------|
| A | No, current version only | Simpler, current behavior |
| B | ✅ **Recommended** - Yes, keep last 5 versions | Balance storage vs utility |
| C | Yes, unlimited history | Storage concern |

**Q2: Should prompt import/export be added?**

| Option | Description |
|--------|-------------|
| A | ✅ **Recommended** - Yes, JSON export/import in UI | Easy backup |
| B | No, DB backup is sufficient |
| C | Yes, markdown file export | More readable |

### Implementation Tasks (if version history selected)

- [ ] **Add PromptVersion model**: FK to PromptTemplate, stores historical content
- [ ] **Modify PUT endpoint**: Create version before update
- [ ] **Add history endpoint**: `GET /prompts/{layer}/history`
- [ ] **Add rollback endpoint**: `POST /prompts/{layer}/rollback/{version_id}`
- [ ] **Add UI version viewer**: List historical versions with diff view

---

## 10. Error Handling & UX Polish

### Current State

Error handling is implemented but can be improved:

| Area | Status | Notes |
|------|--------|-------|
| Network errors | ✅ Working | Shows toast |
| Processing errors | ✅ Working | Shows error card with actions |
| Validation errors | ⚠️ Partial | Some 422 errors not user-friendly |
| Connection loss | ⚠️ Partial | Falls back to polling but no clear indication |
| Model not available | ⚠️ Partial | Error message could be clearer |

### Implementation Tasks

- [ ] **Improve validation error messages**: Parse FastAPI 422 detail array better
- [ ] **Add connection status indicator**: Show live/reconnecting/offline badge
- [ ] **Add retry logic**: Auto-retry transient failures with backoff
- [ ] **Improve empty states**: Better UI when no jobs/no models

### Decision MCQ

**Q: How should connection loss be displayed?**

| Option | Description |
|--------|-------------|
| A | Toast notification |
| B | ✅ **Recommended** - Status bar indicator (yellow → red) |
| C | Full-screen overlay |
| D | Just log to console |

---

## 11. Performance & Reliability

### Identified Optimization Opportunities

| Area | Current | Improvement |
|------|---------|-------------|
| Model loading | Fetched on every dropdown render | Cache with TTL |
| System info | Polled every page load | Cache for 30s |
| Prompts | Cached in state | Add localStorage backup |
| SSE reconnect | Manual reload required | Auto-reconnect with backoff |

### Implementation Tasks

- [ ] **Add request caching**: Use service worker or in-memory cache
- [ ] **Add SSE auto-reconnect**: With exponential backoff
- [ ] **Add loading skeletons**: Better perceived performance
- [ ] **Optimize DB queries**: Add indexes for common filters

---

## 12. Implementation Checklist

### Priority 0: Deployment (CURRENT FOCUS)
- [x] Fix backend crash-loop (stale volume + DB retry logic)
- [x] Fix nginx crash on missing upstream (Docker DNS resolver)
- [x] Fix worker healthcheck (Celery inspect ping)
- [x] Fix service dependency ordering (depends_on with health conditions)
- [x] Fix Docker socket mount for rootless Docker
- [x] Fix VLLM_HOST in .env.production
- [x] Create one-command restart script (`restart-prod.sh`)
- [x] Push all fixes to GitHub
- [ ] **Pull latest on DGX-02 and run `restart-prod.sh`**
- [ ] **Start vLLM on host with `start-vllm.sh`**
- [ ] **Verify end-to-end: login → upload → process → view results**

### Priority 1: Critical Fixes
- [ ] Fix model dropdown showing unpulled as available after cancel
- [ ] Add job cancellation endpoint
- [ ] Improve error message formatting

### Priority 2: High Value Features
- [ ] Preserve streaming output for review
- [ ] Add raw output view in results
- [ ] Add worker count display/adjustment

### Priority 3: Polish
- [ ] Add connection status indicator
- [ ] Improve empty states
- [ ] Add prompt version history
- [ ] Add export improvements

### Priority 4: Defer/Skip
- [ ] Runtime LLM backend switching (keep env-based)
- [ ] Real-time cross-tab prompt sync

---

## Quick Reference: File Locations

| Component | File Path |
|-----------|-----------|
| Models API | `backend/app/api/routes/models.py` |
| Prompts API | `backend/app/api/routes/prompts.py` |
| System API | `backend/app/api/routes/system.py` |
| Jobs API | `backend/app/api/routes/jobs.py` |
| Streaming | `backend/app/api/routes/stream.py` |
| Upload | `backend/app/api/routes/upload.py` |
| Celery Tasks | `backend/app/workers/tasks.py` |
| DB Models | `backend/app/db/models.py` |
| Config | `backend/app/config.py` |
| Frontend JS | `frontend/src/main.js` |
| Frontend CSS | `frontend/src/styles.css` |
| Frontend HTML | `frontend/index.html` |

---

## Appendix: Test Scenarios

### Model Dropdown Bug Verification

1. Open Model Management
2. Select an unpulled model from dropdown
3. Click Cancel on confirmation dialog
4. **Bug**: Dropdown still shows selected model
5. **Expected**: Should reset to active model

### Streaming Preservation Verification

1. Start processing with valid clinical text
2. Watch streaming output during processing
3. Wait for completion
4. **Bug**: Cannot access raw streaming output
5. **Expected**: "Raw Output" tab available in results

---

*End of Implementation Plan*
