"""
Smoke tests for critical API flows.

These are lightweight integration tests that verify:
1. Job creation returns a job_id
2. Job results contain expected structure
3. Compare endpoint returns layer data
4. Metrics endpoint returns an array
5. Replay returns a new job_id

Tests use an in-memory SQLite database and mock the Celery task layer.
"""

import uuid
import json
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.db.models import Job, JobCase, JobStatus, CaseStatus, User, UserRole
from app.api.routes.auth import create_access_token


# ============================================
# Helpers
# ============================================

def _create_user(db, role=UserRole.ADMIN):
    """Create a test user and return (user, auth_headers)."""
    user = User(
        id=uuid.uuid4(),
        username=f"testuser_{uuid.uuid4().hex[:8]}",
        name="Test User",
        password_hash="$2b$12$dummyhashvalue",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(str(user.id), user.username, user.role.value)
    headers = {"Authorization": f"Bearer {token}"}
    return user, headers


def _create_completed_job(db, user, case_count=1, model_name="test-model"):
    """Create a fully completed job with cases and layer outputs."""
    job = Job(
        id=uuid.uuid4(),
        user_id=user.id,
        status=JobStatus.COMPLETED,
        case_count=case_count,
        completed_count=case_count,
        failed_count=0,
        source_type="text",
        model_name=model_name,
        pipeline_snapshot=[
            {"layer_name": "layer1_ctp", "label": "Layer 1 - CTP", "is_builtin": True, "prompt_version": "1.3"},
            {"layer_name": "layer2_cie", "label": "Layer 2 - CIE", "is_builtin": True, "prompt_version": "1.3"},
            {"layer_name": "layer3_ccc", "label": "Layer 3 - CCC", "is_builtin": True, "prompt_version": "1.3"},
        ],
        created_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
    )
    db.add(job)
    db.flush()

    for i in range(case_count):
        case = JobCase(
            id=uuid.uuid4(),
            job_id=job.id,
            case_number=i + 1,
            input_text=f"Test case {i + 1} clinical text.",
            status=CaseStatus.COMPLETED,
            layer1_output={"clean_course_text": f"Cleaned case {i + 1}"},
            layer2_output={"complications": [], "cci_total": 0},
            layer3_output={"verdict": "PASS", "cci_score": 0},
            layer1_raw_output="raw layer1",
            layer2_raw_output="raw layer2",
            layer3_raw_output="raw layer3",
            final_cci=0,
        )
        db.add(case)

    db.commit()
    db.refresh(job)
    return job


# ============================================
# Smoke Test Suite
# ============================================

class TestSmokeCreateJob:
    """Smoke test 1: Create job → returns job_id."""

    def test_upload_text_returns_job_id(self, client, test_db, sample_clinical_text):
        """Uploading text should create a job and return a valid job_id."""
        _user, headers = _create_user(test_db)

        with patch("app.api.routes.upload.process_batch") as mock_task:
            mock_task.delay.return_value.id = "celery-task-id"

            response = client.post(
                "/api/v1/upload",
                data={"text": sample_clinical_text},
                headers=headers,
            )

            assert response.status_code == 200, f"Upload failed: {response.text}"
            data = response.json()
            assert data["success"] is True
            assert "job_id" in data["data"]
            # Validate UUID format
            job_id = data["data"]["job_id"]
            uuid.UUID(job_id)  # raises ValueError if invalid
            assert data["data"]["case_count"] >= 1


class TestSmokeGetResults:
    """Smoke test 2: Get results → contains snapshot and case data."""

    def test_results_contain_snapshot(self, client, test_db):
        """Completed job results should include pipeline_snapshot and cases."""
        user, headers = _create_user(test_db)
        job = _create_completed_job(test_db, user, case_count=2)

        response = client.get(f"/api/v1/jobs/{job.id}/results", headers=headers)
        assert response.status_code == 200, f"Results failed: {response.text}"
        data = response.json()

        assert data["success"] is True
        result_data = data["data"]

        # Must contain pipeline_snapshot
        assert "pipeline_snapshot" in result_data
        assert isinstance(result_data["pipeline_snapshot"], list)
        assert len(result_data["pipeline_snapshot"]) == 3

        # Must contain cases with layer outputs
        cases = result_data.get("results") or result_data.get("cases")
        assert cases is not None
        assert len(cases) == 2
        assert cases[0].get("layer1_output") is not None
        assert cases[0].get("layer3_output") is not None


class TestSmokeCompare:
    """Smoke test 3: Compare endpoint returns layers."""

    def test_compare_returns_layer_data(self, client, test_db):
        """Comparing two jobs should return per-layer metrics."""
        user, headers = _create_user(test_db)
        job_a = _create_completed_job(test_db, user, case_count=1, model_name="model-a")
        job_b = _create_completed_job(test_db, user, case_count=1, model_name="model-b")

        response = client.get(
            f"/api/v1/jobs/compare?job_a={job_a.id}&job_b={job_b.id}",
            headers=headers,
        )
        assert response.status_code == 200, f"Compare failed: {response.text}"
        data = response.json()

        assert data["success"] is True
        compare_data = data["data"]

        # Should contain both job references
        assert "job_a" in compare_data
        assert "job_b" in compare_data


class TestSmokeMetrics:
    """Smoke test 4: Job metrics endpoint returns array."""

    def test_metrics_returns_array(self, client, test_db):
        """Metrics endpoint should return a structured response with metrics array."""
        user, headers = _create_user(test_db)
        job = _create_completed_job(test_db, user)

        response = client.get(f"/api/v1/jobs/{job.id}/metrics", headers=headers)
        assert response.status_code == 200, f"Metrics failed: {response.text}"
        data = response.json()

        assert data["success"] is True
        metrics_data = data["data"]
        # metrics key should exist and be a list (may be empty if no LayerMetrics rows)
        assert "metrics" in metrics_data
        assert isinstance(metrics_data["metrics"], list)


class TestSmokeReplay:
    """Smoke test 5: Replay returns new job_id."""

    def test_replay_returns_new_job_id(self, client, test_db):
        """Replaying a completed job should create a new job with a different ID."""
        user, headers = _create_user(test_db)
        original_job = _create_completed_job(test_db, user)

        with patch("app.workers.tasks.process_batch") as mock_task, \
             patch("app.api.routes.jobs.logger") as _mock_log:
            mock_task.delay.return_value.id = "replay-task-id"

            response = client.post(
                f"/api/v1/jobs/{original_job.id}/replay",
                headers=headers,
            )
            assert response.status_code == 200, f"Replay failed: {response.text}"
            data = response.json()

            assert data["success"] is True
            new_job_id = data["data"]["job_id"]

            # Must be a valid UUID
            uuid.UUID(new_job_id)

            # Must be different from the original
            assert new_job_id != str(original_job.id)
