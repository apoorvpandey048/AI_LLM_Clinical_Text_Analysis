"""
Phase 3 — deterministic Grade-IV guard.

Grade IV must require an explicit organ-support token (ventilation / vasopressors /
dialysis / stated organ failure). An ICU/IMC mention alone is not enough. The guard
downgrades unsupported IV grades to IIIb and enforces IVa-vs-IVb by organ count.

Targets the verified over-grades: case 90 (AF+ICU → IVa) and case 5 (bleed+ICU → IVb).
"""

from unittest.mock import AsyncMock

from app.pipeline.organ_failure_detector import guard_iv_requires_organ_support
from app.pipeline.orchestrator import PipelineOrchestrator
from app.pipeline.base import LayerResult


class TestIVOrganGuardUnit:
    def test_iv_without_organ_token_is_downgraded_to_iiib(self):
        eps = [{"complication": "Atrial fibrillation with hypotension", "cd_grade": "IVa"}]
        actions = guard_iv_requires_organ_support(
            eps, full_clinical_text="ICU transfer for monitoring; amiodarone given."
        )
        assert eps[0]["cd_grade"] == "IIIb"
        assert actions[0]["from_grade"] == "IVa" and actions[0]["to_grade"] == "IIIb"

    def test_iva_with_vasopressor_is_kept(self):
        eps = [{"complication": "Circulatory failure", "cd_grade": "IVa"}]
        guard_iv_requires_organ_support(
            eps, full_clinical_text="Noradrenalin infusion for circulatory support."
        )
        assert eps[0]["cd_grade"] == "IVa"

    def test_two_organs_promotes_to_ivb(self):
        eps = [{"complication": "Severe deterioration", "cd_grade": "IVa"}]
        guard_iv_requires_organ_support(
            eps, full_clinical_text="Required mechanical ventilation and dialysis."
        )
        assert eps[0]["cd_grade"] == "IVb"

    def test_non_iv_grades_untouched(self):
        eps = [{"complication": "Wound infection", "cd_grade": "II"},
               {"complication": "Pleural puncture", "cd_grade": "IIIa"}]
        actions = guard_iv_requires_organ_support(eps, full_clinical_text="antibiotics; puncture")
        assert [e["cd_grade"] for e in eps] == ["II", "IIIa"]
        assert actions == []


def _result(output: dict, name: str) -> LayerResult:
    return LayerResult(
        success=True, output=output, raw_response="{}", duration_ms=1,
        tokens_input=1, tokens_output=1, error=None, layer_name=name, prompt_version="1.3",
    )


class TestIVGuardEndToEnd:
    async def test_unsupported_iva_downgrades_final_cci_to_iiib(self, mock_llm_client):
        orch = PipelineOrchestrator(mock_llm_client)
        # Clean text has NO organ-support token (only an ICU mention).
        orch.layer1.execute = AsyncMock(return_value=_result(
            {"clean_course_text": "Postoperative atrial fibrillation; transferred to ICU for monitoring.",
             "extracted_dates": {}, "drug_normalization": {}, "notes": ""}, "layer1_ctp"))
        orch.layer2.execute = AsyncMock(return_value=_result(
            {"complications": [{"complication": "Atrial fibrillation", "cd_grade": "IVa"}]}, "layer2_cie"))
        orch.layer3.execute = AsyncMock(return_value=_result(
            {"verdict": "PASS", "rule_violation": False, "rule_violation_notes": "",
             "episode_checks": [], "omission_probes": {}, "likely_omissions": [],
             "cci_check": {"reported_cci": 42.4, "expected_cci": 42.4, "cci_mismatch": False},
             "audited_result": {"final_episode_set": [{"complication": "Atrial fibrillation", "cd_grade": "IVa"}]},
             "final_notes": ""}, "layer3_ccc"))

        pipeline_layers = [
            {"layer_name": "layer1_ctp", "is_builtin": True, "prompt": None, "display_order": 0},
            {"layer_name": "layer2_cie", "is_builtin": True, "prompt": None, "display_order": 1},
            {"layer_name": "layer3_ccc", "is_builtin": True, "prompt": None, "display_order": 2},
        ]
        result = await orch.execute(raw_text="x", pipeline_layers=pipeline_layers)

        # IVa (42.4) without an organ token must be guarded down to IIIb (33.7).
        assert result.final_cci == 33.7
