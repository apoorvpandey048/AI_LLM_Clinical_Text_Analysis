"""
Phase 1 regression: the dynamic pipeline must run the client's 3 monolithic
prompts as 3 separate calls — NOT the decomposed sub-layers, and NOT the
rule-free layer3d_auditor — with the final CCI computed in Python from
Layer 3's audited final_episode_set.

See fix/restore-3layer-pipeline.
"""

from unittest.mock import AsyncMock

from app.pipeline.orchestrator import PipelineOrchestrator
from app.pipeline.base import LayerResult


def _result(output: dict, name: str) -> LayerResult:
    return LayerResult(
        success=True,
        output=output,
        raw_response="{}",
        duration_ms=1,
        tokens_input=1,
        tokens_output=1,
        error=None,
        layer_name=name,
        prompt_version="1.3",
    )


class TestDynamicPipelineRestored3Layer:
    async def test_runs_three_monolithic_layers_without_decomposition_or_l3d(self, mock_llm_client):
        orch = PipelineOrchestrator(mock_llm_client)

        # Monolithic client-design layers — MUST be called.
        orch.layer1.execute = AsyncMock(return_value=_result(
            {"clean_course_text": "clean", "extracted_dates": {}, "drug_normalization": {}, "notes": ""},
            "layer1_ctp",
        ))
        orch.layer2.execute = AsyncMock(return_value=_result(
            {"complications": [{"complication": "Postoperative atrial fibrillation", "cd_grade": "II"}]},
            "layer2_cie",
        ))
        orch.layer3.execute = AsyncMock(return_value=_result(
            {
                "verdict": "PASS",
                "rule_violation": False,
                "rule_violation_notes": "",
                "episode_checks": [],
                "omission_probes": {},
                "likely_omissions": [],
                "cci_check": {"reported_cci": 20.9, "expected_cci": 20.9, "cci_mismatch": False},
                "audited_result": {
                    "final_episode_set": [
                        {"complication": "Postoperative atrial fibrillation", "cd_grade": "II"}
                    ]
                },
                "final_notes": "",
            },
            "layer3_ccc",
        ))

        # Retired decomposition + rule-free auditor — MUST NOT be called.
        orch.layer2a.execute = AsyncMock()
        orch.layer2b.execute = AsyncMock()
        orch.layer3d.execute = AsyncMock()

        pipeline_layers = [
            {"layer_name": "layer1_ctp", "is_builtin": True, "prompt": None, "display_order": 0},
            {"layer_name": "layer2_cie", "is_builtin": True, "prompt": None, "display_order": 1},
            {"layer_name": "layer3_ccc", "is_builtin": True, "prompt": None, "display_order": 2},
        ]

        result = await orch.execute(raw_text="Fall #1 postoperativer Verlauf", pipeline_layers=pipeline_layers)

        # 3-layer client design is used.
        orch.layer1.execute.assert_awaited_once()
        orch.layer2.execute.assert_awaited_once()
        orch.layer3.execute.assert_awaited_once()

        # Decomposed sub-layers and the rule-free auditor are gone.
        orch.layer2a.execute.assert_not_awaited()
        orch.layer2b.execute.assert_not_awaited()
        orch.layer3d.execute.assert_not_awaited()

        # CCI is computed in Python from Layer 3's audited set: a single Grade II => 20.9.
        assert result.success is True
        assert result.final_cci == 20.9
