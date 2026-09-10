"""Product closure for candidate-bound current validation publication evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_host import (
    EngineeringRunner,
    _has_current_local_validation_evidence,
    _required_validation_controls_pass,
    _validation_profile_digest,
)
from engineering_platform.execution_errors import RunnerError
from engineering_platform.execution_models import AgentResult, RepositoryEvidence
from engineering_platform.storage import (
    EngineeringStorageError,
    load_validation_context,
    open_storage,
    record_validation_command_invocation,
    record_validation_command_terminal,
    record_validation_profile,
)
from engineering_platform.validation_profile import (
    strict_required_controls_pass,
    validation_profile_identity,
)


CANDIDATE = "a" * 40
BINDING = {
    "validation_id": "required_suite",
    "required": True,
    "category": "repository",
    "control_identity": "canonical required suite",
    "command": ["python3", "-m", "unittest", "discover"],
}


def _context() -> dict[str, object]:
    identity, digest = validation_profile_identity(
        candidate_sha=CANDIDATE, currentness=1,
        selected_validation_tier="FULL", validation_profile_version="1.0",
        profile_reference="validation-profile-registry:FULL@1.0",
        profile_selection_source="diff_classification",
        required_validation_controls=("required_suite",),
        control_bindings=(BINDING,),
    )
    return {
        **identity,
        "required_validation_controls": tuple(identity["required_validation_controls"]),
        "control_bindings": tuple(identity["control_bindings"]),
        "profile_digest": digest,
        "profile_currentness_conflict": False,
        "controls": {
            "required_suite": {
                "validation_id": "required_suite",
                "command_id": "required-suite-1",
                "category": "repository",
                "control_identity": "canonical required suite",
                "required_for_profile": True,
                "execution_status": "EXECUTED",
                "result": "PASS",
                "exit_code": 0,
                "currentness": 1,
                "evidence_authority": "command_terminal",
                "started_at": "2026-09-10T10:00:00+00:00",
                "ended_at": "2026-09-10T10:00:01+00:00",
            },
            "provider_optional_claim": {
                "required_for_profile": True,
                "execution_status": "NOT_EXECUTED",
                "result": "FAIL",
            },
        },
    }


class StrictValidationContractTest(unittest.TestCase):
    def test_every_current_required_control_needs_an_exact_passing_terminal_receipt(self) -> None:
        context = _context()
        self.assertTrue(strict_required_controls_pass(context, candidate_sha=CANDIDATE, currentness=1))

        mutations = {
            "missing_required_control": lambda value: value["controls"].pop("required_suite"),
            "missing_receipt": lambda value: value["controls"]["required_suite"].pop("command_id"),
            "not_executed": lambda value: value["controls"]["required_suite"].update(execution_status="NOT_EXECUTED"),
            "failed": lambda value: value["controls"]["required_suite"].update(result="FAIL"),
            "skipped": lambda value: value["controls"]["required_suite"].update(result="SKIPPED"),
            "not_applicable": lambda value: value["controls"]["required_suite"].update(result="NOT_APPLICABLE"),
            "unavailable": lambda value: value["controls"]["required_suite"].update(result="UNAVAILABLE"),
            "timeout": lambda value: value["controls"]["required_suite"].update(result="TIMEOUT"),
            "nonzero": lambda value: value["controls"]["required_suite"].update(exit_code=7),
            "stale_ordinal": lambda value: value["controls"]["required_suite"].update(currentness=0),
            "wrong_digest": lambda value: value.update(profile_digest="sha256:" + "f" * 64),
            "wrong_required_set": lambda value: value.update(required_validation_controls=("other",)),
            "missing_started_at": lambda value: value["controls"]["required_suite"].update(started_at=None),
            "missing_ended_at": lambda value: value["controls"]["required_suite"].update(ended_at=None),
            "invalid_time_order": lambda value: value["controls"]["required_suite"].update(ended_at="2026-09-10T09:59:59+00:00"),
            "prose_only": lambda value: value["controls"]["required_suite"].update(
                command_id=None, evidence_authority="agent_result", exit_code=None,
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = deepcopy(context)
                mutate(changed)
                self.assertFalse(strict_required_controls_pass(changed, candidate_sha=CANDIDATE, currentness=1))

        self.assertFalse(strict_required_controls_pass(context, candidate_sha="b" * 40, currentness=1))
        self.assertFalse(strict_required_controls_pass(context, candidate_sha=CANDIDATE, currentness=2))

    def test_profile_and_receipts_are_append_only_and_recover_by_exact_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digest = record_validation_profile(
                root, run_id="strict-restart", selected_validation_tier="FULL",
                validation_profile_version="1.0", required_validation_controls=("required_suite",),
                profile_reference="validation-profile-registry:FULL@1.0",
                profile_selection_source="diff_classification", control_bindings=(BINDING,),
                candidate_sha=CANDIDATE, currentness=1,
                recorded_at="2026-09-10T10:00:00+00:00",
            )
            record_validation_command_invocation(
                root, run_id="strict-restart", validation_id="required_suite",
                command_id="required-suite-1", category="repository",
                control_identity="canonical required suite", required_for_profile=True,
                started_at="2026-09-10T10:00:00+00:00", currentness=1,
            )
            record_validation_command_terminal(
                root, run_id="strict-restart", command_id="required-suite-1",
                completed_at="2026-09-10T10:00:01+00:00", exit_code=0,
            )
            recovered = load_validation_context(root, "strict-restart", currentness=1)
            self.assertEqual(recovered["profile_digest"], digest)
            self.assertTrue(strict_required_controls_pass(
                recovered, candidate_sha=CANDIDATE, currentness=1,
            ))

            with self.assertRaisesRegex(EngineeringStorageError, "different immutable identity"):
                record_validation_profile(
                    root, run_id="strict-restart", selected_validation_tier="FULL",
                    validation_profile_version="1.0", required_validation_controls=("required_suite",),
                    profile_reference="validation-profile-registry:FULL@1.0",
                    profile_selection_source="diff_classification", control_bindings=(BINDING,),
                    candidate_sha="b" * 40, currentness=1,
                    recorded_at="2026-09-10T10:01:00+00:00",
                )
            with open_storage(root) as connection, self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE execution_validation_profile_identities SET candidate_sha=? WHERE run_id=?",
                    ("b" * 40, "strict-restart"),
                )

    def test_quality_and_security_passes_cannot_compensate_for_missing_validation_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("bounded implementation", encoding="utf-8")
            digest = record_validation_profile(
                root, run_id="qs-cannot-override", selected_validation_tier="FULL",
                validation_profile_version="1.0", required_validation_controls=("required_suite",),
                profile_reference="validation-profile-registry:FULL@1.0",
                profile_selection_source="diff_classification", control_bindings=(BINDING,),
                candidate_sha=CANDIDATE, currentness=1,
                recorded_at="2026-09-10T10:00:00+00:00",
            )
            assurance_digest = "sha256:" + "c" * 64
            profile = {
                "version": "validation-profile@1.0", "digest": assurance_digest,
                "candidate_sha": CANDIDATE, "criteria_digest": "sha256:" + "d" * 64,
                "validation_profile_digest": digest,
            }
            reviews = tuple({
                "reviewer": role, "status": "PASS", "candidate_sha": CANDIDATE,
                "profile_digest": assurance_digest, "invocation_id": f"{role}-1",
                "findings": [], "contract_version": "1.0",
                "started_at": "2026-09-10T10:00:02+00:00",
                "completed_at": "2026-09-10T10:00:03+00:00",
            } for role in ("quality", "security"))
            audit = ({
                "iteration": "1", "observed_at": "2026-09-10T10:00:01+00:00",
                "failed_checks": "No failed controls.", "proposed_action": "FULL profile.",
                "agent_summary": "Provider claimed completion.", "commit_sha": "not_recorded",
                "outcome": "validated",
            },)
            state = TransactionState(
                "qs-cannot-override", "pcvantol/djconnect", str(prompt), "QUALITY_CONTROL_AGENT",
                branch="codex/strict", owner_authorized=True, repair_iterations=1,
                local_validation_iterations=1, local_validation_audit=audit,
                assurance_profile=profile, assurance_reviews=reviews,
            )

            class Repository:
                @staticmethod
                def inspect(_: Path) -> RepositoryEvidence:
                    return RepositoryEvidence("pcvantol/djconnect", "codex/strict", CANDIDATE, True)

            class Agent:
                called = False

                @staticmethod
                def available() -> bool:
                    return True

                @staticmethod
                def version() -> str:
                    return "0.146.0"

                def invoke(self, _: Path, __: str) -> AgentResult:
                    self.called = True
                    return AgentResult("COMPLETE", "codex/strict", 71, commit_sha=CANDIDATE)

            agent = Agent()
            runner = EngineeringRunner(
                root, StateStore(root / ".engineering" / "engineering-runs"),
                Repository(), None, agent, lambda _: None,
            )
            blocked, _ = runner._publish_first_implementation_pull_request(
                state, AgentResult("COMPLETE", "codex/strict", commit_sha=CANDIDATE),
            )
            self.assertTrue(blocked.terminal)
            self.assertEqual(blocked.next_action, "implementation_publication_assurance_required")
            self.assertFalse(agent.called)

    def test_candidate_bound_profile_and_validated_audit_make_current_validation_eligible(self) -> None:
        context = _context()
        digest = context["profile_digest"]
        state = TransactionState(
            "eligible", "pcvantol/djconnect", "prompt.md", "QUALITY_CONTROL_AGENT",
            repair_iterations=1,
            local_validation_audit=({"outcome": "validated"},),
            assurance_profile={
                "version": "validation-profile@1.0", "digest": "sha256:" + "c" * 64,
                "candidate_sha": CANDIDATE, "criteria_digest": "sha256:" + "d" * 64,
                "validation_profile_digest": digest,
            },
        )
        self.assertTrue(_has_current_local_validation_evidence(state, context))

    def test_host_adapters_fail_closed_and_classify_exact_profile_launchers(self) -> None:
        context = _context()
        state = TransactionState(
            "adapter-fail-closed", "pcvantol/djconnect", "prompt.md", "QUALITY_CONTROL_AGENT",
            repair_iterations=1,
            local_validation_audit=({"outcome": "validated"},),
            assurance_profile={
                "version": "validation-profile@1.0", "digest": "sha256:" + "c" * 64,
                "candidate_sha": CANDIDATE, "criteria_digest": "sha256:" + "d" * 64,
                "validation_profile_digest": context["profile_digest"],
            },
        )
        self.assertIsNone(_validation_profile_digest({
            "profile_digest": "invalid", "candidate_sha": CANDIDATE, "currentness": 1,
        }))
        self.assertFalse(_required_validation_controls_pass(state, None))
        missing_candidate = deepcopy(context)
        missing_candidate["candidate_sha"] = None
        self.assertFalse(_required_validation_controls_pass(
            replace(state, assurance_profile=None), missing_candidate,
        ))
        self.assertFalse(_has_current_local_validation_evidence(
            replace(state, local_validation_audit=()), context,
        ))
        self.assertFalse(_has_current_local_validation_evidence(
            replace(state, local_validation_audit=({"outcome": "failed"},)), context,
        ))
        self.assertFalse(_has_current_local_validation_evidence(
            replace(state, assurance_profile=None), context,
        ))
        self.assertEqual(
            EngineeringRunner._validation_kind("python3 tools/qualification/console_route_ownership_guard.py"),
            "console_route_ownership",
        )
        self.assertEqual(
            EngineeringRunner._validation_kind("npm run test:ui-localization"),
            "ui_localization",
        )
        self.assertEqual(
            EngineeringRunner._validation_id_for_profile("python3 -m unittest discover", "tests", "FULL"),
            "repository_suite",
        )

    def test_publication_adapters_block_missing_assurance_and_repository_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("bounded implementation", encoding="utf-8")
            profile = {
                "version": "validation-profile@1.0", "digest": "sha256:" + "c" * 64,
                "candidate_sha": CANDIDATE, "criteria_digest": "sha256:" + "d" * 64,
                "validation_profile_digest": "sha256:" + "e" * 64,
            }
            state = TransactionState(
                "publication-adapters", "pcvantol/djconnect", str(prompt), "QUALITY_CONTROL_AGENT",
                branch="codex/strict", owner_authorized=True, repair_iterations=1,
                assurance_profile=profile,
            )

            class MissingRepository:
                @staticmethod
                def inspect(_: Path) -> RepositoryEvidence:
                    raise RunnerError("candidate unavailable")

            runner = EngineeringRunner(
                root, StateStore(root / ".engineering" / "engineering-runs"),
                MissingRepository(), None, object(), lambda _: None,
            )
            implementation = AgentResult("COMPLETE", "codex/strict", commit_sha=CANDIDATE)
            with patch.object(runner, "_current_local_validation_passes", return_value=True):
                missing_reviews, _ = runner._publish_first_implementation_pull_request(state, implementation)
            self.assertEqual(missing_reviews.next_action, "implementation_publication_assurance_required")

            reviews = tuple({
                "reviewer": role, "status": "PASS", "candidate_sha": CANDIDATE,
                "profile_digest": profile["digest"], "invocation_id": f"{role}-1",
                "findings": [], "contract_version": "1.0",
                "started_at": "2026-09-10T10:00:02+00:00",
                "completed_at": "2026-09-10T10:00:03+00:00",
            } for role in ("quality", "security"))
            with patch.object(runner, "_current_local_validation_passes", return_value=True):
                missing_candidate, _ = runner._publish_first_implementation_pull_request(
                    replace(state, assurance_reviews=reviews), implementation,
                )
            self.assertEqual(
                missing_candidate.next_action, "implementation_publication_candidate_unavailable",
            )

            with patch(
                "engineering_platform.execution_host.load_validation_context",
                side_effect=EngineeringStorageError("corrupt profile"),
            ):
                self.assertIsNone(runner._validation_context(state))


if __name__ == "__main__":
    unittest.main()
