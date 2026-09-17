"""Regressions for scoped reporting outcomes and submission evidence integrity."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform.agent_state import TransactionState
from engineering_platform.execution_reporting import (
    _outcome_projection,
    _persisted_producer_submission,
    collect_terminal_evidence,
    generate_terminal_report,
    report_consistency_errors,
)
from engineering_platform.managed_autonomy import terminal_snapshot
from engineering_platform.storage import (
    EngineeringStorageError,
    SUBMISSION_EVIDENCE_CORRUPT,
    SUBMISSION_EVIDENCE_IDENTITY_CONFLICT,
    SUBMISSION_EVIDENCE_LEGACY_ABSENT,
    SUBMISSION_EVIDENCE_MISSING_MODERN_BINDING,
    SUBMISSION_EVIDENCE_STORAGE_UNAVAILABLE,
    SUBMISSION_EVIDENCE_VALID_MODERN,
    database_path,
    record_run_qualification_context,
    record_run_qualification_snapshot,
    record_submission,
    resolve_submission_attempt_evidence,
    sqlite_connection,
)


class ReportingIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("# Bounded objective\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _state(self, run_id: str, phase: str = "COMPLETE", **values: object) -> TransactionState:
        return TransactionState(
            run_id,
            "pcvantol/engineering-platform",
            str(self.prompt),
            phase,
            terminal=True,
            **values,
        )

    def _record_submission(
        self,
        *,
        submission_id: str,
        run_id: str,
        producer_id: str = "forge",
        correlation_id: str = "correlation-1",
        retry_parent_submission_id: str | None = None,
        constraints: dict[str, object] | None = None,
    ) -> None:
        record_submission(
            self.root,
            submission_id=submission_id,
            producer_id=producer_id,
            producer_type="FORGE",
            producer_version="1.2",
            contract_version="1.2",
            correlation_id=correlation_id,
            mission_id="MISSION-0003",
            engineering_action_id=f"ACTION-{submission_id}",
            prompt_content="bounded modern submission",
            prompt_metadata={"constraints": constraints or {"scope": submission_id}},
            target_identity={"repository": "pcvantol/engineering-platform"},
            original_envelope={"kind": "synthetic"},
            received_at="2026-09-17T10:00:00+00:00",
            link_run_id=run_id,
            retry_parent_submission_id=retry_parent_submission_id,
        )

    def _create_central_submission_rows(self, rows: tuple[tuple[str, str, str], ...]) -> Path:
        database = database_path(self.root)
        with sqlite_connection(database) as connection:
            connection.execute(
                "CREATE TABLE ep_submissions("
                "submission_id TEXT PRIMARY KEY,producer_id TEXT,producer_type TEXT,"
                "producer_version TEXT,correlation_id TEXT,mission_id TEXT,"
                "engineering_action_id TEXT,constraints TEXT)"
            )
            connection.executemany(
                "INSERT INTO ep_submissions VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        submission_id,
                        producer_id,
                        "FORGE",
                        "1.2",
                        "correlation-1",
                        "MISSION-0003",
                        f"ACTION-{submission_id}",
                        constraints,
                    )
                    for submission_id, producer_id, constraints in rows
                ],
            )
        return database

    def _record_qualification_snapshot(
        self,
        state: TransactionState,
        *,
        qualification: str,
        conflicts: list[str] | None = None,
    ) -> dict[str, object]:
        snapshot = terminal_snapshot(
            self.root,
            run_id=state.run_id,
            execution_outcome=state.phase,
            implementation_pr=None,
            finalization_pr=None,
            repository_state="MERGED_RECONCILED",
            workspace_state="WORKSPACE_READY",
            main_origin_sync="YES",
            worktree_state="CLEAN",
            active_blocker="NONE",
            recovery_required="NO",
            persist=False,
        )
        snapshot.update({
            "run_qualification": qualification,
            "required_validation_state": "PASS" if qualification == "QUALIFIED" else "UNRESOLVED",
            "qualification_failure_reasons": [] if qualification == "QUALIFIED" else ["synthetic missing evidence"],
            "projection_conflicts": conflicts or [],
            "qualification_snapshot_id": f"qualification:synthetic:{state.run_id}",
            "required_control_snapshot_ref": f"required-controls:synthetic:{state.run_id}",
            "terminal_checkpoint_ref": f"terminal-checkpoint:{state.run_id}:{state.phase}",
            "persisted_at": "2026-09-17T10:00:00+00:00",
            "reconciliation_evidence": {
                "repository_state": "MERGED_RECONCILED",
                "workspace_state": "WORKSPACE_READY",
            },
        })
        return record_run_qualification_snapshot(self.root, snapshot)

    @staticmethod
    def _summary(body: str) -> dict[str, object]:
        match = re.search(
            r"^## Engineering Evidence Summary\s*$\n```json\n(?P<body>.*?)\n```",
            body,
            re.MULTILINE | re.DOTALL,
        )
        if match is None:
            raise AssertionError("Engineering Evidence Summary missing")
        return json.loads(match.group("body"))

    def test_complete_not_qualified_is_not_general_acceptance(self) -> None:
        self.prompt.write_text(
            "Never claim PASS for missing evidence. Historical quote: PASS.\n",
            encoding="utf-8",
        )
        assurance = ({
            "reviewer": "quality",
            "status": "PASS",
            "candidate_sha": "a" * 40,
            "profile_digest": "sha256:" + "b" * 64,
            "invocation_id": "quality-review-1",
            "findings": [],
        },)
        body = generate_terminal_report(
            self.root,
            self._state("complete-not-qualified", assurance_reviews=assurance),
        ).read_text(encoding="utf-8")

        self.assertIn("- Technical Delivery: `COMPLETE`", body)
        self.assertIn("- EP Run Qualification: `NOT_QUALIFIED`", body)
        self.assertIn("- Mission / Autonomy Acceptance: `NOT_ESTABLISHED_BY_EP`", body)
        self.assertIn("- Reviewer: `quality`\n  - Status: `PASS`", body)
        self.assertNotRegex(body, r"Final Deliverable Answer:.*(?:YES|PASS|GO)")
        outcomes = self._summary(body)["outcomes"]
        self.assertEqual(outcomes["technical_delivery"], "COMPLETE")
        self.assertEqual(outcomes["ep_run_qualification"], "NOT_QUALIFIED")
        self.assertEqual(outcomes["mission_autonomy_acceptance"], "NOT_ESTABLISHED_BY_EP")

    def test_qualification_and_acceptance_semantics_are_independent(self) -> None:
        complete = self._state("outcome-matrix")
        missing = _outcome_projection(complete, None)
        qualified = _outcome_projection(complete, {
            "run_qualification": "QUALIFIED",
            "qualification_snapshot_id": "qualification:valid",
            "projection_conflicts": [],
        })
        conflict = _outcome_projection(complete, {
            "run_qualification": "NOT_QUALIFIED",
            "qualification_snapshot_id": "qualification:conflict",
            "projection_conflicts": ["REQUIRED_CONTROL_CONFLICT"],
        })

        self.assertEqual(missing["ep_run_qualification"], "UNAVAILABLE")
        self.assertEqual(qualified["ep_run_qualification"], "QUALIFIED")
        self.assertEqual(conflict["ep_run_qualification"], "EVIDENCE_CONFLICT")
        self.assertEqual(conflict["ep_run_qualification_persisted_outcome"], "NOT_QUALIFIED")
        for projection in (missing, qualified, conflict):
            self.assertEqual(projection["technical_delivery"], "COMPLETE")
            self.assertEqual(projection["mission_autonomy_acceptance"], "NOT_ESTABLISHED_BY_EP")

    def test_complete_qualified_report_still_does_not_claim_mission_acceptance(self) -> None:
        state = self._state("complete-qualified")
        self._record_qualification_snapshot(state, qualification="QUALIFIED")

        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")

        self.assertIn("- Technical Delivery: `COMPLETE`", body)
        self.assertIn("- EP Run Qualification: `QUALIFIED`", body)
        self.assertIn("- Mission / Autonomy Acceptance: `NOT_ESTABLISHED_BY_EP`", body)
        self.assertNotIn("Final Deliverable Answer:", body)

    def test_evidence_conflict_is_visible_in_readable_and_machine_projection(self) -> None:
        state = self._state("complete-conflict")
        self._record_qualification_snapshot(
            state,
            qualification="NOT_QUALIFIED",
            conflicts=["REQUIRED_CONTROL_CONFLICT"],
        )

        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        outcomes = self._summary(body)["outcomes"]

        self.assertIn("- EP Run Qualification: `EVIDENCE_CONFLICT`", body)
        self.assertIn("- EP Qualification Conflicts: `REQUIRED_CONTROL_CONFLICT`", body)
        self.assertEqual(outcomes["ep_run_qualification"], "EVIDENCE_CONFLICT")
        self.assertEqual(outcomes["ep_run_qualification_persisted_outcome"], "NOT_QUALIFIED")

    def test_consistency_validator_rejects_only_unscoped_acceptance_section(self) -> None:
        state = self._state("consistency-scope")
        body = generate_terminal_report(self.root, state).read_text(encoding="utf-8")
        outcome = self._summary(body)["outcomes"]
        bundle = collect_terminal_evidence(self.root, state)
        self.assertEqual(report_consistency_errors(body, state, bundle, "PASS", outcome), ())

        tampered = body.replace(
            "## Deliverable Answer\n",
            "## Deliverable Answer\n- Final Deliverable Answer: YES / PASS / GO\n",
            1,
        )
        errors = report_consistency_errors(tampered, state, bundle, "PASS", outcome)
        self.assertIn("deliverable answer contains an unscoped acceptance conclusion", errors)

    def test_blocked_and_failed_keep_factual_execution_status(self) -> None:
        for phase in ("BLOCKED", "FAILED"):
            with self.subTest(phase=phase):
                body = generate_terminal_report(
                    self.root,
                    self._state(f"terminal-{phase.casefold()}", phase),
                ).read_text(encoding="utf-8")
                self.assertIn(f"- Technical Delivery: `{phase}`", body)
                self.assertIn("- EP Run Qualification: `NOT_QUALIFIED`", body)
                self.assertIn("- Mission / Autonomy Acceptance: `NOT_ESTABLISHED_BY_EP`", body)
                self.assertNotRegex(body, r"Final Deliverable Answer:.*(?:YES|PASS|GO)")

    def test_valid_modern_submission_uses_immutable_evidence_without_prompt_parser(self) -> None:
        self._record_submission(submission_id="submission-root", run_id="modern-root")
        state = self._state("modern-root")
        with patch(
            "engineering_platform.execution_reporting.parse_producer_metadata",
            side_effect=AssertionError("prompt fallback must not run"),
        ):
            producer, submission, provenance = _persisted_producer_submission(
                self.root, state, "Mission ID: WRONG",
            )
        self.assertEqual(producer.mission_id, "MISSION-0003")
        self.assertEqual(submission["submission_id"], "submission-root")
        self.assertEqual(provenance, "IMMUTABLE_SUBMISSION")

    def test_valid_modern_retry_preserves_attempt_and_root_identities(self) -> None:
        self._record_submission(submission_id="submission-root", run_id="run-root")
        self._record_submission(
            submission_id="submission-retry",
            run_id="run-retry",
            retry_parent_submission_id="submission-root",
            constraints={"attempt": "retry"},
        )
        database = self._create_central_submission_rows((
            ("submission-root", "forge", '{"attempt":"root"}'),
            ("submission-retry", "forge", '{"attempt":"retry"}'),
        ))

        result = resolve_submission_attempt_evidence(
            self.root, "run-retry", central_database=database,
        )

        self.assertEqual(result.status, SUBMISSION_EVIDENCE_VALID_MODERN)
        self.assertEqual(result.submission["submission_id"], "submission-retry")
        self.assertEqual(result.submission["canonical_submission_id"], "submission-root")
        self.assertEqual(result.submission["constraints"], {"attempt": "retry"})

    def test_identity_conflict_and_corrupt_constraints_never_call_prompt_fallback(self) -> None:
        for case, producer_id, constraints, expected in (
            ("identity", "other-producer", '{}', SUBMISSION_EVIDENCE_IDENTITY_CONFLICT),
            ("corrupt", "forge", '[not-json', SUBMISSION_EVIDENCE_CORRUPT),
            ("wrong-type", "forge", '[]', SUBMISSION_EVIDENCE_CORRUPT),
        ):
            with self.subTest(case=case):
                root = self.root / case
                root.mkdir()
                original_root = self.root
                self.root = root
                try:
                    self._record_submission(submission_id="submission-root", run_id="conflict-run")
                    database = self._create_central_submission_rows((
                        ("submission-root", producer_id, constraints),
                    ))
                    resolution = resolve_submission_attempt_evidence(
                        self.root, "conflict-run", central_database=database,
                    )
                    self.assertEqual(resolution.status, expected)
                    with patch(
                        "engineering_platform.execution_reporting.parse_producer_metadata",
                        side_effect=AssertionError("prompt fallback must not run"),
                    ), self.assertRaisesRegex(EngineeringStorageError, "SUBMISSION_EVIDENCE_"):
                        _persisted_producer_submission(
                            self.root,
                            self._state("conflict-run"),
                            "Mission ID: WRONG\nProducer ID: attacker",
                            central_database=database,
                        )
                finally:
                    self.root = original_root

    def test_missing_modern_binding_and_storage_failure_are_not_legacy(self) -> None:
        self._record_submission(submission_id="submission-root", run_id="missing-attempt")
        database = self._create_central_submission_rows((
            ("submission-root", "forge", '{}'),
        ))
        with sqlite_connection(database) as connection:
            connection.execute(
                "DELETE FROM execution_submission_attempt_links WHERE run_id=?",
                ("missing-attempt",),
            )
        missing = resolve_submission_attempt_evidence(
            self.root, "missing-attempt", central_database=database,
        )
        unavailable = resolve_submission_attempt_evidence(
            self.root,
            "unavailable-run",
            central_database=self.root / "missing-central.sqlite",
        )
        self.assertEqual(missing.status, SUBMISSION_EVIDENCE_MISSING_MODERN_BINDING)
        self.assertEqual(unavailable.status, SUBMISSION_EVIDENCE_STORAGE_UNAVAILABLE)
        with patch(
            "engineering_platform.execution_reporting.parse_producer_metadata",
            side_effect=AssertionError("prompt fallback must not run"),
        ):
            for run_id, central_database in (
                ("missing-attempt", database),
                ("unavailable-run", self.root / "missing-central.sqlite"),
            ):
                with self.subTest(run_id=run_id), self.assertRaisesRegex(
                    EngineeringStorageError,
                    "SUBMISSION_EVIDENCE_",
                ):
                    _persisted_producer_submission(
                        self.root,
                        self._state(run_id),
                        "Mission ID: WRONG",
                        central_database=central_database,
                    )

        modern_without_submission = self.root / "lineage"
        modern_without_submission.mkdir()
        record_run_qualification_context(
            modern_without_submission,
            run_id="modern-lineage",
            submission_id="expected-submission",
            fresh_submission=True,
            retry_parent_run_id=None,
            resume_parent_run_id=None,
            recorded_at="2026-09-17T10:00:00+00:00",
        )
        result = resolve_submission_attempt_evidence(modern_without_submission, "modern-lineage")
        self.assertEqual(result.status, SUBMISSION_EVIDENCE_MISSING_MODERN_BINDING)

    def test_supported_legacy_absence_is_the_only_prompt_fallback(self) -> None:
        state = self._state("legacy-run")
        result = resolve_submission_attempt_evidence(self.root, state.run_id)
        self.assertEqual(result.status, SUBMISSION_EVIDENCE_LEGACY_ABSENT)
        with patch(
            "engineering_platform.execution_reporting.parse_producer_metadata",
            wraps=__import__(
                "engineering_platform.execution_reporting",
                fromlist=["parse_producer_metadata"],
            ).parse_producer_metadata,
        ) as parser:
            _producer, submission, provenance = _persisted_producer_submission(
                self.root,
                state,
                "Producer ID: legacy-human\nMission ID: MISSION-LEGACY",
            )
        parser.assert_called_once()
        self.assertIsNone(submission)
        self.assertEqual(provenance, "LEGACY_PROMPT_METADATA")

    def test_integrated_report_refuses_modern_conflict_without_mutation_or_fallback(self) -> None:
        self._record_submission(submission_id="submission-root", run_id="integrated-conflict")
        database = self._create_central_submission_rows((
            ("submission-root", "wrong-producer", '{}'),
        ))
        with sqlite_connection(database) as connection:
            before = connection.execute(
                "SELECT submission_id,producer_id,prompt_metadata FROM execution_submissions"
            ).fetchall()
        report_directory = database.parent / "artifacts" / "reports"

        with patch(
            "engineering_platform.execution_reporting.parse_producer_metadata",
            side_effect=AssertionError("prompt fallback must not run"),
        ), self.assertRaisesRegex(
            EngineeringStorageError,
            "SUBMISSION_EVIDENCE_IDENTITY_CONFLICT",
        ):
            generate_terminal_report(
                self.root,
                self._state("integrated-conflict"),
                central_database=database,
            )

        with sqlite_connection(database) as connection:
            after = connection.execute(
                "SELECT submission_id,producer_id,prompt_metadata FROM execution_submissions"
            ).fetchall()
        self.assertEqual(after, before)
        self.assertFalse(report_directory.exists())


if __name__ == "__main__":
    unittest.main()
