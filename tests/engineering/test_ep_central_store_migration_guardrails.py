"""Regression coverage for the Phase-2 central-store migration control contract."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
GUARDRAILS = ROOT / "docs/engineering/EP_CENTRAL_STORE_MIGRATION_GUARDRAILS.md"
ADR = ROOT / "docs/adr/0023-ep-central-store-migration-guardrails.md"
CUTOVER_ADR = ROOT / "docs/adr/0024-ep-controlled-central-store-cutover.md"


class CentralStoreMigrationGuardrailTests(unittest.TestCase):
    def test_portable_contract_and_macOS_realization_are_canonical(self) -> None:
        contents = GUARDRAILS.read_text(encoding="utf-8")
        self.assertIn('platformdirs.user_data_dir("Engineering Platform")', contents)
        self.assertIn('/Users/pcvantol/Library/Application Support/Engineering Platform', contents)
        self.assertIn('<user_data_dir("Engineering Platform")>/engineering.db', contents)

    def test_fail_closed_cardinality_and_preflight_rules_are_recorded(self) -> None:
        contents = GUARDRAILS.read_text(encoding="utf-8")
        for value in (
            "LEGACY_STORE_NOT_FOUND",
            "LEGACY_STORE_AMBIGUOUS",
            "TARGET_STORE_CONFLICT",
            "ACTIVE_EXECUTION",
            "ACTIVE_LEASE",
            "SOURCE_SCHEMA_MISMATCH",
            "SOURCE_INTEGRITY_FAILED",
            "BACKUP_FAILED",
            "COPY_FAILED",
            "TARGET_INTEGRITY_FAILED",
            "EVIDENCE_MISMATCH",
            "AUTHORITY_HANDOFF_NOT_SAFE",
        ):
            self.assertIn(value, contents)
        self.assertIn("creates neither directory nor database", contents)
        self.assertIn("no SQLite or filesystem mutation", contents)

    def test_one_writer_and_phase_boundaries_remain_explicit(self) -> None:
        contents = GUARDRAILS.read_text(encoding="utf-8")
        self.assertIn("There is no dual-writer interval", contents)
        self.assertIn("LEGACY_ROLLBACK_COMPATIBLE", contents)
        self.assertIn("`LEGACY_ROLLBACK_RETIRED` is not authorized", contents)
        self.assertIn("REIMPLEMENTATION FROM SCRATCH IS FORBIDDEN", contents)
        self.assertIn("Phase 5 legacy removal", contents)
        self.assertIn("authorizes no database relocation", ADR.read_text(encoding="utf-8"))

    def test_increment_three_authorization_remains_operator_only_and_post_write_safe(self) -> None:
        contents = GUARDRAILS.read_text(encoding="utf-8")
        decision = CUTOVER_ADR.read_text(encoding="utf-8")
        for value in (
            "ADMISSION_FROZEN",
            "CENTRAL_STORE_ACTIVE_POST_WRITE",
            "admission_freeze.v1",
            "store-authority.json",
            "SOURCE_CHANGED_AFTER_PREFLIGHT",
            "TARGET_EQUIVALENCE_FAILED",
            "CENTRAL_STORE_NOT_IN_USE",
            "ROLLBACK_FAILED",
        ):
            self.assertIn(value, contents)
        self.assertIn("direct legacy rollback is no longer permitted", contents)
        self.assertIn("Prompt content, watcher payloads, Local Consumer API input", contents)
        self.assertIn("schema `40`", decision)
        self.assertIn("central_store_migration stage-a", contents)
        self.assertIn("The sole Stage-A predecessor is `SERVICES_RESTARTED`", contents)
