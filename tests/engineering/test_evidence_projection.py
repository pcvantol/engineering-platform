from __future__ import annotations

import unittest

from tools.engineering.evidence_projection import (
    FAILED_DIAGNOSTIC_LIMIT,
    PASSING_TEST_LIMIT,
    deterministic_fixture,
    project_output,
)


class EvidenceProjectionTests(unittest.TestCase):
    def test_small_output_passes_through_unchanged(self) -> None:
        projected = project_output(("git", "status"), "## main\n", 0)
        self.assertEqual(projected.text, "## main\n")
        self.assertFalse(projected.more_evidence_available)

    def test_large_passing_test_output_is_bounded(self) -> None:
        raw = "line\n" * 500
        projected = project_output(("pytest",), raw, 0)
        self.assertLessEqual(projected.projected_bytes, PASSING_TEST_LIMIT * 2)
        self.assertIn("PASSING_TEST_OUTPUT_BOUNDED", projected.text)
        self.assertTrue(projected.more_evidence_available)

    def test_failed_test_retains_actionable_diagnostics(self) -> None:
        raw = "FAILED test_name\nAssertionError: expected\n" + "trace\n" * 4000
        projected = project_output(("pytest",), raw, 1)
        self.assertIn("FAILED test_name", projected.text)
        self.assertIn("AssertionError", projected.text)
        self.assertLessEqual(projected.projected_bytes, FAILED_DIAGNOSTIC_LIMIT)

    def test_search_is_bounded_and_explicitly_expandable(self) -> None:
        projected = project_output(("rg", "needle"), "\n".join(str(item) for item in range(80)), 0)
        self.assertIn("MATCHES_BOUNDED", projected.text)
        self.assertIn("MORE_EVIDENCE_AVAILABLE", projected.text)
        self.assertTrue(projected.more_evidence_available)

    def test_broad_git_and_github_outputs_become_bounded_facts(self) -> None:
        git = project_output(("git", "log"), "commit\n" * 1000, 0)
        github = project_output(("gh", "pr", "view"), "item\n" * 2000, 0)
        self.assertIn("GIT_EVIDENCE_BOUNDED", git.text)
        self.assertIn("GITHUB_EVIDENCE_BOUNDED", github.text)

    def test_unknown_or_exact_source_output_is_not_silently_summarized(self) -> None:
        raw = "exact source\n" * 1000
        projected = project_output(("sed", "-n", "1,100p", "source.py"), raw, 0)
        self.assertEqual(projected.text, raw)
        self.assertFalse(projected.more_evidence_available)

    def test_deterministic_fixture_reduces_projected_bytes_without_losing_evidence(self) -> None:
        fixture = deterministic_fixture()
        self.assertGreaterEqual(fixture["reduction"], 0.40)
        self.assertTrue(fixture["required_evidence_retained"])
        self.assertTrue(fixture["more_evidence_available"])

    def test_projection_is_ephemeral_and_never_exposes_raw_output_as_metadata(self) -> None:
        projected = project_output(("rg", "needle"), "match\n" * 200, 0)
        self.assertNotIn("match\n" * 200, repr(projected))
        self.assertEqual(set(projected.__dict__), {"category", "text", "raw_bytes", "projected_bytes", "more_evidence_available"})
