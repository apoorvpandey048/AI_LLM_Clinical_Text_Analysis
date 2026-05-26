"""
Tests for pipeline layers.

Tests Layer 1 (CTP), Layer 2 (CIE), and Layer 3 (CCC) validation logic.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.pipeline.layer1_ctp import Layer1CTP
from app.pipeline.layer2_cie import Layer2CIE
from app.pipeline.layer3_ccc import Layer3CCC
from app.llm import LLMResponse


class TestLayer1CTP:
    """Tests for Layer 1 Clinical Text Pre-Processor."""

    def test_layer_name(self, mock_llm_client):
        """Verify layer name."""
        layer = Layer1CTP(mock_llm_client)
        assert layer.layer_name == "layer1_ctp"

    def test_validate_output_valid(self, mock_llm_client, sample_layer1_output):
        """Valid output should pass validation."""
        layer = Layer1CTP(mock_llm_client)
        is_valid, error = layer.validate_output(sample_layer1_output)
        assert is_valid is True
        assert error is None

    def test_validate_output_missing_clean_text(self, mock_llm_client):
        """Missing clean_course_text should fail."""
        layer = Layer1CTP(mock_llm_client)
        output = {
            "extracted_dates": {},
            "drug_normalization": {},
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "clean_course_text" in error

    def test_validate_output_missing_dates(self, mock_llm_client):
        """Missing extracted_dates should be auto-normalized to empty dict and pass."""
        layer = Layer1CTP(mock_llm_client)
        output = {
            "clean_course_text": "text",
            "drug_normalization": {},
        }
        is_valid, error = layer.validate_output(output)
        # normalize_output fills in extracted_dates: {}
        assert is_valid is True
        assert error is None
        assert "extracted_dates" in output

    def test_validate_output_wrong_type(self, mock_llm_client):
        """Wrong type for clean_course_text should fail."""
        layer = Layer1CTP(mock_llm_client)
        output = {
            "clean_course_text": 123,  # Should be string
            "extracted_dates": {},
            "drug_normalization": {},
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "string" in error.lower()

    def test_build_user_prompt(self, mock_llm_client):
        """User prompt should contain raw text."""
        layer = Layer1CTP(mock_llm_client)
        raw_text = "Test clinical text"
        prompt = layer.build_user_prompt(raw_text=raw_text)
        assert raw_text in prompt
        assert "JSON" in prompt


class TestLayer2CIE:
    """Tests for Layer 2 Complication Inference Engine."""

    def test_layer_name(self, mock_llm_client):
        """Verify layer name."""
        layer = Layer2CIE(mock_llm_client)
        assert layer.layer_name == "layer2_cie"

    def test_validate_output_valid(self, mock_llm_client, sample_layer2_output):
        """Valid output should pass validation."""
        layer = Layer2CIE(mock_llm_client)
        is_valid, error = layer.validate_output(sample_layer2_output)
        assert is_valid is True
        assert error is None

    def test_validate_output_missing_complications(self, mock_llm_client):
        """Missing complications field should fail."""
        layer = Layer2CIE(mock_llm_client)
        output = {
            "cci_grade_list": [],
            "cci_weights": [],
            "cci_R": 0,
            "cci_total": 0,
            "cci_check_passed": True,
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "complications" in error

    def test_validate_output_invalid_cd_grade(self, mock_llm_client):
        """Invalid CD grade should fail."""
        layer = Layer2CIE(mock_llm_client)
        output = {
            "complications": [
                {"complication": "Test", "cd_grade": "VI"}  # Invalid grade
            ],
            "cci_grade_list": ["VI"],
            "cci_weights": [0],
            "cci_R": 0,
            "cci_total": 0,
            "cci_check_passed": True,
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "cd_grade" in error.lower()

    def test_validate_output_cci_total_now_optional(self, mock_llm_client):
        """cci_total is no longer required (pipeline v2.0).

        CCI is computed deterministically in Python from the CD grades, so the LLM is no
        longer trusted to emit cci_total. An output with valid complications and no
        cci_total is therefore VALID. (Replaces the obsolete
        ``test_validate_output_missing_cci_total``, which asserted the old contract.)
        """
        layer = Layer2CIE(mock_llm_client)
        output = {
            "complications": [],
            "cci_grade_list": [],
            "cci_weights": [],
            "cci_R": 0,
            "cci_check_passed": True,
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is True
        assert error is None

    def test_validate_all_cd_grades_valid(self, mock_llm_client):
        """All valid CD grades should pass."""
        layer = Layer2CIE(mock_llm_client)
        valid_grades = ["I", "II", "IIIa", "IIIb", "IVa", "IVb", "V"]
        
        for grade in valid_grades:
            output = {
                "complications": [
                    {"complication": f"Test {grade}", "cd_grade": grade}
                ],
                "cci_grade_list": [grade],
                "cci_weights": [300],
                "cci_R": 300,
                "cci_total": 8.7,
                "cci_check_passed": True,
            }
            is_valid, error = layer.validate_output(output)
            assert is_valid is True, f"Grade {grade} should be valid"


class TestLayer3CCC:
    """Tests for Layer 3 Clinical Consistency Challenger."""

    def test_layer_name(self, mock_llm_client):
        """Verify layer name."""
        layer = Layer3CCC(mock_llm_client)
        assert layer.layer_name == "layer3_ccc"

    def test_validate_output_valid(self, mock_llm_client, sample_layer3_output):
        """Valid output should pass validation."""
        layer = Layer3CCC(mock_llm_client)
        is_valid, error = layer.validate_output(sample_layer3_output)
        assert is_valid is True
        assert error is None

    def test_validate_output_missing_verdict(self, mock_llm_client):
        """Missing verdict should fail."""
        layer = Layer3CCC(mock_llm_client)
        output = {
            "rule_violation": False,
            "episode_checks": [],
            "omission_probes": {},
            "cci_check": {
                "reported_cci": 0,
                "expected_cci": 0,
                "cci_mismatch": False,
            },
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "verdict" in error

    def test_validate_output_invalid_verdict(self, mock_llm_client):
        """Invalid verdict value should fail."""
        layer = Layer3CCC(mock_llm_client)
        output = {
            "verdict": "INVALID_VERDICT",
            "rule_violation": False,
            "episode_checks": [],
            "omission_probes": {},
            "cci_check": {
                "reported_cci": 0,
                "expected_cci": 0,
                "cci_mismatch": False,
            },
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "verdict" in error.lower()

    def test_validate_all_verdicts_valid(self, mock_llm_client):
        """All valid verdicts should pass."""
        layer = Layer3CCC(mock_llm_client)
        valid_verdicts = [
            "PASS",
            "PASS_WITH_WARNINGS",
            "FAIL_REVIEW_REQUIRED",
            "FAIL_REEXTRACTION_RECOMMENDED",
        ]
        
        for verdict in valid_verdicts:
            output = {
                "verdict": verdict,
                "rule_violation": False,
                "episode_checks": [],
                "omission_probes": {},
                "cci_check": {
                    "reported_cci": 0,
                    "expected_cci": 0,
                    "cci_mismatch": False,
                },
            }
            is_valid, error = layer.validate_output(output)
            assert is_valid is True, f"Verdict {verdict} should be valid"

    def test_validate_output_missing_cci_check(self, mock_llm_client):
        """Missing cci_check should fail."""
        layer = Layer3CCC(mock_llm_client)
        output = {
            "verdict": "PASS",
            "rule_violation": False,
            "episode_checks": [],
            "omission_probes": {},
        }
        is_valid, error = layer.validate_output(output)
        assert is_valid is False
        assert "cci_check" in error

    def test_build_user_prompt(self, mock_llm_client, sample_layer2_output):
        """User prompt should contain both clean text and Layer 2 output."""
        layer = Layer3CCC(mock_llm_client)
        clean_text = "Test clinical text"
        prompt = layer.build_user_prompt(
            clean_text=clean_text,
            layer2_output=sample_layer2_output,
        )
        assert clean_text in prompt
        assert "complications" in prompt
        assert "JSON" in prompt
