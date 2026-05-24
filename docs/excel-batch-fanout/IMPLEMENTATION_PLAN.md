# IMPLEMENTATION_PLAN.md — Excel Batch Fan-Out

**Branch:** `feature/excel-batch-fanout` (from `revert-prompts-baseline` @ `a975a81`)
**Repo:** `/mnt/c/Users/Apoor/n/snap-ai` (the inner repo; the outer `/mnt/c/Users/Apoor/n/.git` is empty)
**Source of truth:** `SNAP_AI_ENGINEERING_CONTEXT.md` (verified §-by-§ against live code on 2026-05-24)
**Companions:** [CANONICAL_RUNTIME](CANONICAL_RUNTIME.md) · [ARCHITECTURE_DIFF](ARCHITECTURE_DIFF.md) · [RISK_REGISTER](RISK_REGISTER.md) · [TESTING_PLAN](TESTING_PLAN.md)

**Objective:** Upload `.xlsx` → parse 156 structured clinical cases → fan out per-case Celery
tasks → process safely on the local host-GPU vLLM → stream live progress → persist continuously →
resume after crashes → "press button and forget" — **without rewriting the orchestrator or core
pipeline.** Minimum architectural change required.

---

## 1. Branch structure & ownership

```
revert-prompts-baseline (a975a81)         ← integration line; NOT modified
   └── feature/excel-batch-fanout         ← ALL work here
         (main and feature/* branches are NOT touched)
```

**Agents = ownership boundaries** (realized as sequential, single-threaded execution in phase
order — chosen because Agents A & C both edit `tasks.py`/`celery_app.py` and the phases are
inherently ordered; parallel edits to shared files would conflict).

