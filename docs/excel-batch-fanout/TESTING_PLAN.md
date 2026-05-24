# TESTING_PLAN.md

**Owner:** Agent E (QA & Validation). Defines how we prove the 156-case Excel batch is correct,
resumable, and GPU-safe — and that nothing in the kept components regressed.

Test tiers, fastest→slowest: **unit** (no services) → **integration** (Postgres+Redis+stub LLM)
→ **long-run** (real vLLM on host GPU). Existing suite lives in `backend/tests/`
(`test_api.py`, `test_cci.py`, `test_pipeline.py`, `test_smoke.py`, `conftest.py`) and is
SQLite-portable with a `StubLLMClient` — we extend it, not replace it.

---

## 1. Unit tests (no external services) — Phase 2 & 3

### 1.1 Excel ingestion (`backend/tests/test_ingestion_excel.py`) — Agent B
Inputs: a tiny synthetic `.xlsx` fixture **and** a 3–5 row slice of the real
`Stichprobe 1 - 150 Fälle.xlsx` (golden sample, committed as a fixture or referenced by path).

- `parse_workbook` returns N `CaseRecord`s = N data rows; `case_label == ID`.
- **Labeled-section concatenation** (NOT regex): output contains `Diagnosen:\n...`, `Operationen:\n...`
  in column order; empty/whitelisted-empty columns are skipped; the 7 always-empty columns never appear.
- Preprocessing: `_x000D_` stripped; meaningful embedded newlines preserved; bullets/tabs normalized.
- IDs validated against `^OLC-CHI-\d{9}$`; duplicates and missing IDs reported, not silently merged.
- Validation report: correct row count, flags rows missing all core text.
- Robustness: corrupt/non-xlsx bytes → clean error (no traceback leak).
- **Golden assertion:** for one known real row, the full `input_text` matches a stored expected string
  (guards against silent column-map drift — R-N5).

### 1.2 Fan-out task logic (`backend/tests/test_fanout.py`) — Agent C
With stub LLM + SQLite:
- `process_one_case` on a COMPLETED case is a **no-op** (idempotent skip) — counts unchanged.
- On a QUEUED case: sets PROCESSING → COMPLETED, writes result to DB, **returns no fat payload**
  (assert return is `None`/minimal — `ignore_result` contract).
- Forced exception → retried (assert retry invoked) → after max_retries → case FAILED, others untouched.
- `finalize_job` recomputes COMPLETED/FAILED counts **from DB rows**, sets job COMPLETED (or partial),
  and is **idempotent** when called twice.
- Cancellation: job STOPPING → `process_one_case` early-returns without processing.

---

## 2. Integration tests (Postgres + Redis + stub/Ollama) — Phase 3 & 5

Run under the canonical stack with `LLM_BACKEND` pointed at stub/Ollama (no GPU needed).

| ID | Scenario | Pass criteria |
|---|---|---|
| IT-1 | Upload `.xlsx` (golden 5-row) → job + 5 JobCases created | `case_count==5`, statuses QUEUED, `case_label==ID` |
| IT-2 | `start_job` fans out → all cases COMPLETED | 5 `process_one_case` enqueued on `cases` queue; `finalize_job` sets COMPLETED |
| IT-3 | **Resume after partial** — mark 2 cases COMPLETED, kill worker mid-run, `POST /resume` | only the 3 incomplete re-enqueue; final = 5 COMPLETED; no double counts |
| IT-4 | **Crash mid-case** — SIGKILL worker during a case | `acks_late` redelivers ONLY that case; batch completes; job not stuck |
| IT-5 | Queue separation — submit a batch, then an interactive single-case job | interactive job completes without waiting for the batch to drain |
| IT-6 | Redis hygiene — run a batch, inspect result backend | no per-case result keys; `INFO memory` flat vs baseline (R-4) |
| IT-7 | SSE longevity — connect EventSource, advance a simulated clock past old 3600 s | stream not force-closed; reconnect re-subscribes; `/status` reconciles (R-5) |
| IT-8 | Backward compat — upload a `.txt` multi-case blob | still parsed by `parse_cases_from_text`; processed via fan-out shim |
| IT-9 | Replay/reprocess path still works (`process_batch` shim → `start_job`) | job reprocesses from stored snapshot |

---

## 3. Failure & recovery simulations — Phase 5

Deliberate fault injection; each must leave the system in a **recoverable, consistent** state.

1. **Worker SIGKILL** at random case → restart → `/resume` → batch finishes; counts correct.
2. **Redis restart** mid-batch → broker reconnects; in-flight case redelivered; no livelock.
3. **vLLM down** for a window → affected cases retry/backoff; on recovery, `/resume` finishes them;
   no case permanently lost.
4. **Duplicate delivery** (force redelivery of an in-flight case) → idempotency guard prevents
   double-processing / double counts (R-N3).
5. **Cancellation** mid-batch (job → STOPPING) → in-flight case finishes or aborts cleanly;
   remaining cases not started; job → CANCELLED.
6. **Coordinator/chord misfire** simulated (callback dropped) → job recoverable via `/resume`;
   `finalize_job` idempotent (R-N2).

---

## 4. Long-run validation (real vLLM, host GPU) — Phase 5

**Pre-req:** canonical runtime up, host vLLM serving GPT-OSS-120B. **One batch at a time.**

- **Throughput ladder:** measure mean/median s/case and total wall-clock at `concurrency ∈ {1, 2, 3}`
  on a 10–20 case subset. Record vLLM latency + GPU memory (`nvidia-smi`) at each step. Pick the
  highest concurrency with **no latency collapse and the host still responsive** (R-7). Default to 2
  if data is inconclusive.
- **Full 156-case run** at the chosen concurrency: must **complete** (not livelock), persist all
  results to Postgres, and keep the UI live (stream or poll) throughout (R-1, R-5).
- **Interactive responsiveness:** during the full run, submit a single interactive case → it returns
  in roughly normal single-case time (R-6).
- **Redis memory** sampled across the full run stays bounded (no result-backend growth) (R-4).
- **DB verification:** 156 JobCases COMPLETED (or explicitly FAILED with `error_message`); per-case
  `final_verdict`/`final_cci` populated; `layer_metrics` rows present; `final_cci` column type
  confirmed (R-10).

---

## 5. Regression guard (kept components must not change behavior)

- Existing `backend/tests/` suite passes unchanged (orchestrator, CCI, API, smoke).
- **Output parity:** run a small known set through the OLD `process_batch` path (shim) and the NEW
  fan-out path with a fixed seed; per-case `final_verdict`/`final_cci` must match (orchestrator is
  unchanged — this proves the wrapper didn't alter results).
- Snapshot/replay determinism preserved: a replayed job reproduces the original verdicts.
- docx/pdf/txt upload paths unchanged (IT-8).

---

## 6. Definition of Done (acceptance)

- [ ] `.xlsx` upload → 156 cases parsed (labeled-section, validated) → job created.
- [ ] Full 156-case batch **completes** on the single GPU without livelock.
- [ ] Kill worker mid-batch → `/resume` finishes only the unfinished cases; no double counts.
- [ ] No fat results in Redis; Postgres is sole source of truth; Redis memory bounded.
- [ ] Live progress survives the whole multi-hour run (stream or durable polling).
- [ ] Interactive single-case jobs remain usable during a batch.
- [ ] Existing test suite green; old vs new output parity on the seeded sample.
- [ ] All five deliverable docs current; deprecated runtime paths labeled (not deleted).
