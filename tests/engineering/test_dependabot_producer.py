from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engineering_platform import dependabot_producer, external_producer_binding, server


class _Provider:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def github(self, *_args: object) -> str:
        return json.dumps(self.payload)


class DependabotProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "server"
        server.initialize(self.root)
        self.database = self.root / server.SERVER_DATABASE_FILENAME
        with sqlite3.connect(self.database) as connection:
            for project_id, repository_id in (("project-a", "repository-a"), ("project-b", "repository-b")):
                connection.execute(
                    "INSERT INTO ep_project_registrations(project_id,attachment_contract,status,created_at,updated_at) VALUES(?, '{}', 'ACTIVE', 'now', 'now')",
                    (project_id,),
                )
                connection.execute(
                    """INSERT INTO ep_repository_registrations(
                           repository_id,project_id,authority_repository_id,role,attachment_contract,created_at,updated_at
                       ) VALUES(?,?,?, 'authority', '{}', 'now', 'now')""",
                    (repository_id, project_id, repository_id),
                )
            for identity, project_id, repository_id in (
                ("pcvantol/repository-a", "project-a", "repository-a"),
                ("pcvantol/repository-b", "project-b", "repository-b"),
            ):
                external_producer_binding.register(
                    connection,
                    data_root=self.root,
                    producer_type=external_producer_binding.DEPENDABOT,
                    external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                    external_resource_identity=identity,
                    project_id=project_id,
                    repository_id=repository_id,
                    reason="installed producer qualification",
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _pull(number: int, sha: str = "a" * 40) -> dependabot_producer.DependabotPullRequest:
        return dependabot_producer.DependabotPullRequest(
            number=number,
            title="Bump dependency",
            url=f"https://github.com/pcvantol/repository-a/pull/{number}",
            head_branch="dependabot/pip/example",
            head_sha=sha,
        )

    def test_two_explicit_bindings_admit_only_to_their_bound_project_repository(self) -> None:
        with sqlite3.connect(self.database) as connection:
            first = dependabot_producer.admit(
                connection,
                external_repository="pcvantol/repository-a",
                pull_request=self._pull(17),
            )
            second_pull = dependabot_producer.DependabotPullRequest(
                number=18,
                title="Bump dependency",
                url="https://github.com/pcvantol/repository-b/pull/18",
                head_branch="dependabot/npm/example",
                head_sha="b" * 40,
            )
            second = dependabot_producer.admit(
                connection,
                external_repository="pcvantol/repository-b",
                pull_request=second_pull,
            )
            self.assertEqual((first.project_id, first.repository_id), ("project-a", "repository-a"))
            self.assertEqual((second.project_id, second.repository_id), ("project-b", "repository-b"))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ep_submissions WHERE transport='DEPENDABOT'").fetchone()[0],
                2,
            )

    def test_replay_is_idempotent_and_unknown_external_identity_fails_closed(self) -> None:
        with sqlite3.connect(self.database) as connection:
            first = dependabot_producer.admit(
                connection,
                external_repository="pcvantol/repository-a",
                pull_request=self._pull(17),
            )
            replay = dependabot_producer.admit(
                connection,
                external_repository="pcvantol/repository-a",
                pull_request=self._pull(17),
            )
            self.assertEqual(replay.submission_id, first.submission_id)
            self.assertTrue(replay.duplicate)
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "BINDING_NOT_FOUND"):
                dependabot_producer.admit(
                    connection,
                    external_repository="pcvantol/unbound",
                    pull_request=self._pull(19),
                )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone()[0], 1)

    def test_discovery_requires_actual_dependabot_source_and_canonical_pull_url(self) -> None:
        accepted = {
            "number": 7,
            "title": "Bump example",
            "html_url": "https://github.com/pcvantol/repository-a/pull/7",
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/pip/example", "sha": "a" * 40},
        }
        forged = {**accepted, "number": 8, "html_url": "https://example.invalid/pull/8"}
        human = {**accepted, "number": 9, "user": {"login": "someone"}, "html_url": "https://github.com/pcvantol/repository-a/pull/9"}
        discovered = dependabot_producer.discover_open_pull_requests(
            "pcvantol/repository-a", _Provider([accepted, forged, human]),
        )
        self.assertEqual([item.number for item in discovered], [7])
