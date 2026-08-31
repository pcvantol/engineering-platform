"""Regression coverage for the producer-neutral Execution Host boundary."""

from __future__ import annotations

import unittest
import json

from engineering_platform.producer import ProducerSubmissionError, parse_producer_metadata, parse_producer_submission
from engineering_platform.recommendation_handoff import ForgeGovernanceHandoff, report_lines
from engineering_platform.execution_host import execution_mode_for
from engineering_platform.validation_profile import producer_profile_payload


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

    def test_structured_validation_only_requires_a_canonical_profile_without_prompt_inference(self) -> None:
        envelope = {
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": "submission-validation-only"},
            "producer": {"id": "forge", "type": "FORGE"},
            "prompt": {"text": "validation_profile: DASHBOARD"},
            "execution_context": {"context_version": "1.0", "action_intent": "VALIDATION_ONLY"},
        }
        with self.assertRaisesRegex(ProducerSubmissionError, "validation_profile is required"):
            parse_producer_submission(json.dumps(envelope))
        envelope["execution_context"]["validation_profile"] = producer_profile_payload("DASHBOARD")
        self.assertEqual(
            parse_producer_submission(json.dumps(envelope)).execution_context["validation_profile"],
            producer_profile_payload("DASHBOARD"),
        )

    def test_profile_and_action_intent_are_independent_explicit_fields(self) -> None:
        envelope = {
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": "submission-mutating-profile"},
            "producer": {"id": "forge", "type": "FORGE"},
            "prompt": {"text": "validation only in prose"},
            "execution_context": {
                "context_version": "1.0", "action_intent": "MUTATING_DELIVERY",
                "validation_profile": producer_profile_payload("DASHBOARD"),
            },
        }
        self.assertEqual(parse_producer_submission(json.dumps(envelope)).execution_context["action_intent"], "MUTATING_DELIVERY")

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

    def test_forge_governance_handoff_is_versioned_and_read_only(self) -> None:
        raw = json.dumps({
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": "submission-forge"}, "producer": {"id": "forge", "type": "FORGE"},
            "prompt": {"text": "A bounded action"},
            "forge_governance_handoff": {"version": "1.0", "recommendation_set": {"id": "set-1", "count": 2},
                "selected_recommendation": {"recommendation_id": "REC-1", "title": "Mission Aurora", "rank": 1, "lifecycle_status": "RECOMMENDED", "confidence": "0.91"},
                "alternatives": [{"recommendation_id": "REC-2", "title": "Mission Borealis", "rank": 2, "lifecycle_status": "PROPOSED", "confidence": "0.7"}],
                "decision_evidence": {"id": "DEC-7", "type": "RANKING", "timestamp": "2026-08-15T00:00:00Z"},
                "governance": {"business_approval_state": "PENDING"}},
        })
        submission = parse_producer_submission(raw)
        assert submission.forge_governance_handoff is not None
        handoff = ForgeGovernanceHandoff.from_snapshot(submission.forge_governance_handoff)
        self.assertEqual(handoff.recommendation_set_id, "set-1")
        self.assertEqual(handoff.alternatives[0].rank, 2)
        self.assertEqual(handoff.completeness, "COMPLETE")
        self.assertIn("Selected Recommendation ID: `REC-1`", "\n".join(report_lines(handoff, "COMPLETE")))

    def test_malformed_supplied_governance_handoff_fails_closed(self) -> None:
        raw = json.dumps({
            "contract": {"name": "djconnect.producer_submission", "version": "1.0"},
            "submission": {"id": "submission-invalid"}, "producer": {"id": "forge", "type": "FORGE"},
            "prompt": {"text": "A bounded action"}, "forge_governance_handoff": {"version": "1.0", "alternatives": "invalid"},
        })
        with self.assertRaisesRegex(ProducerSubmissionError, "alternatives"):
            parse_producer_submission(raw)
