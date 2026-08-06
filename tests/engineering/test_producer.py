"""Regression coverage for the producer-neutral Execution Host boundary."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from tools.engineering.producer import parse_producer_metadata
from tools.engineering.recommendation_handoff import parse_forge_recommendation_handoff
from tools.engineering.execution_host import execution_mode_for


class ProducerContractTest(unittest.TestCase):
    def test_legacy_prompt_defaults_to_a_human_producer(self) -> None:
        self.assertEqual(parse_producer_metadata("# Existing prompt").producer_id, "legacy")
        self.assertEqual(parse_producer_metadata("# Existing prompt").producer_type, "HUMAN")

    def test_forge_contract_preserves_audit_metadata(self) -> None:
        metadata = parse_producer_metadata(
            "\n".join((
                "Producer ID: forge", "Producer Type: FORGE", "Producer Version: 2.0",
                "Producer Correlation ID: corr-42", "Mission ID: MISSION-0003",
                "Engineering Action ID: EA-0042", "Execution Constraint Version: 1.0",
            ))
        )
        self.assertEqual(metadata.producer_type, "FORGE")
        self.assertEqual(metadata.mission_id, "MISSION-0003")
        self.assertEqual(metadata.engineering_action_id, "EA-0042")

    def test_unknown_and_future_producer_types_remain_observable(self) -> None:
        self.assertEqual(parse_producer_metadata("Producer Type: UNKNOWN").producer_type, "UNKNOWN")
        self.assertEqual(parse_producer_metadata("Producer Type: PARTNER_V2").producer_type, "PARTNER_V2")

    def test_producer_fields_do_not_change_execution_mode_interpretation(self) -> None:
        legacy = "Execution Mode: Genesis"
        producer_prompt = "Producer Type: FORGE\nProducer ID: forge\n" + legacy
        self.assertEqual(parse_producer_metadata(producer_prompt).producer_type, "FORGE")
        self.assertEqual(execution_mode_for(producer_prompt), execution_mode_for(legacy))

    def test_forge_recommendation_is_read_only_from_declared_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "forge-recommendation.json"
            artifact.write_text(json.dumps({
                "recommended_candidate": {
                    "recommendation_id": "REC-1", "title": "Mission Aurora", "mission_origin": "PORTFOLIO_INTELLIGENCE",
                    "rank": 1, "summary": "Highest validated value.", "business_value": "High", "confidence": "0.91",
                    "dependencies": ["DEC-7"], "decision_evidence_reference": "DEC-7", "status": "RECOMMENDED",
                },
                "alternative_candidates": [{"title": "Mission Borealis", "rank": 2, "ordering_reason": "Lower confidence", "status": "PROPOSED"}],
            }), encoding="utf-8")
            handoff = parse_forge_recommendation_handoff(
                "Forge Recommendation Artifact Path: forge-recommendation.json", root, producer_type="FORGE"
            )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(handoff.recommendation.title, "Mission Aurora")
        self.assertEqual(handoff.alternatives[0].rank, 2)
        self.assertEqual(handoff.projection_status, "COMPLETE")

    def test_missing_decision_evidence_is_explicitly_incomplete(self) -> None:
        handoff = parse_forge_recommendation_handoff(
            """Forge Recommendation Handoff JSON:
```json
{"recommendation": {"title": "Mission Aurora", "status": "RECOMMENDED"}}
```""", Path.cwd(), producer_type="FORGE"
        )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(handoff.projection_status, "INCOMPLETE")
        self.assertEqual(handoff.missing_fields, ("Decision Evidence reference",))
