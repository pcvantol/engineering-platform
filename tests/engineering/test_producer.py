"""Regression coverage for the producer-neutral Execution Host boundary."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from tools.engineering.producer import ProducerSubmissionError, parse_producer_metadata, parse_producer_submission
from tools.engineering.recommendation_handoff import parse_forge_recommendation_handoff
from tools.engineering.execution_host import execution_mode_for


class ProducerContractTest(unittest.TestCase):
    def test_valid_envelope_preserves_forward_fields_and_context_without_prompt_parsing(self) -> None:
        raw = json.dumps({
            "contract": {"name": "djconnect.producer_submission", "version": "1.0", "future": True},
            "submission": {"id": "submission-42", "metadata": {"future": "kept"}},
            "producer": {"id": "forge", "type": "FORGE", "mission_id": "MISSION-42"},
            "prompt": {"text": "Execution Mode: Genesis", "metadata": {"future": "kept"}},
            "execution_context": {"context_version": "2.0", "mission_title": "Aurora", "future": {"kept": True}},
            "future_top_level": ["kept"],
        })
        submission = parse_producer_submission(raw)
        self.assertFalse(submission.is_legacy)
        self.assertEqual(submission.submission_id, "submission-42")
        self.assertEqual(submission.producer.mission_id, "MISSION-42")
        self.assertEqual(submission.execution_context, {"context_version": "2.0", "mission_title": "Aurora", "future": {"kept": True}})
        self.assertEqual(submission.envelope["future_top_level"], ["kept"])

    def test_envelope_without_execution_context_is_valid(self) -> None:
        submission = parse_producer_submission(json.dumps({
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": "submission-1"},
            "producer": {"id": "producer-1", "type": "EXTERNAL"},
            "prompt": {"text": "A bounded action"},
        }))
        self.assertIsNone(submission.execution_context)

    def test_invalid_json_like_envelope_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProducerSubmissionError, "valid JSON"):
            parse_producer_submission('{"contract":')
        with self.assertRaisesRegex(ProducerSubmissionError, "context_version"):
            parse_producer_submission(json.dumps({
                "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
                "submission": {"id": "submission-1"},
                "producer": {"id": "producer-1", "type": "FORGE"},
                "prompt": {"text": "A bounded action"},
                "execution_context": {},
            }))

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
