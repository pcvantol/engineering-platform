from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engineering_platform import external_producer_binding, server


class ExternalProducerBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "server"
        server.initialize(self.root)
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        with sqlite3.connect(self.database) as connection:
            self._topology(connection, "project-a", "repository-a")
            self._topology(connection, "project-b", "repository-b")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _topology(connection: sqlite3.Connection, project_id: str, repository_id: str) -> None:
        connection.execute(
            """INSERT INTO ep_project_registrations(
                   project_id,attachment_contract,status,created_at,updated_at
               ) VALUES(?, '{}', 'ACTIVE', 'now', 'now')""",
            (project_id,),
        )
        connection.execute(
            """INSERT INTO ep_repository_registrations(
                   repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at
               ) VALUES(?,?,?, 'authority', '{}', 'now', 'now')""",
            (repository_id, project_id, repository_id),
        )

    def test_registration_normalizes_and_resolves_an_explicit_github_binding(self) -> None:
        with sqlite3.connect(self.database) as connection:
            binding = external_producer_binding.register(
                connection,
                data_root=self.root,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                external_resource_identity="https://github.com/PCVantol/Engineering-Platform.git/",
                project_id="project-a",
                repository_id="repository-a",
                reason="Dependabot migration qualification",
            )
            resolved = external_producer_binding.resolve(
                connection,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                external_resource_identity="git@github.com:pcvantol/engineering-platform.git",
            )
            self.assertEqual(resolved, binding)
            self.assertEqual(
                connection.execute("SELECT action,reason FROM ep_external_producer_binding_audit").fetchone(),
                ("REGISTER", "Dependabot migration qualification"),
            )

    def test_duplicate_and_cross_project_binding_fail_closed(self) -> None:
        with sqlite3.connect(self.database) as connection:
            args = {
                "data_root": self.root,
                "producer_type": external_producer_binding.DEPENDABOT,
                "external_resource_type": external_producer_binding.GITHUB_REPOSITORY,
                "external_resource_identity": "pcvantol/engineering-platform",
                "project_id": "project-a",
                "repository_id": "repository-a",
                "reason": "initial registration",
            }
            external_producer_binding.register(connection, **args)
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "BINDING_CONFLICT"):
                external_producer_binding.register(connection, **args)
            args["repository_id"] = "repository-b"
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "REPOSITORY_NOT_AUTHORIZED"):
                external_producer_binding.register(connection, **args)

    def test_deactivation_preserves_audit_and_removes_the_active_resolution(self) -> None:
        with sqlite3.connect(self.database) as connection:
            binding = external_producer_binding.register(
                connection,
                data_root=self.root,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                external_resource_identity="pcvantol/engineering-platform",
                project_id="project-a",
                repository_id="repository-a",
                reason="temporary binding",
            )
            retired = external_producer_binding.deactivate(
                connection,
                data_root=self.root,
                binding_id=binding.binding_id,
                reason="repository ownership changed",
            )
            self.assertEqual(retired.version, 2)
            self.assertEqual(external_producer_binding.list_bindings(connection, data_root=self.root)[0]["status"], "INACTIVE")
            self.assertEqual(
                connection.execute("SELECT action FROM ep_external_producer_binding_audit ORDER BY audit_id").fetchall(),
                [("REGISTER",), ("DEACTIVATE",)],
            )
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "BINDING_NOT_FOUND"):
                external_producer_binding.resolve(
                    connection,
                    producer_type=external_producer_binding.DEPENDABOT,
                    external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                    external_resource_identity="pcvantol/engineering-platform",
                )

    def test_registration_requires_the_installation_data_root_owner(self) -> None:
        with sqlite3.connect(self.database) as connection:
            with patch("engineering_platform.platform_admin.os.geteuid", return_value=-1):
                with self.assertRaisesRegex(PermissionError, "PLATFORM_ADMIN_FORBIDDEN"):
                    external_producer_binding.register(
                        connection,
                        data_root=self.root,
                        producer_type=external_producer_binding.DEPENDABOT,
                        external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                        external_resource_identity="pcvantol/engineering-platform",
                        project_id="project-a",
                        repository_id="repository-a",
                        reason="must not accept an arbitrary caller identity",
                    )

    def test_resolution_has_no_default_project_or_external_identity_fallback(self) -> None:
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "BINDING_NOT_FOUND"):
                external_producer_binding.resolve(
                    connection,
                    producer_type=external_producer_binding.DEPENDABOT,
                    external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                    external_resource_identity="pcvantol/unbound",
                )
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "UNSUPPORTED_PRODUCER_BINDING"):
                external_producer_binding.resolve(
                    connection,
                    producer_type="FILE_INBOX",
                    external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                    external_resource_identity="pcvantol/engineering-platform",
                )
