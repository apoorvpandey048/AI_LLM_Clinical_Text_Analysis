"""
DB-backed tests for the per-case fan-out orchestration (app.workers.tasks).

Validates the highest-risk logic against a real (SQLite) database:
  - resume re-enqueue selection (skip COMPLETED; include QUEUED/FAILED/stale-PROCESSING)
  - idempotent skip in process_one_case
  - finalize_job terminal-status determination + idempotency
  - authoritative count recomputation (no double-count on resume)

tasks.py uses a module-level SessionLocal; we rebind it to a SQLite sessionmaker and stub the
Celery enqueues + the heavy per-case impl, so the orchestration logic is exercised without
Postgres/Redis/vLLM. (Full-stack integration is still covered by TESTING_PLAN §2-3.)
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.db.models import Job, JobCase, JobStatus, CaseStatus
from app.workers import tasks
import app.utils.stream_manager as stream_manager


class _DummyPublisher:
    def publish_job_complete(self, *a, **k): pass
    def publish_job_failed(self, *a, **k): pass
    def close(self): pass


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # tasks.py does `from app.db.session import SessionLocal` -> patch the name in tasks ns.
    monkeypatch.setattr(tasks, "SessionLocal", TestSession)
    # Avoid prompt-template DB deps in snapshot building.
    monkeypatch.setattr(tasks, "_build_pipeline_snapshot", lambda: [])
    monkeypatch.setattr(tasks, "_get_active_model", lambda: "test-model")
    # finalize_job publishes via StreamPublisher -> stub out Redis.
    monkeypatch.setattr(stream_manager, "StreamPublisher", _DummyPublisher)
    return TestSession


def _make_job(TestSession, statuses):
    db = TestSession()
    job = Job(status=JobStatus.QUEUED, case_count=len(statuses),
              completed_count=0, failed_count=0)
    db.add(job)
    db.flush()
    jid = str(job.id)
    ids = []
    for i, st in enumerate(statuses, start=1):
        c = JobCase(job_id=job.id, case_number=i, case_label=f"C{i}",
                    status=st, input_text=f"text {i}")
        db.add(c)
        db.flush()
        ids.append(str(c.id))
    db.commit()
    db.close()
    return jid, ids


def _job(TestSession, jid):
    db = TestSession()
    try:
        return db.query(Job).filter(Job.id == uuid.UUID(jid)).first()
    finally:
        db.close()


# --- resume / enqueue selection -------------------------------------------------------


def test_start_job_enqueues_only_unfinished(db_factory, monkeypatch):
    enq, fin = [], []
    monkeypatch.setattr(tasks.process_one_case, "apply_async", lambda *a, **k: enq.append(k["args"]))
    monkeypatch.setattr(tasks.finalize_job, "apply_async", lambda *a, **k: fin.append(k["args"]))

    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.QUEUED, CaseStatus.FAILED, CaseStatus.PROCESSING])
    tasks._start_job_impl(jid)

    enqueued_case_numbers = sorted(args[2] for args in enq)
    assert enqueued_case_numbers == [2, 3, 4]  # COMPLETED (case 1) is NOT re-enqueued
    assert fin == []  # work remains, so no immediate finalize


def test_start_job_finalizes_when_nothing_to_do(db_factory, monkeypatch):
    enq, fin = [], []
    monkeypatch.setattr(tasks.process_one_case, "apply_async", lambda *a, **k: enq.append(k["args"]))
    monkeypatch.setattr(tasks.finalize_job, "apply_async", lambda *a, **k: fin.append(k["args"]))

    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.COMPLETED])
    tasks._start_job_impl(jid)

    assert enq == []
    assert fin == [[jid]]


# --- count recomputation --------------------------------------------------------------


def test_recompute_job_counts_is_authoritative(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.FAILED, CaseStatus.QUEUED])
    tasks._recompute_job_counts(jid)
    job = _job(db_factory, jid)
    assert job.completed_count == 1
    assert job.failed_count == 1


# --- finalize status determination ----------------------------------------------------


def test_finalize_still_processing_when_incomplete(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.QUEUED])
    res = tasks._finalize_job_impl(jid)
    assert res["status"] == "still_processing"
    assert _job(db_factory, jid).status == JobStatus.QUEUED  # unchanged (was QUEUED)


def test_finalize_completed_when_all_done(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.COMPLETED])
    tasks._finalize_job_impl(jid)
    job = _job(db_factory, jid)
    assert job.status == JobStatus.COMPLETED
    assert job.completed_count == 2


def test_finalize_partial_is_completed(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.FAILED])
    tasks._finalize_job_impl(jid)
    assert _job(db_factory, jid).status == JobStatus.COMPLETED  # partial success still COMPLETED


def test_finalize_failed_when_all_failed(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.FAILED, CaseStatus.FAILED])
    tasks._finalize_job_impl(jid)
    assert _job(db_factory, jid).status == JobStatus.FAILED


def test_finalize_cancelled_when_stopping(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.FAILED])
    db = db_factory()
    db.query(Job).filter(Job.id == uuid.UUID(jid)).first().status = JobStatus.STOPPING
    db.commit(); db.close()
    tasks._finalize_job_impl(jid)
    assert _job(db_factory, jid).status == JobStatus.CANCELLED


def test_finalize_is_idempotent(db_factory):
    jid, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.COMPLETED])
    r1 = tasks._finalize_job_impl(jid)
    r2 = tasks._finalize_job_impl(jid)
    assert r1["status"] == r2["status"] == "completed"
    assert _job(db_factory, jid).completed_count == 2


# --- _maybe_finalize ------------------------------------------------------------------


def test_maybe_finalize_only_when_no_incomplete(db_factory, monkeypatch):
    fin = []
    monkeypatch.setattr(tasks.finalize_job, "apply_async", lambda *a, **k: fin.append(k["args"]))

    jid_incomplete, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.QUEUED])
    tasks._maybe_finalize(jid_incomplete)
    assert fin == []  # still has a QUEUED case

    jid_done, _ = _make_job(db_factory, [CaseStatus.COMPLETED, CaseStatus.FAILED])
    tasks._maybe_finalize(jid_done)
    assert fin == [[jid_done]]


# --- process_one_case idempotency + happy path (Celery eager) -------------------------


def test_process_one_case_skips_completed(db_factory, monkeypatch):
    called = {"impl": False}

    def fake_impl(**kw):
        called["impl"] = True
        return {"success": True}

    monkeypatch.setattr(tasks, "_process_case_impl", fake_impl)
    monkeypatch.setattr(tasks.finalize_job, "apply_async", lambda *a, **k: None)
    monkeypatch.setattr(tasks.celery_app.conf, "task_always_eager", True)

    jid, ids = _make_job(db_factory, [CaseStatus.COMPLETED])
    tasks.process_one_case.apply(args=[jid, ids[0], 1])

    assert called["impl"] is False  # COMPLETED -> skipped, impl never invoked


def test_process_one_case_happy_path(db_factory, monkeypatch):
    def fake_impl(**kw):
        # mimic _process_case_impl/save_case_result marking the case COMPLETED
        db = db_factory()
        c = db.query(JobCase).filter(
            JobCase.job_id == uuid.UUID(kw["job_id"]),
            JobCase.case_number == kw["case_number"],
        ).first()
        c.status = CaseStatus.COMPLETED
        db.commit(); db.close()
        return {"success": True}

    fin = []
    monkeypatch.setattr(tasks, "_process_case_impl", fake_impl)
    monkeypatch.setattr(tasks.finalize_job, "apply_async", lambda *a, **k: fin.append(k["args"]))
    monkeypatch.setattr(tasks.celery_app.conf, "task_always_eager", True)

    jid, ids = _make_job(db_factory, [CaseStatus.QUEUED])
    tasks.process_one_case.apply(args=[jid, ids[0], 1])

    job = _job(db_factory, jid)
    assert job.completed_count == 1
    assert fin == [[jid]]  # last case -> finalize enqueued
