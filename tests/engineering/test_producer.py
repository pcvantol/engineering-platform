"""Regression coverage for the producer-neutral Execution Host boundary."""

from __future__ import annotations

import unittest

from tools.engineering.producer import parse_producer_metadata
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
