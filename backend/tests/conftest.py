"""
Pytest fixtures and configuration.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Generator

# Set test environment before importing app modules
os.environ["ENVIRONMENT"] = "development"
os.environ["POSTGRES_HOST"] = "localhost"
os.environ["POSTGRES_PORT"] = "5432"
os.environ["POSTGRES_USER"] = "test"
os.environ["POSTGRES_PASSWORD"] = "test"
os.environ["POSTGRES_DB"] = "test"
os.environ["REDIS_HOST"] = "localhost"
os.environ["REDIS_PORT"] = "6379"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db import Base, get_db
from app.db import session as db_session_module
from app.llm import LLMClient, LLMResponse


# ============================================
# Database Fixtures
# ============================================

@pytest.fixture(scope="function")
def test_db():
    """Create an in-memory SQLite database for testing."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    
    # Create tables
    Base.metadata.create_all(bind=test_engine)

    # Patch the module-level engine so lifespan uses SQLite too
    original_engine = db_session_module.engine
    db_session_module.engine = test_engine
    
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()
    
    app.dependency_overrides[get_db] = override_get_db
    
    yield TestingSessionLocal()
    
    # Cleanup
    app.dependency_overrides.clear()
    db_session_module.engine = original_engine


def _seed_admin(db):
    """Insert an active admin user into the test DB and return it.

    The API enforces auth at two layers: the global ``auth_middleware`` (validates the
    JWT) and the ``require_auth``/``require_admin`` dependencies (load the user from the
    DB). So an authenticated test client needs BOTH a valid token AND a matching user row.
    """
    from app.db.models import User, UserRole

    admin = db.query(User).filter(User.username == "test-admin").first()
    if admin is None:
        admin = User(
            username="test-admin",
            name="Test Admin",
            password_hash="not-used-by-require_auth",  # require_auth never verifies password
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
    return admin


@pytest.fixture(scope="function")
def client(test_db) -> Generator:
    """Authenticated test client (admin) with the SQLite DB override.

    Seeds an admin user and attaches a real ``Authorization: Bearer`` header signed with
    the same JWT secret the middleware verifies. Public endpoints (health/info) ignore the
    extra header, so this is safe for every existing test.
    """
    from app.api.routes.auth import create_access_token

    admin = _seed_admin(test_db)
    token = create_access_token(str(admin.id), admin.username, admin.role.value)

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


@pytest.fixture(scope="function")
def anon_client(test_db) -> Generator:
    """Unauthenticated test client — for asserting auth is actually enforced (401)."""
    with TestClient(app) as c:
        yield c


# ============================================
# LLM Mock Fixtures
# ============================================

@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    mock = MagicMock(spec=LLMClient)
    mock.generate = AsyncMock()
    mock.health_check = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mock_llm_response():
    """Factory for creating mock LLM responses."""
    def _create_response(
        content: str = "",
        parsed_json: dict = None,
        success: bool = True,
        error: str = None,
        tokens_input: int = 100,
        tokens_output: int = 50,
        duration_ms: int = 1000,
    ) -> LLMResponse:
        return LLMResponse(
            content=content,
            parsed_json=parsed_json,
            success=success,
            error=error,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            duration_ms=duration_ms,
        )
    return _create_response


# ============================================
# Sample Data Fixtures
# ============================================

@pytest.fixture
def sample_clinical_text():
    """Sample German clinical text for testing."""
    return """
    Fall #1
    
    Postoperativer Verlauf:
    Der postoperative Verlauf gestaltete sich insgesamt komplikationsarm.
    Am 2. postoperativen Tag wurde eine milde Wundinfektion festgestellt,
    die mit lokaler Wundpflege behandelt wurde.
    
    Der Patient erhielt prophylaktische Antibiotikatherapie.
    Elektrolyte wurden substituiert.
    
    Entlassung am 7. postoperativen Tag.
    """


@pytest.fixture
def sample_layer1_output():
    """Sample Layer 1 (CTP) output."""
    return {
        "clean_course_text": "Postoperativer Verlauf: Milde Wundinfektion am POD 2, lokale Wundpflege. Elektrolytsubstitution.",
        "extracted_dates": {
            "operation_date": None,
            "discharge_date": None,
        },
        "drug_normalization": {},
        "notes": "",
    }


@pytest.fixture
def sample_layer2_output():
    """Sample Layer 2 (CIE) output with complications."""
    return {
        "complications": [
            {
                "complication": "Wundinfektion",
                "cd_grade": "I",
                "evidence_quote": "milde Wundinfektion, lokale Wundpflege",
            },
        ],
        "cci_grade_list": ["I"],
        "cci_weights": [300],
        "cci_R": 300,
        "cci_sqrt_R": 17.32,
        "cci_total": 8.7,
        "cci_check_passed": True,
    }


@pytest.fixture
def sample_layer3_output():
    """Sample Layer 3 (CCC) verification output."""
    return {
        "verdict": "PASS",
        "rule_violation": False,
        "rule_violation_notes": "",
        "episode_checks": [
            {
                "episode_id": "1",
                "complication": "Wundinfektion",
                "cd_grade_as_given": "I",
                "evidence_quote": "milde Wundinfektion, lokale Wundpflege",
                "evidence_sufficient": True,
                "grade_consistent": True,
                "issues": "",
            },
        ],
        "omission_probes": {
            "probe_A": "not_applicable",
            "probe_B": "not_applicable",
            "probe_C": "not_applicable",
            "probe_D": "not_applicable",
            "probe_E": "confirmed",
            "probe_F": "not_applicable",
        },
        "likely_omissions": [],
        "cci_check": {
            "reported_cci": 8.7,
            "expected_cci": 8.7,
            "cci_mismatch": False,
        },
        "audited_result": {
            "final_episode_set": ["Wundinfektion"],
            "audited_cci": {
                "grade_list": ["I"],
                "weights": [300],
                "R": 300,
                "sqrt_R": 17.32,
                "cci_total": 8.7,
            },
        },
        "final_notes": "",
    }


# ============================================
# CCI Calculation Fixtures
# ============================================

@pytest.fixture
def cci_weights():
    """Fixed CCI weights per Clavien-Dindo grade."""
    return {
        "I": 300,
        "II": 1750,
        "IIIa": 2750,
        "IIIb": 4550,
        "IVa": 7200,
        "IVb": 8550,
        "V": float("inf"),  # Death = CCI 100
    }
