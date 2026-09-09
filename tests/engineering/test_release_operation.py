from __future__ import annotations

import json
import os
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
            operation_record = operation()
            store.acquire(operation_record.operation_id)
            try:
                prepared = store.save(operation_record)
                qualified = store.replace(prepared, prepared.transition("QUALIFIED", evidence={"exact_main_sha": "a" * 40}))
                published = store.replace(qualified, qualified.transition("PUBLISHED", evidence={"registry": "pypi", "wheel": qualified.artifacts["wheel"]}))
                store.record_publication(published)
                self.assertEqual(store.load("release-0001"), published)
                completed = store.replace(published, published.transition("RELEASE_COMPLETE", evidence={"temporary_paths": []}))
                store.record_publication(completed)
                self.assertEqual(completed.state, "RELEASE_COMPLETE")
            finally:
                store.release(operation_record.operation_id)

    def test_prepublication_prepare_binds_exact_artifacts_before_registry_side_effects(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            evidence = {"exact_main_sha": "a" * 40, "artifact_digests": dict(operation().artifacts)}
            operation_record = operation()
            store.acquire(operation_record.operation_id)
            try:
                qualified = store.prepare_qualified(operation_record, evidence=evidence)
                self.assertEqual(qualified.state, "QUALIFIED")
                self.assertEqual(store.prepare_qualified(operation_record, evidence=evidence), qualified)
                changed = ReleaseOperation.create(
                    operation_id="release-0001", product="engineering-platform", component="server",
                    version="2.3.1", policy_revision="release-lifecycle-v1", source_revision="a" * 40,
                    artifacts={"wheel": "sha256:" + "d" * 64, "sdist": "sha256:" + "c" * 64},
                )
                with self.assertRaisesRegex(ReleaseOperationError, "different immutable identity"):
                    store.prepare_qualified(changed, evidence=evidence)
                with self.assertRaisesRegex(ReleaseOperationError, "qualification evidence changed"):
                    store.prepare_qualified(operation_record, evidence={"exact_main_sha": "b" * 40})
            finally:
                store.release(operation_record.operation_id)

    def test_same_release_identity_with_different_bytes_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            first = operation().transition("QUALIFIED", evidence={"check": "PASS"}).transition("PUBLISHED", evidence={"receipt": "one"})
            store.acquire(first.operation_id)
            try:
                store.record_publication(first)
            finally:
                store.release(first.operation_id)
            other = operation("release-0002").transition("QUALIFIED", evidence={"check": "PASS"})
            other = ReleaseOperation(**{**other.__dict__, "artifacts": {"wheel": "sha256:" + "d" * 64, "sdist": "sha256:" + "c" * 64}})
            other = other.transition("PUBLISHED", evidence={"receipt": "two"})
            store.acquire(other.operation_id)
            try:
                with self.assertRaisesRegex(ReleaseOperationError, "different bytes"):
                    store.record_publication(other)
            finally:
                store.release(other.operation_id)

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
            operation_record = operation()
            store.acquire(operation_record.operation_id)
            try:
                prepared = store.save(operation_record)
                with self.assertRaisesRegex(ReleaseOperationError, "immutable"):
                    store.save(prepared.transition("QUALIFIED", evidence={"pass": True}))
                with self.assertRaisesRegex(ReleaseOperationError, "changed before"):
                    store.replace(prepared.transition("QUALIFIED", evidence={"pass": True}), prepared)
                with self.assertRaisesRegex(ReleaseOperationError, "release lock"):
                    store.replace(operation("release-0002"), prepared)
                store._path("release-0001").write_text("{}")
                with self.assertRaisesRegex(ReleaseOperationError, "unknown or missing"):
                    store.load("release-0001")
            finally:
                store.release(operation_record.operation_id)
            artifact = Path(temporary) / "artifact.whl"; artifact.write_bytes(b"exact bytes")
            self.assertTrue(store.artifact_digest(artifact).startswith("sha256:"))
            with self.assertRaisesRegex(ReleaseOperationError, "unavailable"):
                store.artifact_digest(Path(temporary) / "missing")

    def test_registry_receipt_cleanup_pending_and_terminal_resume_are_distinct(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            operation_record = operation()
            store.acquire(operation_record.operation_id)
            try:
                qualified = store.prepare_qualified(operation_record, evidence={"qualification": "PASS"})
                published = store.mark_published(
                    operation_record,
                    evidence={"registry": "pypi", "readback": "PASS", "artifacts": dict(operation_record.artifacts)},
                )
                self.assertEqual((qualified.state, published.state), ("QUALIFIED", "PUBLISHED"))
                pending = store.mark_cleanup_pending(
                    operation_record,
                    evidence={"result": "CLEANUP_PENDING", "failed_targets": ["readback"]},
                )
                self.assertEqual(pending.state, "CLEANUP_PENDING")
                completed = store.complete(
                    operation_record,
                    evidence={"result": "COMPLETE", "temporary_paths": ["readback"]},
                )
                self.assertEqual(completed.state, "RELEASE_COMPLETE")
                self.assertEqual(
                    completed,
                    store.complete(
                        operation_record,
                        evidence={"result": "COMPLETE", "temporary_paths": ["readback"]},
                    ),
                )
            finally:
                store.release(operation_record.operation_id)

    def test_changed_published_receipt_and_nonfinite_evidence_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            operation_record = operation()
            store.acquire(operation_record.operation_id)
            try:
                store.prepare_qualified(operation_record, evidence={"qualification": "PASS"})
                store.mark_published(operation_record, evidence={"registry": "pypi", "readback": "PASS"})
                with self.assertRaisesRegex(ReleaseOperationError, "receipt changed"):
                    store.mark_published(operation_record, evidence={"registry": "pypi", "readback": "DIFFERENT"})
            finally:
                store.release(operation_record.operation_id)
        with self.assertRaisesRegex(ReleaseOperationError, "durable evidence"):
            operation().transition("QUALIFIED", evidence={"value": float("nan")})

    def test_digest_identity_normalizes_symlinks_and_paths_with_spaces(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            spaced = root / "release inputs"
            spaced.mkdir()
            artifact = spaced / "platform wheel.whl"
            artifact.write_bytes(b"immutable EP artifact")
            linked = root / "platform wheel link.whl"
            os.symlink(artifact, linked)
            store = ReleaseOperationStore(root / "release evidence")
            self.assertEqual(store.artifact_digest(artifact), store.artifact_digest(linked))

    def test_persisted_unknown_or_incomplete_terminal_record_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            store = ReleaseOperationStore(Path(temporary))
            operation_record = operation()
            store.acquire(operation_record.operation_id)
            try:
                qualified = store.prepare_qualified(operation_record, evidence={"qualification": "PASS"})
                published = store.mark_published(operation_record, evidence={"registry": "pypi", "readback": "PASS"})
                payload = json.loads((store._path(operation_record.operation_id)).read_text(encoding="utf-8"))
                payload["unexpected"] = True
                store._path(operation_record.operation_id).write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ReleaseOperationError, "unknown or missing"):
                    store.load(operation_record.operation_id)
                for state in ("CLEANUP_PENDING", "RELEASE_COMPLETE"):
                    store._path(operation_record.operation_id).write_text(json.dumps({
                        **published.__dict__, "state": state, "cleanup": None,
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(ReleaseOperationError, "missing cleanup"):
                        store.load(operation_record.operation_id)
            finally:
                store.release(operation_record.operation_id)

    def test_terminal_recovery_uses_already_recorded_identity_after_artifact_removal(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel, sdist = root / "wheel.whl", root / "source.tar.gz"
            wheel.write_bytes(b"wheel")
            sdist.write_bytes(b"sdist")
            operation_record = ReleaseOperation.create(
                operation_id="release-0001", product="engineering-platform", component="server", version="2.3.1",
                policy_revision="release-lifecycle-v1", source_revision="a" * 40,
                artifacts={
                    "wheel": ReleaseOperationStore.artifact_digest(wheel),
                    "sdist": ReleaseOperationStore.artifact_digest(sdist),
                },
            )
            store = ReleaseOperationStore(root / "evidence")
            store.acquire(operation_record.operation_id)
            try:
                store.prepare_qualified(operation_record, evidence={"qualification": "PASS"})
                published = store.mark_published(operation_record, evidence={"registry": "pypi", "readback": "PASS"})
                wheel.unlink()
                sdist.unlink()
                self.assertEqual(
                    "RELEASE_COMPLETE",
                    store.complete(
                        published,
                        evidence={"result": "COMPLETE", "temporary_paths": ["downloaded-artifacts"]},
                    ).state,
                )
            finally:
                store.release(operation_record.operation_id)
