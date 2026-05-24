# ARCHITECTURE_DIFF.md

**What changes, what stays, and exactly where.** All file:line anchors verified against
`feature/excel-batch-fanout` @ `a975a81` on 2026-05-24.

Guiding rule (from the brief): **build the minimum architectural change** to safely process
156 Excel cases on one GPU. Keep the orchestrator, layer logic, DB schema, SSE model, and
snapshot/replay. Change only ingestion, work orchestration, task granularity, resumability,
and runtime clarity.

---

## 0. KEEP — explicitly NOT touched

| Component | Location | Why preserved |
|---|---|---|
| Pipeline orchestrator | `backend/app/pipeline/orchestrator.py` (`async execute(...)` @ 147) | Core IP; fan-out calls it unchanged via the existing `_process_case_impl` path. |
| Layer modules (L1/L2/L3 + aggregators) | `backend/app/pipeline/layer*.py` | Clinical logic is correct & frozen. |
| DB schema *structure* | `backend/app/db/models.py` (`Job` @109, `JobCase` @195) | Reused as-is; the only additive change is an optional progress/retry bookkeeping column (see §4). |
| SSE streaming model | `stream.py`, `utils/stream_manager.py`, Redis channel `snapai:stream:{job_id}` | Transport unchanged; only the 1 h timeout is lifted. |
| Snapshot / replay | `_build_pipeline_snapshot` (tasks.py:316), `Job.pipeline_snapshot` (models.py:145) | Determinism guarantee; coordinator reuses it. |
| `save_case_result` / metrics | tasks.py:412 / `save_layer_metric` | Persistence path stays; it's already DB-first. |

---

## 1. Ingestion path (Agent B, Phase 2)

### BEFORE
- `upload.py:34` — `ALLOWED_EXTENSIONS = {".docx", ".pdf", ".txt"}` — **no `.xlsx`.**
- `upload.py:37-71` — `parse_cases_from_text(...)` splits cases by **regex header match**:
  ```python
  pattern = r'(?:^|\n)\s*(Case\s*#?\s*\d+|Fall\s*#?\s*\d+|Patient\s*#?\s*\d+)'
  ```
  Correct for free text, **wrong for a spreadsheet** (one row = one case, no headers).
- `requirements.txt` — **no `openpyxl` / `pandas`.**
- `upload.py:337-340` — enqueues the whole case list (with full text) into one task.

### AFTER
- **New module** `backend/app/ingestion/excel.py` (Agent B owns) — pure functions, no FastAPI deps:
  - `parse_workbook(path|bytes) -> list[CaseRecord]` using **openpyxl read-only**.
  - Row→case mapping keyed by the `ID` column (`^OLC-CHI-\d{9}$`); `case_label = ID`.
  - **Labeled-section concatenation** (NOT regex splitting) builds `input_text`:
    ```
    Diagnosen:
    <text>

    Operationen:
    <text>
    ...
    ```
    Skips empty/whitelisted-empty columns; strips `_x000D_`; normalizes bullets/tabs; keeps
    meaningful embedded newlines. Column→label map is an explicit constant (no header regex).
  - `validate(records) -> ValidationReport` (row count, duplicate/missing IDs, empty-core-text rows).
- `requirements.txt` — **add `openpyxl==3.1.5`** (no pandas).
- `upload.py` — add `.xlsx` to `ALLOWED_EXTENSIONS`; when the uploaded file is `.xlsx`, route to
  the Excel parser instead of `parse_cases_from_text`; surface the validation report to the caller
  before enqueue. **`parse_cases_from_text` is untouched** (docx/pdf/txt keep working — backward compatible).

---

## 2. Work orchestration (Agents C + A, Phase 3) — the core fix

### BEFORE — one monolithic task
- `tasks.py:882` — `@celery_app.task(bind=True, name="process_batch")`, **no `ignore_result`**.
- `tasks.py:979-1059` — single sequential `for i, case in enumerate(cases):` loop, **no
  skip-completed guard**; per-case cancellation check only.
- `tasks.py:1144-1152` — `return {... "results": results}` — embeds **every** case's full result
  (incl. `layer_stream_data`) → stored in the Redis result backend (`result_expires=86400`,
  2 GB `allkeys-lru`).
- `celery_app.py` — `task_time_limit=3660` (61 min), `task_acks_late=True`,
  `task_reject_on_worker_lost=True`, `worker_concurrency=1`, no queues.
- **Net effect:** 156 cases ≈ 6.5–8 h ≫ 61 min ⇒ SIGKILL ⇒ `acks_late` redelivery ⇒ restart
  from case #1 ⇒ livelock. (Risks R-1, R-2.)

### AFTER — coordinator + per-case fan-out
New tasks in `tasks.py` (additive; `process_batch` retained as a thin shim — see §6):

```
start_job(job_id)                      # lightweight coordinator, default queue, no LLM
  ├─ build/load snapshot once (reuse _build_pipeline_snapshot)
  ├─ for each case in (QUEUED | FAILED): process_one_case.apply_async(args=[case_id], queue="cases")
  └─ (Celery chord) callback -> finalize_job(job_id)

process_one_case(case_id)              # queue="cases", short (~3 min), ignore_result=True
  ├─ if case.status == COMPLETED: return            # idempotent skip  ← enables resume
  ├─ if job.status in (STOPPING, CANCELLED): return # honor cancellation
  ├─ set PROCESSING -> _process_case_impl(...)      # UNCHANGED orchestrator call
  ├─ stream tokens to Redis channel (UNCHANGED)
  ├─ save_case_result(...)  -> DB ONLY (NOT in return value)
  └─ autoretry_for=(LLMError, TimeoutError), retry_backoff=True, max_retries=2
       final failure -> mark case FAILED, batch continues

finalize_job(job_id)                   # recompute counts from DB, set COMPLETED/partial, publish 'complete'
```

