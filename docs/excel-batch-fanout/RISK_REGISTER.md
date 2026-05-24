# RISK_REGISTER.md

Risks for the Excel-batch-fanout feature: pre-existing defects we are fixing, plus new risks
introduced by the change. Severity: 🔴 critical · 🟠 high · 🟡 medium · 🟢 low.
"Owner" = the responsible agent (A–E). "Phase" = when it is addressed.

---

## A. Pre-existing defects this feature MUST resolve

| ID | Sev | Risk | Evidence | Mitigation | Owner/Phase |
|---|---|---|---|---|---|
| R-1 | 🔴 | Monolithic `process_batch` exceeds 61-min hard limit → SIGKILL → `acks_late` redelivery → restart from case #1 → **livelock**. | celery_app.py `task_time_limit=3660`; tasks.py:979 single loop; ~189 s/case | Per-case fan-out: each task ≪ limit; `acks_late` re-runs only the in-flight case. | C / P3 |
| R-2 | 🔴 | **No resumability** — crash re-runs from scratch; no skip-completed guard. | tasks.py:979-1059 (no status check) | Idempotent `process_one_case` (skip if COMPLETED) + `POST /jobs/{id}/resume` re-enqueues QUEUED/FAILED only. | C / P3 |
| R-3 | 🟠 | **No Excel ingestion** (deps + endpoint + parser all missing). | requirements.txt; upload.py:34 | `openpyxl`, `ingestion/excel.py`, `.xlsx` route, labeled-section concat. | B / P2 |
| R-4 | 🟠 | Fat task results stored in Redis (`result_expires=86400`, 2 GB `allkeys-lru`) → eviction can purge **broker** keys → message/result loss. | tasks.py:1144-1152 `return {... "results"}` | `ignore_result=True` on case tasks; Postgres is sole source of truth. | A / P3 |
| R-5 | 🟠 | SSE hard timeout 3600 s → live stream dies ~1 h into a multi-hour batch. | stream.py:42, stream_manager.py:172 | Env-configurable timeout (raise/disable); FE transparent reconnect; polling floor kept. | A,D / P3,P4 |
| R-6 | 🟠 | Single shared vLLM + `concurrency=1` → one big batch starves all other jobs. | celery_app.py; overlay worker cmd | Separate `cases` vs `interactive` queues; tuned concurrency. | A / P3 |
| R-7 | 🟡 | GPU oversubscription if concurrency raised naively (×3 parallel L3 calls, ×2 comparison mode) on one 40 GB A100. | orchestrator L3 `asyncio.gather`; comparison mode | Start `concurrency=2`; measure (E); optional Redis semaphore capping total vLLM requests. | A,E / P3,P5 |
| R-8 | 🟡 | `/jobs/{id}/results` & audit export load all cases unpaginated → slow/large at 156. | jobs.py | **Deferred** (not required for batch correctness); documented. Stream/paginate if it bites. | C / P3 (opt) |
| R-10 | 🟡 | `final_cci` Float (model) vs INTEGER (startup migrator) drift on live DB. | models.py:248 vs main.py migrator | Verify live column type; reconcile via migrator if drifted. | E / P5 |
| R-11 | 🟡 | Per-job runtime config read from **global** Redis keys (`snapai:temperature` etc.) → concurrent jobs override each other. | tasks.py `_get_*` | **Out of scope** (long-term). Documented; safe because batches run one-at-a-time on one GPU. Note in plan. | — / future |

---

## B. New risks introduced by THIS change

| ID | Sev | Risk | Mitigation | Owner/Phase |
|---|---|---|---|---|
| N-1 | 🟠 | **CRLF churn pollutes commits.** ~90 tracked files differ by line-ending only; `git add -A` would bury real changes in noise and risk clobbering. | `.gitattributes` (`* text=auto eol=lf`) committed first; **never `git add -A`** — only explicit paths; inspect `git diff --cached --stat` before every commit. | A / P1 ✅ |
| N-2 | 🟠 | **Chord/coordinator correctness.** A Celery chord over 156 tasks can mis-fire its callback (e.g. on retries) → job stuck PROCESSING. | `finalize_job` recomputes counts **from the DB** (idempotent), not from chord results; also reachable via `/resume`. A periodic "stuck job" sweep is a fallback. | C / P3 |
| N-3 | 🟠 | **Idempotency race.** Two workers grab the same case (redelivery + concurrency) → double-processing / double counts. | Skip-completed check + status transition guarded by a DB row update with WHERE status != COMPLETED/PROCESSING; counts derived in `finalize_job` from DB, not incremented blindly. | C / P3 |
| N-4 | 🟡 | **openpyxl memory** on a 156×23 sheet with large cells. | `read_only=True` iterator; parse once at upload, persist `input_text` per case; never hold the workbook in the worker. | B / P2 |
| N-5 | 🟡 | **Excel parsing correctness** — wrong column→label map or dropping a filled column silently loses clinical text. | Explicit column-label constant; validation report (row count, empty-core rows) shown before enqueue; golden-sample parser tests vs the real `Stichprobe` file. | B,E / P2,P5 |
| N-6 | 🟡 | **Queue routing regression** — splitting queues could leave coordinator/finalize unrouted → tasks never run. | Worker consumes ALL queues (`-Q cases,interactive,celery`); explicit `task_routes`; smoke test that each task type dequeues. | A / P3 |
| N-7 | 🟡 | **`ignore_result` breaks a consumer** that reads `AsyncResult(...).get()` (e.g. status polling, replay). | Audit callers of the task return value before flipping `ignore_result`; status/progress already come from DB, not the result backend. | C / P3 |
| N-8 | 🟡 | **Backend hot-reload vs worker no-reload** confusion → testing stale worker code. | CANONICAL_RUNTIME documents `restart worker` after task/pipeline edits; QA checklist enforces it. | A,E / P1,P5 |
| N-9 | 🟢 | **`.xlsx` MIME/extension spoofing** on upload. | Extension allowlist + openpyxl load guarded by try/except → 4xx on parse failure, not 500. | B / P2 |
| N-10 | 🟢 | New `openpyxl` dep adds image, not yet in image. | Add to requirements + rebuild image in canonical `up --build`; CI/test install. | A,B / P2 |

---

## C. Risk-acceptance notes (labeled assumptions)

- **Concurrency speedup is unmeasured.** Wall-clock at `concurrency=2-3` depends on vLLM batching
  headroom on a single 40 GB A100. We **start at 2** and let Agent E measure; we do **not** assume a speedup.
- **Comparison mode doubles GPU cost.** Batch sizing/benchmarks assume independent (single-pass) mode
  unless explicitly testing comparison mode.
- **One batch at a time** is the operating assumption on a single GPU; this is why R-11 (global runtime
  config) is safe to defer.
- **We do not delete infrastructure.** Deprecated compose files/scripts get banner comments only (R-12,
  policy): reversible, auditable.
