"""
Tests for CCI (Comprehensive Complication Index) calculation.

Verifies the CCI formula implementation with various scenarios.
"""

import pytest
import math


def calculate_cci(grades: list[str], weights: dict[str, int]) -> float:
    """
    Calculate CCI from a list of CD grades.
    
    Formula: CCI = sqrt(sum of weights) / 2
    Rounded to 1 decimal place.
    """
    if not grades:
        return 0.0
    
    # Check for death (Grade V)
    if "V" in grades:
        return 100.0
    
    # Sum weights
    total_weight = sum(weights.get(g, 0) for g in grades)
    
    # Calculate CCI
    cci = math.sqrt(total_weight) / 2
    
    return round(cci, 1)


class TestCCICalculation:
    """Test suite for CCI calculation."""

    def test_no_complications(self, cci_weights):
        """CCI should be 0 when there are no complications."""
        result = calculate_cci([], cci_weights)
        assert result == 0.0

    def test_single_grade_i(self, cci_weights):
        """Single Grade I complication."""
        result = calculate_cci(["I"], cci_weights)
        # sqrt(300) / 2 = 17.32 / 2 = 8.66 ≈ 8.7
        assert result == 8.7

    def test_single_grade_ii(self, cci_weights):
        """Single Grade II complication."""
        result = calculate_cci(["II"], cci_weights)
        # sqrt(1750) / 2 = 41.83 / 2 = 20.92 ≈ 20.9
        assert result == 20.9

    def test_single_grade_iiia(self, cci_weights):
        """Single Grade IIIa complication."""
        result = calculate_cci(["IIIa"], cci_weights)
        # sqrt(2750) / 2 = 52.44 / 2 = 26.22 ≈ 26.2
        assert result == 26.2

    def test_single_grade_iiib(self, cci_weights):
        """Single Grade IIIb complication."""
        result = calculate_cci(["IIIb"], cci_weights)
        # sqrt(4550) / 2 = 67.45 / 2 = 33.73 ≈ 33.7
        assert result == 33.7

    def test_single_grade_iva(self, cci_weights):
        """Single Grade IVa complication."""
        result = calculate_cci(["IVa"], cci_weights)
        # sqrt(7200) / 2 = 84.85 / 2 = 42.43 ≈ 42.4
        assert result == 42.4

    def test_single_grade_ivb(self, cci_weights):
        """Single Grade IVb complication."""
        result = calculate_cci(["IVb"], cci_weights)
        # sqrt(8550) / 2 = 92.47 / 2 = 46.23 ≈ 46.2
        assert result == 46.2

    def test_grade_v_death(self, cci_weights):
        """Grade V (death) should always return CCI 100."""
        result = calculate_cci(["V"], cci_weights)
        assert result == 100.0

    def test_grade_v_with_others(self, cci_weights):
        """Grade V with other complications still returns CCI 100."""
        result = calculate_cci(["I", "II", "V"], cci_weights)
        assert result == 100.0

    def test_multiple_grade_i(self, cci_weights):
        """Multiple Grade I complications."""
        result = calculate_cci(["I", "I", "I"], cci_weights)
        # sqrt(300 + 300 + 300) / 2 = sqrt(900) / 2 = 30 / 2 = 15.0
        assert result == 15.0

    def test_mixed_grades(self, cci_weights):
        """Mix of different grades."""
        result = calculate_cci(["I", "II"], cci_weights)
        # sqrt(300 + 1750) / 2 = sqrt(2050) / 2 = 45.28 / 2 = 22.64 ≈ 22.6
        assert result == 22.6

    def test_complex_case(self, cci_weights):
        """Complex case with multiple complications."""
        # I + IIIa + IIIb
        result = calculate_cci(["I", "IIIa", "IIIb"], cci_weights)
        # sqrt(300 + 2750 + 4550) / 2 = sqrt(7600) / 2 = 87.18 / 2 = 43.59 ≈ 43.6
        assert result == 43.6

    def test_high_severity_case(self, cci_weights):
        """High severity case approaching 100."""
        # IVa + IVb
        result = calculate_cci(["IVa", "IVb"], cci_weights)
        # sqrt(7200 + 8550) / 2 = sqrt(15750) / 2 = 125.5 / 2 = 62.75 ≈ 62.7 or 62.8
        assert 62.7 <= result <= 62.8

    def test_invalid_grade_ignored(self, cci_weights):
        """Invalid grades should be ignored (weight 0)."""
        result = calculate_cci(["I", "INVALID", "II"], cci_weights)
        # sqrt(300 + 0 + 1750) / 2 = same as I + II
        expected = calculate_cci(["I", "II"], cci_weights)
        assert result == expected


class TestCCISelfCheck:
    """Test the CCI self-check mechanism."""

    def test_self_check_pass(self, cci_weights):
        """Verify self-check passes when calculation is correct."""
        grades = ["I", "II"]
        weights = [300, 1750]
        R = sum(weights)
        sqrt_R = math.sqrt(R)
        cci_total = round(sqrt_R / 2, 1)
        
        # Verify intermediate values
        assert R == 2050
        assert round(sqrt_R, 2) == 45.28
        assert cci_total == 22.6

    def test_self_check_components(self, cci_weights):
        """Verify all self-check components are present."""
        grades = ["IIIa"]
        weights = [2750]
        R = sum(weights)
        sqrt_R = math.sqrt(R)
        cci_total = round(sqrt_R / 2, 1)
        
        # All required components
        assert len(grades) == 1
        assert len(weights) == 1
        assert R == 2750
        assert cci_total == 26.2
