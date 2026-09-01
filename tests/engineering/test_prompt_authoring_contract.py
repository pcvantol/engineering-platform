"""Repository-truth tests for the canonical EP prompt authoring foundation."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]


class PromptAuthoringContractTest(unittest.TestCase):
    """Keep the authoring artifacts versioned, neutral and non-enforcing."""

    def test_contract_and_template_are_canonical_and_producer_neutral(self) -> None:
        contract = (ROOT / "docs/engineering/EP_PROMPT_AUTHORING_CONTRACT.md").read_text(encoding="utf-8")
        template = (ROOT / "docs/engineering/EP_PROMPT_TEMPLATE.md").read_text(encoding="utf-8")

        self.assertIn("Contract ID:** `EP_PROMPT_AUTHORING_CONTRACT`", contract)
        self.assertIn("Prompt Authoring Contract Version:** 1", contract)
        self.assertIn("EP Prompt Authoring Contract: 1", template)
        self.assertIn("Template Version: 1", template)
        self.assertIn("Execution Mode: <Managed | Genesis>", template)
        self.assertIn("**Managed**", contract)
        self.assertIn("**Genesis**", contract)
        self.assertIn("Repository Truth is\nauthoritative", contract)
        self.assertIn("Arbitrary GPT Authoring Instruction", contract)
        self.assertIn("Forge is optional", contract)
        self.assertIn("Existing manually authored", contract)
        self.assertIn("does not parse headings", contract)
        self.assertIn("Project Workspace", contract)
        self.assertNotIn("Forge", template)
        self.assertNotIn("DJConnect", template)
        self.assertNotIn("/Users/", template)
        for section in (
            "# Context", "# Objective", "# Architecture / Ownership Boundaries",
            "# Required Behavior", "# Compatibility / Invariants", "# Tests",
            "# Validation", "# Explicitly Out of Scope", "# Final Report",
        ):
            self.assertIn(section, template)

    def test_operations_console_has_no_prompt_template_export(self) -> None:
        dashboard = (ROOT / "src/engineering_platform/dashboard.py").read_text(encoding="utf-8")
        locales = (ROOT / "src/engineering_platform/assets/dashboard_locales.mjs").read_text(encoding="utf-8")
        stylesheet = (ROOT / "src/engineering_platform/assets/dashboard.css").read_text(encoding="utf-8")

        self.assertNotIn("/api/prompt-template", dashboard)
        self.assertNotIn("downloadPromptTemplate", dashboard)
        self.assertNotIn("prompt_authoring.", locales)
        self.assertNotIn("prompt-authoring__download", stylesheet)
