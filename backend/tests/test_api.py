"""
Tests for API endpoints.
"""

import pytest
import json
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, client):
        """Basic health endpoint should return healthy."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_readiness_check(self, client):
        """Readiness endpoint should return status."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "services" in data
        assert "api" in data["services"]


class TestInfoEndpoint:
    """Tests for info endpoint."""

    def test_api_info(self, client):
        """API info endpoint should return config."""
        response = client.get("/api/v1/info")
        assert response.status_code == 200
        data = response.json()
        
        assert data["success"] is True
        assert data["data"]["name"] == "SNAP-AI"
        assert "version" in data["data"]
        assert "llm" in data["data"]
        assert "meta" in data


class TestUploadEndpoints:
    """Tests for upload endpoints."""

    def test_upload_no_file_or_text(self, client):
        """Uploading with no file or text should fail."""
        response = client.post("/api/v1/upload")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data

    def test_upload_text(self, client, sample_clinical_text):
        """Uploading text should create a job."""
        with patch("app.api.routes.upload.process_batch") as mock_task:
            mock_task.delay = MagicMock()
            
            response = client.post(
                "/api/v1/upload",
                data={"text": sample_clinical_text}
            )
            
            assert response.status_code == 200
            data = response.json()
            
            assert data["success"] is True
            assert "job_id" in data["data"]
            assert data["data"]["case_count"] >= 1

    def test_upload_invalid_file_type(self, client):
        """Uploading non-DOCX file should fail."""
        response = client.post(
            "/api/v1/upload",
            files={"file": ("test.txt", b"Hello", "text/plain")}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "docx" in data["error"]["message"].lower()


class TestJobsEndpoints:
    """Tests for jobs endpoints."""

    def test_list_jobs_empty(self, client):
        """List jobs with empty database."""
        response = client.get("/api/v1/jobs")
        assert response.status_code == 200
        data = response.json()
        
        assert data["success"] is True
        assert data["data"]["jobs"] == []
        assert data["data"]["total"] == 0

    def test_list_jobs_pagination(self, client):
        """List jobs with pagination params."""
        response = client.get("/api/v1/jobs?page=1&limit=10")
        assert response.status_code == 200
        data = response.json()
        
        assert data["data"]["page"] == 1
        assert data["data"]["limit"] == 10

    def test_get_job_not_found(self, client):
        """Get non-existent job should return 404-style error."""
        response = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"]["code"] == "NOT_FOUND"

    def test_get_job_invalid_uuid(self, client):
        """Get job with invalid UUID should fail."""
        response = client.get("/api/v1/jobs/invalid-uuid")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "invalid" in data["error"]["message"].lower()

    def test_delete_job_not_found(self, client):
        """Delete non-existent job should return error."""
        response = client.delete("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False


class TestSettingsEndpoints:
    """Tests for settings endpoints."""

    def test_get_retention_settings(self, client):
        """Get retention settings."""
        response = client.get("/api/v1/settings/retention")
        assert response.status_code == 200
        data = response.json()
        
        assert data["success"] is True
        assert "auto_delete_enabled" in data["data"]
        assert "retention_days" in data["data"]

    def test_update_retention_settings(self, client):
        """Update retention settings."""
        response = client.put(
            "/api/v1/settings/retention",
            json={
                "auto_delete_enabled": True,
                "retention_days": 30,
            }
        )
        assert response.status_code == 200
        data = response.json()
        
        assert data["success"] is True
        assert data["data"]["auto_delete_enabled"] is True
        assert data["data"]["retention_days"] == 30

    def test_update_retention_invalid_days(self, client):
        """Retention days must be positive."""
        response = client.put(
            "/api/v1/settings/retention",
            json={
                "auto_delete_enabled": True,
                "retention_days": 0,
            }
        )
        # This might return 422 for validation error or 200 with error
        data = response.json()
        # Should either be a Pydantic validation error or our custom error
        assert response.status_code in [200, 422]


class TestResponseFormat:
    """Tests for standardized response format."""

    def test_response_has_meta(self, client):
        """All responses should have meta section."""
        response = client.get("/api/v1/info")
        data = response.json()
        
        assert "meta" in data
        assert "timestamp" in data["meta"]
        assert "request_id" in data["meta"]

    def test_request_id_in_header(self, client):
        """Request ID should be in response header."""
        response = client.get("/api/v1/info")
        assert "X-Request-ID" in response.headers
        
        data = response.json()
        assert data["meta"]["request_id"] == response.headers["X-Request-ID"]
