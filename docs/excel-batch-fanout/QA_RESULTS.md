# QA_RESULTS.md — Phase 5 (Agent E)

What was actually verified, and the runbook for the parts that need the live GPU stack.

---

## 1. Verified locally (this environment)

A throwaway venv (`/tmp/snapvenv`) was built from `backend/requirements.txt` and the suite run
with SQLite (no GPU/Postgres/Redis needed for unit + DB-backed tests).

| Check | Result |
|---|---|
| Excel parser vs the **real** `Stichprobe 1 - 150 Fälle.xlsx` | ✅ **156 cases**, 0 missing/invalid IDs, exactly the 7 always-empty columns dropped, labeled-section output clean (no `_x000D_`) |
| `test_ingestion_excel.py` (18 tests) | ✅ all pass |
| `test_fanout.py` (retry/backoff/kill-switch/wiring, 21) | ✅ all pass |
| `test_fanout_db.py` (DB-backed orchestration, 12) | ✅ all pass |
| **Full backend suite** | ✅ **95 passed**, 12 failed |
| Regression parity vs base `revert-prompts-baseline` | ✅ the **same 12 failures exist on base** (auth-harness + 1 untouched pipeline test) — **0 regressions introduced**; my branch adds 51 passing tests (44→95) |
| `app.main` import (all routers + workers) | ✅ imports cleanly — upload/jobs/stream + tasks/celery changes load without error |
| Python syntax (`ast.parse`) on every edited file | ✅ |
| `node --check frontend/src/main.js` | ✅ |
| Compose YAML parse (local-gpu/prod/remote/dev) | ✅ |

**Behaviors proven by `test_fanout_db.py` (the highest-risk items):**
- Resume re-enqueues only QUEUED/FAILED/stale-PROCESSING; **COMPLETED is skipped** (R-2).
- `process_one_case` idempotent skip — impl never invoked for a COMPLETED case (R-2/N-3).
- `finalize_job` status logic: `still_processing` while incomplete; all-done→COMPLETED;
  partial→COMPLETED; all-failed→FAILED; job STOPPING→CANCELLED; **idempotent** on repeat (N-2).
- `_recompute_job_counts` is authoritative from DB rows → no double-count on resume (N-3).

**The 12 pre-existing failures** (NOT introduced here, identical on base): `test_api.py` upload/jobs/
settings endpoints return 401 (the test client doesn't authenticate — a pre-existing harness gap),
and `test_pipeline.py::test_validate_output_missing_cci_total` (pipeline code untouched by this work).
Out of scope to fix here; flagged for a follow-up.

### Reproduce
```bash
python3 -m venv /tmp/snapvenv && /tmp/snapvenv/bin/pip install -r backend/requirements.txt
cd backend && PYTHONDONTWRITEBYTECODE=1 /tmp/snapvenv/bin/python -m pytest -p no:cacheprovider \
  tests/test_ingestion_excel.py tests/test_fanout.py tests/test_fanout_db.py -q
```

---

## 2. Runbook — requires the canonical runtime up (GPU host)

Cannot be run in this environment (no GPU / 120B vLLM). Run on the DGX host with the
canonical stack up (CANONICAL_RUNTIME.md). Commands are copy-pasteable; fill in measured numbers.

```bash
ENV="-f docker-compose.yml -f docker-compose.local-gpu.yml"
./start-vllm.sh &                         # ensure VLLM_PORT matches VLLM_HOST (=8005)
docker compose $ENV up -d --build
```

### IT — integration (TESTING_PLAN §2)
- **IT-1/IT-2 upload→fan-out**: upload the `.xlsx` via the UI (or `curl -F files=@"Stichprobe...xlsx"`
  to `/api/v1/upload` with a JWT). Expect a job with **156 QUEUED** cases, then `process_one_case`
  tasks draining the `cases` queue. Watch: `docker compose $ENV logs -f worker | grep -E "JOB_FANOUT_ENQUEUED|CASE_|JOB_FINALIZED"`.
- **IT-3 resume**: mid-run `docker compose $ENV restart worker`; then `POST /api/v1/jobs/{id}/resume`.
  Expect only non-COMPLETED cases re-enqueued; final = all COMPLETED; counts not doubled.
- **IT-5 queue isolation**: during the batch, submit a single interactive case → it completes without
  waiting for the batch (consumed from `interactive`).
- **IT-6 Redis hygiene** (R-4): `docker compose $ENV exec redis redis-cli INFO memory | grep used_memory_human`
  before/during/after — should stay flat; `redis-cli --scan --pattern 'celery-task-meta-*' | wc -l`
  should be ~0 (per-case tasks are `ignore_result`).

### Failure/recovery simulations (TESTING_PLAN §3)
- **Worker SIGKILL**: `docker compose $ENV kill -s SIGKILL worker` mid-case → `up -d worker` → `/resume`
  → batch finishes; the in-flight case is reprocessed (acks_late), not the whole batch (R-1).
- **Redis restart**: `docker compose $ENV restart redis` mid-batch → no livelock; cases resume.
- **vLLM down**: `pkill -f 'vllm serve'` for a window → affected cases retry/backoff → restart vLLM →
  `/resume` finishes them.
- **Cancel**: `POST /jobs/{id}/cancel` mid-batch → job → CANCELLED; remaining cases not started.

### Throughput ladder (TESTING_PLAN §4 / R-7) — MEASURE
Run a 10–20 case subset at `WORKER_CONCURRENCY ∈ {1,2,3}` (set env, `restart worker`), watch
`nvidia-smi` and vLLM latency. Pick the highest concurrency with no latency collapse + responsive host.

| concurrency | mean s/case | 20-case wall-clock | GPU mem peak | host responsive? |
|---|---|---|---|---|
| 1 | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| 2 (default) | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| 3 | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

Then the **full 156-case run** at the chosen concurrency: must **complete** (no livelock), persist all
results to Postgres, keep the UI live throughout. Record total wall-clock here: _TBD_.

### DB verification (psql)
```sql
SELECT status, count(*) FROM job_cases WHERE job_id='<JID>' GROUP BY status;     -- expect 156 COMPLETED
SELECT completed_count, failed_count, status FROM jobs WHERE id='<JID>';
SELECT data_type FROM information_schema.columns
  WHERE table_name='job_cases' AND column_name='final_cci';                       -- R-10: confirm type
```

---

## 3. Definition-of-Done status

| Criterion | Status |
|---|---|
| `.xlsx` → 156 cases parsed (labeled-section, validated) | ✅ proven vs real dataset |
| Full 156-case batch completes without livelock | ⏳ run on GPU host (architecture proven by tests) |
| Kill worker mid-batch → `/resume` finishes only unfinished; no double counts | ✅ logic proven (test_fanout_db) · ⏳ live sim |
| No fat results in Redis; Postgres source of truth | ✅ `ignore_result` + tiny shim returns · ⏳ live INFO memory |
| Live progress survives multi-hour run | ✅ SSE timeout lifted + FE reconnect · ⏳ live |
| Interactive jobs usable during a batch | ✅ queue split · ⏳ live |
| Existing suite green; no regressions | ✅ 95 passed; 12 pre-existing failures unchanged vs base |
| Deprecated runtime paths labeled, not deleted | ✅ |

⏳ = needs the GPU host; the runbook above is the exact procedure.
