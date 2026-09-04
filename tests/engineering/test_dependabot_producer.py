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
                    pull_request=dependabot_producer.DependabotPullRequest(
                        19, "Bump", "https://github.com/pcvantol/unbound/pull/19", "dependabot/pip/a", "a" * 40,
                    ),
                )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone()[0], 1)

    def test_inactive_binding_project_and_forged_source_fail_before_admission(self) -> None:
        with sqlite3.connect(self.database) as connection:
            binding = external_producer_binding.resolve(
                connection,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                external_resource_identity="pcvantol/repository-a",
            )
            external_producer_binding.deactivate(
                connection,
                data_root=self.root,
                binding_id=binding.binding_id,
                reason="negative canary",
            )
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "BINDING_NOT_FOUND"):
                dependabot_producer.admit(
                    connection,
                    external_repository="pcvantol/repository-a",
                    pull_request=self._pull(31),
                )
            connection.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='project-b'")
            with self.assertRaisesRegex(external_producer_binding.ProducerBindingError, "PROJECT_INACTIVE"):
                dependabot_producer.admit(
                    connection,
                    external_repository="pcvantol/repository-b",
                    pull_request=dependabot_producer.DependabotPullRequest(
                        32, "Bump", "https://github.com/pcvantol/repository-b/pull/32", "dependabot/pip/b", "b" * 40,
                    ),
                )
            with self.assertRaisesRegex(dependabot_producer.DependabotProducerError, "INVALID_SOURCE_METADATA"):
                dependabot_producer.admit(
                    connection,
                    external_repository="pcvantol/repository-a",
                    pull_request=dependabot_producer.DependabotPullRequest(
                        33, "Bump", "https://example.invalid/pull/33", "dependabot/pip/a", "a" * 40,
                    ),
                )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ep_submissions").fetchone()[0], 0)

    def test_binding_change_cannot_reinterpret_an_already_admitted_pr_head(self) -> None:
        with sqlite3.connect(self.database) as connection:
            dependabot_producer.admit(
                connection,
                external_repository="pcvantol/repository-a",
                pull_request=self._pull(47),
            )
            original = external_producer_binding.resolve(
                connection,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                external_resource_identity="pcvantol/repository-a",
            )
            external_producer_binding.deactivate(
                connection,
                data_root=self.root,
                binding_id=original.binding_id,
                reason="controlled binding drift test",
            )
            external_producer_binding.register(
                connection,
                data_root=self.root,
                producer_type=external_producer_binding.DEPENDABOT,
                external_resource_type=external_producer_binding.GITHUB_REPOSITORY,
                external_resource_identity="pcvantol/repository-a",
                project_id="project-b",
                repository_id="repository-b",
                reason="controlled binding drift test",
            )
            with self.assertRaisesRegex(dependabot_producer.DependabotProducerError, "BINDING_DRIFT_REQUIRES_NEW_HEAD"):
                dependabot_producer.admit(
                    connection,
                    external_repository="pcvantol/repository-a",
                    pull_request=self._pull(47),
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

    def test_server_owned_service_observes_active_bindings_without_a_local_queue(self) -> None:
        payload = [{
            "number": 23,
            "title": "Bump example",
            "html_url": "https://github.com/pcvantol/repository-a/pull/23",
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/pip/example", "sha": "c" * 40},
        }]
        service = dependabot_producer.DependabotService(self.root, provider=_Provider(payload))
        self.assertEqual(service.tick(), 1)
        heartbeat = dependabot_producer.read_heartbeat(self.root)
        # ``tick`` is intentionally usable before the thread is started; the
        # child loop owns heartbeat projection in normal Server composition.
        self.assertIsNone(heartbeat)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT project_id,repository_id,transport FROM ep_submissions").fetchone(),
                ("project-a", "repository-a", "DEPENDABOT"),
            )