| Agent | Owns (files) | Must NOT touch |
|---|---|---|
| **A — Runtime/Infra** | `docker-compose.local-gpu.yml`, `.env(.example)`, `celery_app.py` (queues/concurrency/results), `start-vllm.sh` (port), SSE timeout in `stream.py`/`stream_manager.py`, CANONICAL_RUNTIME.md | pipeline internals, ingestion logic, FE |
| **B — Ingestion** | `backend/app/ingestion/excel.py` (new), `requirements.txt` (openpyxl), `.xlsx` branch in `upload.py`, ingestion tests | task orchestration, FE, celery config |
| **C — Fan-out** | `tasks.py` (new `start_job`/`process_one_case`/`finalize_job` + `process_batch` shim), `jobs.py` (`/resume`), optional `retry_count` migrator in `main.py`, fan-out tests | orchestrator/layer internals, ingestion parsing, FE |
| **D — Frontend** | `frontend/src/main.js`, `styles.css`, `index.html` (xlsx upload, batch dashboard, SSE reconnect, export) | backend, celery, framework swap |
| **E — QA** | `backend/tests/*`, simulation scripts, long-run + throughput measurements, doc upkeep | feature code (reviews, doesn't author) |

**Shared-file protocol:** only one agent edits a given file within a phase. `tasks.py` is edited in
Phase 3 only (Agent C); Agent A's celery changes are in `celery_app.py`, disjoint from C's task code.

---

## 2. Implementation order (mandatory phases)

### Phase 1 — Establish (THIS deliverable) ✅
Canonical runtime decision, branch creation, ownership map, `.gitattributes`, and the five docs.
**Gate:** plan delivered; per the approved workflow, proceed automatically through Phases 2–5,
checking in only on blockers.

### Phase 2 — Excel ingestion (Agent B)
1. Add `openpyxl==3.1.5` to `backend/requirements.txt`.
2. New `backend/app/ingestion/__init__.py` + `excel.py`: `parse_workbook`, column→label constant,
   preprocessing (`_x000D_` strip, drop the 7 empty columns, normalize bullets/tabs, keep newlines),
   `validate`. **Labeled-section concatenation — no regex header splitting.**
3. Wire `.xlsx` into `upload.py`: extend `ALLOWED_EXTENSIONS`; route `.xlsx` to the parser;
   build one `Job` + N `JobCase` (`case_label = ID`, `input_text` = concat); return the validation
   report. Leave `parse_cases_from_text` (docx/pdf/txt) untouched.
4. Unit tests (§TESTING_PLAN 1.1) incl. golden assertion vs the real `Stichprobe` file.
**Exit:** `.xlsx` upload creates a job with 156 correctly-labeled cases; tests green.

### Phase 3 — Fan-out + Redis/Celery + resume (Agents C, then A)
1. (C) Add `start_job`, `process_one_case` (idempotent skip, `ignore_result=True`, autoretry,
   per-case fail-safe), `finalize_job` (DB-derived counts, idempotent). Convert `process_batch`
   into a shim → `start_job`. Repoint `upload.py` enqueue to `start_job`.
2. (C) `POST /api/v1/jobs/{id}/resume` → `start_job` (re-enqueues QUEUED/FAILED only). Optional
   `retry_count` column via the startup migrator.
3. (A) `celery_app.py`: `task_routes` + `cases`/`interactive` queues, `worker_concurrency=2`;
   overlay worker cmd `-Q cases,interactive,celery`. Confirm fat results gone from Redis.
4. (A) Lift SSE `timeout=3600` → env-configurable; reconcile vLLM port (R-5.1) in
   `start-vllm.sh`/`.env`. Add deprecation banners to prod/remote compose + obsolete scripts.
5. Integration tests (§TESTING_PLAN 2): fan-out, resume, crash, queue separation, Redis hygiene.
**Exit:** batch fans out, survives kill, resumes; Redis bounded; queues separated.

### Phase 4 — Frontend batch UX (Agent D)
1. `.xlsx` accepted in the upload control + small Excel affordance / optional column preview.
2. Batch progress dashboard improvements (X-of-Y, per-case list, partial-failure display);
   **Resume** control calling `/resume`.
3. SSE transparent reconnect for multi-hour jobs (re-subscribe on timeout/drop, reconcile via
   `/status`); preserve the existing backoff + polling floor. **Vanilla JS only.**
4. Batch export affordance keyed by `OLC-CHI` ID.
**Exit:** upload→watch→resume→export works end-to-end in the browser; no framework added.

### Phase 5 — QA & validation (Agent E)
Run §TESTING_PLAN 3–5: failure/recovery simulations, throughput ladder + full 156-case run on the
host GPU, regression/parity guard, `final_cci` type check, Redis-memory + DB verification.
Finalize docs; record measured concurrency and throughput.
**Exit:** Definition-of-Done (§TESTING_PLAN 6) met.

---

## 3. Commit grouping strategy (small, one concern each)

Commit only **explicit paths** (never `git add -A`); inspect `git diff --cached --stat` before each
commit to keep CRLF churn out (R-N1). Suggested commit sequence:

```
P1  chore(repo): add .gitattributes to normalize line endings (LF)
P1  docs(batch): canonical runtime, architecture diff, risk register, testing + implementation plan
P2  feat(ingestion): add openpyxl dependency
P2  feat(ingestion): excel parser module (row->case, labeled-section concat, preprocessing, validate)
P2  test(ingestion): excel parser unit + golden tests
P2  feat(upload): accept .xlsx and route to excel ingestion (docx/pdf/txt unchanged)
P3  feat(tasks): add process_one_case (idempotent, ignore_result, retry)
P3  feat(tasks): add start_job coordinator + finalize_job; process_batch becomes shim
P3  feat(api): POST /jobs/{id}/resume (re-enqueue QUEUED/FAILED)
P3  feat(celery): cases/interactive queue routing + concurrency=2; drop fat results
P3  fix(runtime): env-configurable SSE timeout; reconcile vLLM port; deprecation banners
P3  test(fanout): idempotency, resume, crash, finalize
P4  feat(ui): .xlsx upload affordance
P4  feat(ui): batch dashboard + resume control
P4  feat(ui): SSE transparent reconnect for long jobs
P5  test(integration): failure/recovery simulations + throughput notes
P5  docs(batch): record measured concurrency/throughput; finalize
```

One concern per commit; each commit should leave the suite green (or the test commit immediately
precedes its feature where TDD fits). Conventional-commit prefixes for scannability.

---

## 4. Rollback strategy

**Granularity of safety, smallest→largest:**

1. **Per-commit:** small focused commits → `git revert <sha>` undoes one concern cleanly.
2. **Feature flag (ingestion & fan-out):**
   - `.xlsx` ingestion is purely additive — disable by removing `.xlsx` from `ALLOWED_EXTENSIONS`
     (one line); docx/pdf/txt unaffected.
   - Fan-out: `process_batch` is **kept as a working shim**. A kill-switch env
     (`BATCH_FANOUT_ENABLED`, default on) lets `process_batch` fall back to the **original
     sequential loop** if fan-out misbehaves in the field — zero redeploy of code, flip env +
     `restart worker`. (Implemented in Phase 3 alongside the shim.)
3. **Per-phase:** each phase is a contiguous commit group; `git revert` the range to drop a phase
   while keeping earlier phases.
4. **Whole feature:** the branch is isolated. Abandon = `git switch revert-prompts-baseline` (the
   integration line is untouched). Nothing was merged to `main` or the older `feature/*` branches.
5. **Runtime:** no infrastructure deleted — deprecated compose/scripts only get banner comments, so
   reverting runtime decisions is a comment removal, not a file restore.
6. **Data:** no destructive DB migrations. The only schema change (`retry_count`) is additive
   `ADD COLUMN IF NOT EXISTS`; rolling back code leaves the unused column harmless.

**Pre-merge checklist:** suite green · 156-case run completes · resume verified · Redis bounded ·
docs current · `git diff revert-prompts-baseline...HEAD --stat` shows only intended files (no CRLF).

---

## 5. Out of scope (explicitly deferred — see RISK_REGISTER C)

- Per-job (non-global) runtime config (R-11) — safe to defer while running one batch at a time.
- `/jobs/{id}/results` pagination (R-8) — not required for batch correctness.
- Alembic vs startup-migrator unification — pre-existing tech debt, not this feature.
- Multi-GPU / multi-node / Cloudflare tunnel / prod & remote compose parity.
- Object-storage offload of raw streams, retention/TTL policies (long-term refactors).

---

## 6. Assumptions (labeled)

- Single host, single A100-40GB, host vLLM (GPT-OSS-120B) reachable per CANONICAL_RUNTIME.
- ~189 s/case mean (from one 19-case `output.json`); concurrency speedup unmeasured → measured in P5.
- Independent (single-pass) mode for sizing; comparison mode doubles GPU cost.
- One batch at a time operationally.
- The real dataset matches the profiled `Stichprobe 1 - 150 Fälle.xlsx` (156 rows, `OLC-CHI` IDs,
  the column set in context §8.1).
