from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform.release_operation import ReleaseOperation, ReleaseOperationError, ReleaseOperationStore


def operation(identifier: str = "release-0001") -> ReleaseOperation:
    return ReleaseOperation.create(
        operation_id=identifier, product="engineering-platform", component="server", version="2.3.1",
        policy_revision="release-lifecycle-v1", source_revision="a" * 40,
        artifacts={"wheel": "sha256:" + "b" * 64, "sdist": "sha256:" + "c" * 64},
    )


class ReleaseOperationTests(unittest.TestCase):
    def test_lost_answer_resumes_same_operation_and_keeps_published_separate_from_complete(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            prepared = store.save(operation())
            qualified = store.replace(prepared, prepared.transition("QUALIFIED", evidence={"exact_main_sha": "a" * 40}))
            published = store.replace(qualified, qualified.transition("PUBLISHED", evidence={"registry": "pypi", "wheel": qualified.artifacts["wheel"]}))
            store.record_publication(published)
            self.assertEqual(store.load("release-0001"), published)
            completed = store.replace(published, published.transition("RELEASE_COMPLETE", evidence={"temporary_paths": []}))
            store.record_publication(completed)
            self.assertEqual(completed.state, "RELEASE_COMPLETE")

    def test_prepublication_prepare_binds_exact_artifacts_before_registry_side_effects(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            evidence = {"exact_main_sha": "a" * 40, "artifact_digests": dict(operation().artifacts)}
            qualified = store.prepare_qualified(operation(), evidence=evidence)
            self.assertEqual(qualified.state, "QUALIFIED")
            self.assertEqual(store.prepare_qualified(operation(), evidence=evidence), qualified)
            changed = ReleaseOperation.create(
                operation_id="release-0001", product="engineering-platform", component="server",
                version="2.3.1", policy_revision="release-lifecycle-v1", source_revision="a" * 40,
                artifacts={"wheel": "sha256:" + "d" * 64, "sdist": "sha256:" + "c" * 64},
            )
            with self.assertRaisesRegex(ReleaseOperationError, "different immutable identity"):
                store.prepare_qualified(changed, evidence=evidence)
            with self.assertRaisesRegex(ReleaseOperationError, "qualification evidence changed"):
                store.prepare_qualified(operation(), evidence={"exact_main_sha": "b" * 40})

    def test_same_release_identity_with_different_bytes_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            first = operation().transition("QUALIFIED", evidence={"check": "PASS"}).transition("PUBLISHED", evidence={"receipt": "one"})
            store.record_publication(first)
            other = operation("release-0002").transition("QUALIFIED", evidence={"check": "PASS"})
            other = ReleaseOperation(**{**other.__dict__, "artifacts": {"wheel": "sha256:" + "d" * 64, "sdist": "sha256:" + "c" * 64}})
            other = other.transition("PUBLISHED", evidence={"receipt": "two"})
            with self.assertRaisesRegex(ReleaseOperationError, "different bytes"):
                store.record_publication(other)

    def test_only_one_release_operation_can_hold_the_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            contender = ReleaseOperationStore(Path(temporary))
            store.acquire("release-0001")
            with self.assertRaisesRegex(ReleaseOperationError, "another release operation"):
                contender.acquire("release-0002")
            with self.assertRaisesRegex(ReleaseOperationError, "does not own"):
                contender.release("release-0002")
            store.release("release-0001")
            contender.acquire("release-0002")
            contender.release("release-0002")

    def test_bad_operation_records_and_illegal_transitions_are_rejected(self) -> None:
        prepared = operation()
        with self.assertRaisesRegex(ReleaseOperationError, "not permitted"):
            prepared.transition("PUBLISHED", evidence={"receipt": "not qualified"})
        with self.assertRaisesRegex(ReleaseOperationError, "exact wheel and sdist"):
            ReleaseOperation.create(operation_id="release-0003", product="engineering-platform", component="server", version="2.3.1", policy_revision="v1", source_revision="a" * 40, artifacts={"wheel": "sha256:" + "b" * 64})

    def test_recovery_guards_and_artifact_hashing_cover_tampering(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            prepared = store.save(operation())
            with self.assertRaisesRegex(ReleaseOperationError, "immutable"):
                store.save(prepared.transition("QUALIFIED", evidence={"pass": True}))
            with self.assertRaisesRegex(ReleaseOperationError, "changed before"):
                store.replace(operation("release-0002"), prepared)
            store._path("release-0001").write_text("{}")
            with self.assertRaisesRegex(ReleaseOperationError, "unknown or missing"):
                store.load("release-0001")
            artifact = Path(temporary) / "artifact.whl"; artifact.write_bytes(b"exact bytes")
            self.assertTrue(store.artifact_digest(artifact).startswith("sha256:"))
            with self.assertRaisesRegex(ReleaseOperationError, "unavailable"):
                store.artifact_digest(Path(temporary) / "missing")
