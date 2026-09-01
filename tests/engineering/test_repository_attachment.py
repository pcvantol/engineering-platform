"""Focused coverage for the declarative repository attachment contract."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from engineering_platform.repository_attachment import (
    CONFIG_RELATIVE_PATH,
    RepositoryAttachmentError,
    config_path,
    load_repository_attachment,
    parse_repository_attachment,
)
from engineering_platform.resources import package_text


FIXTURES = Path(__file__).parent / "fixtures" / "repository_attachment"


class RepositoryAttachmentTest(unittest.TestCase):
    def test_schema_is_packaged_and_declares_canonical_version(self) -> None:
        schema = json.loads(package_text("schemas/repository-attachment.schema.json"))
        self.assertEqual(schema["$id"], "https://engineering-platform.dev/schemas/repository-attachment/1.0")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.0")
        self.assertFalse(schema["additionalProperties"])

    def test_fixtures_cover_product_technologies_without_product_specific_contract_fields(self) -> None:
        expected = {
            "dotnet-authority.json", "swift-child.json", "python-authority.json",
            "node-authority.json", "embedded-authority.json", "static-authority.json",
            "djconnect-future-authority.json",
        }
        self.assertEqual({path.name for path in FIXTURES.glob("*.json")}, expected)
        for fixture in sorted(FIXTURES.glob("*.json")):
            attachment = parse_repository_attachment(json.loads(fixture.read_text(encoding="utf-8")))
            self.assertTrue(attachment.project_id)
            self.assertNotIn("DJConnect", json.dumps(attachment.agent_read_surface()))

    def test_single_repository_authority_is_its_execution_repository(self) -> None:
        payload = json.loads((FIXTURES / "dotnet-authority.json").read_text(encoding="utf-8"))
        attachment = parse_repository_attachment(payload)
        self.assertEqual(attachment.authority_repository_id, attachment.repository_id)
        self.assertEqual(attachment.repository_role, "authority")
        self.assertEqual(attachment.validation.entrypoint, "dotnet test")

    def test_child_repository_keeps_logical_topology_separate_from_host_details(self) -> None:
        payload = json.loads((FIXTURES / "swift-child.json").read_text(encoding="utf-8"))
        attachment = parse_repository_attachment(payload)
        surface = attachment.agent_read_surface()
        self.assertEqual(surface["project"]["authority_repository_id"], "acme-mobile-app")
        self.assertEqual(surface["repository"]["role"], "child")
        self.assertNotIn("path", json.dumps(surface).lower())
        self.assertNotIn("remote", json.dumps(surface).lower())

    def test_loads_only_the_canonical_repository_local_path(self) -> None:
        payload = (FIXTURES / "python-authority.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = config_path(root)
            self.assertEqual(target, root / CONFIG_RELATIVE_PATH)
            target.parent.mkdir()
            target.write_text(payload, encoding="utf-8")
            self.assertEqual(load_repository_attachment(root).project_id, "acme-data")

    def test_invalid_configurations_fail_closed(self) -> None:
        valid = json.loads((FIXTURES / "python-authority.json").read_text(encoding="utf-8"))
        cases = []
        cases.append({**valid, "unknown": True})
        mismatch = json.loads(json.dumps(valid))
        mismatch["repository"]["role"] = "child"
        cases.append(mismatch)
        unsafe = json.loads(json.dumps(valid))
        unsafe["validation"]["entrypoint"] = "pytest\nrm -rf /"
        cases.append(unsafe)
        no_validation = json.loads(json.dumps(valid))
        no_validation["validation"] = {"kind": "none", "entrypoint": "ignored"}
        cases.append(no_validation)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(RepositoryAttachmentError):
                    parse_repository_attachment(payload)

    def test_missing_file_fails_closed_without_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RepositoryAttachmentError, "repository attachment is missing"):
                load_repository_attachment(Path(temporary))