Celery config changes (`celery_app.py`):
- `task_routes`: `process_one_case -> cases`, `*interactive* -> interactive`, coordinator/finalize -> default.
- Case task: `acks_late=True` now means a crash re-runs **only the in-flight case**.
- `worker_concurrency=2` (start; Agent E tunes). Worker consumes `-Q cases,interactive,celery`.
- Per-case `ignore_result=True` (or strip `results` from any returned dict).
- Keep `task_time_limit` as a *safety net* but it is no longer load-bearing (each case ≪ limit).

### Resume (Agent C)
- **New endpoint** `POST /api/v1/jobs/{id}/resume` (`jobs.py`) → re-invokes `start_job(job_id)`,
  which (because of the skip-completed guard) re-enqueues only `QUEUED`/`FAILED` cases.
  Crash recovery and manual retry collapse into the same idempotent path. **No `/resume`
  exists today** (verified — absent from `jobs.py` and `upload.py`).

---

## 3. Streaming longevity (Agent A, Phase 3) + resilience (Agent D, Phase 4)

### BEFORE
- `stream.py:42` `generate_job_events(..., timeout=3600)`; check at 113-116 breaks the stream.
- `stream_manager.py:172` `StreamSubscriber.subscribe(..., timeout=3600)`; breaks at 200-203.
- ⇒ live tokens die ~1 h into a multi-hour batch (UI silently degrades to 4 s polling).

### AFTER
- Make the timeout env-configurable (`SSE_STREAM_TIMEOUT`, default raised well past worst-case
  batch wall-clock, e.g. 8 h, or 0 = unbounded with heartbeat-driven liveness).
- **Frontend (Agent D):** transparent EventSource reconnect that re-subscribes on `timeout`/drop
  and reconciles from `/jobs/{id}/status`; the existing 5-attempt backoff + 4 s polling fallback
  is preserved as the durable floor. No framework change — still vanilla JS in `frontend/src/main.js`.

---

## 4. Database (minimal, additive only)

- **Structure preserved.** Job/JobCase reused; statuses reused (`JobStatus`: QUEUED, PROCESSING,
  STOPPING, COMPLETED, FAILED, CANCELLED; `CaseStatus`: QUEUED, PROCESSING, COMPLETED, FAILED).
- **Optional additive column** `job_cases.retry_count INTEGER DEFAULT 0` (only if retry visibility
  is needed in the UI) via the existing idempotent startup migrator pattern in
  `main.py:_run_schema_migrations` (`ADD COLUMN IF NOT EXISTS`). No destructive migration.
- **`final_cci` type check (R-10):** model declares `Float` (models.py:248); the startup migrator
  historically added it as `INTEGER`. Agent E verifies the live column type; reconcile via the
  migrator if drifted. No schema *restructure*.
- **Pagination (deferred / nice-to-have):** `/jobs/{id}/results` loads all cases unpaginated;
  flagged in RISK_REGISTER (R-8) but **not required** for correctness of the 156-case batch.

---

## 5. Redis memory (Agent A, Phase 3)

| | BEFORE | AFTER |
|---|---|---|
| Task result payload | Full per-case results incl. `layer_stream_data` (tens of MB/job) | `ignore_result=True` → nothing fat in the backend |
| Source of truth | Ambiguous (Redis result + Postgres) | **Postgres only** |
| Redis role | broker + result backend (bloated) + pub/sub + KV | broker + pub/sub + light coordination |
| Eviction risk | `allkeys-lru` can purge **broker** keys under bloat → message loss | Removed (no bloat) |

---

## 6. Backward compatibility & shims

- `process_batch` is **kept** as a thin wrapper that delegates to `start_job` (so existing
  callers — replay/reprocess paths, `upload.py:337` — keep working). New uploads call the
  fan-out path directly. This avoids breaking the replay/reprocess re-entry described in the
  context doc §3.
- docx/pdf/txt ingestion via `parse_cases_from_text` is **unchanged**.
- Deprecated runtime files get banner comments (not deletion) — see CANONICAL_RUNTIME §3–4.

---

## 7. Component change-map (one glance)

```
Frontend (vanilla JS)         ── add .xlsx upload + batch dashboard + SSE reconnect (D)
  │ SSE (timeout lifted A)
FastAPI upload.py             ── +.xlsx route -> ingestion/excel.py (B);  +/resume (C)
  │ enqueue start_job (C)
Celery (celery_app.py)        ── +queues cases/interactive, +routes, concurrency=2 (A)
  ├ start_job (C)             [NEW coordinator]
  ├ process_one_case (C)      [NEW, ignore_result, idempotent, retry]  ── calls ↓ UNCHANGED
  │     PipelineOrchestrator.execute(...)   [KEEP]
  ├ finalize_job (C)          [NEW chord callback]
  └ process_batch (C)         [SHIM -> start_job, kept for replay/back-compat]
  │ save_case_result -> Postgres [KEEP]    (results no longer in Redis backend — A)
PostgreSQL                    ── source of truth [KEEP]; +retry_count col optional (C)
Redis                         ── broker + pub/sub only (A)
vLLM (host GPU)               ── concurrency governor / queue separation (A) [server UNCHANGED]
```
