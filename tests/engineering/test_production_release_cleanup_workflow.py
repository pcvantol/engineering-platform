from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from engineering_platform.release_operation import ReleaseOperation, ReleaseOperationStore


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ep-server-production-release.yml"
SOURCE_SHA = "a" * 40
VERSION = "2.3.3"
OPERATION_ID = f"ep-release-{VERSION}-{SOURCE_SHA}"


def _cleanup_script() -> str:
    """Extract the actual cleanup step rather than testing a copied script."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.index("      - name: Clean exact operation-local paths before RELEASE_COMPLETE\n")
    run = workflow.index("        run: |\n", step) + len("        run: |\n")
    end = workflow.index("      - uses: actions/upload-artifact", run)
    return "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in workflow[run:end].splitlines()
    )


def _operation() -> ReleaseOperation:
    return ReleaseOperation.create(
        operation_id=OPERATION_ID,
        product="engineering-platform",
        component="server",
        version=VERSION,
        policy_revision="ep-server-production-release-v1",
        source_revision=SOURCE_SHA,
        artifacts={"wheel": "sha256:" + "b" * 64, "sdist": "sha256:" + "c" * 64},
    )


def _write_published_input(workspace: Path, *, dangling_dist: bool = False) -> bytes:
    store = ReleaseOperationStore(workspace / "published-input" / "release-operation")
    operation = _operation()
    qualification = {
        "exact_main_sha": SOURCE_SHA,
        "artifact_digests": dict(operation.artifacts),
        "qualification": "ep-production-wheel-api-security-browser-installed-v1",
    }
    publication = {
        "registry": "pypi",
        "readback": "PASS",
        "observed_artifact_digests": {
            "engineering_platform-2.3.3-py3-none-any.whl": operation.artifacts["wheel"],
            "engineering_platform-2.3.3.tar.gz": operation.artifacts["sdist"],
        },
    }
    store.acquire(operation.operation_id)
    try:
        store.prepare_qualified(operation, evidence=qualification)
        published = store.mark_published(operation, evidence=publication)
        assert published.state == "PUBLISHED"
    finally:
        store.release(operation.operation_id)
    dist = workspace / "published-input" / "dist"
    if dangling_dist:
        dist.symlink_to(workspace / "missing-dist", target_is_directory=True)
    else:
        dist.mkdir(parents=True)
        (dist / "exact-wheel.whl").write_bytes(b"wheel bytes")
    (workspace / "published-input" / "release-operation" / "registry-readback-digests.json").write_text(
        json.dumps(publication, sort_keys=True), encoding="utf-8"
    )
    return store._path(operation.operation_id).read_bytes()


def _cleanup_pending_bytes(published_bytes: bytes) -> bytes:
    """Make the canonical durable receipt an interrupted cleanup would retain."""
    published = ReleaseOperation.parse(json.loads(published_bytes))
    pending = published.transition(
        "CLEANUP_PENDING",
        evidence={
            "result": "CLEANUP_PENDING",
            "targets": [
                "published-readback",
                "published-input/dist",
                "published-input/release-operation/registry-readback-digests.json",
                "pending-readback",
            ],
            "failed_targets": ["published-input/dist"],
            "error": "failed to remove an operation-local temporary path",
        },
    )
    return (
        json.dumps(asdict(pending), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_fake_commands(directory: Path) -> None:
    gh = directory / "gh"
    gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import shutil
            import sys

            arguments = sys.argv[1:]
            if len(arguments) < 2 or arguments[0] != "release":
                raise SystemExit("fake gh accepts only release commands")
            action = arguments[1]
            values = arguments[2:]
            assets = Path(os.environ["FAKE_GH_ASSET_DIR"])
            log = Path(os.environ["FAKE_GH_LOG"])
            if action == "download":
                pattern = values[values.index("--pattern") + 1]
                if pattern == os.environ.get("FAKE_GH_UNDOWNLOADABLE_ASSET"):
                    raise SystemExit(1)
                source = assets / pattern
                if not source.is_file():
                    raise SystemExit(1)
                if "--output" in values:
                    output = values[values.index("--output") + 1]
                    if output != "-":
                        shutil.copy2(source, output)
                    else:
                        sys.stdout.buffer.write(source.read_bytes())
                else:
                    destination = Path(values[values.index("--dir") + 1])
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination / pattern)
                log.write_text(log.read_text(encoding="utf-8") + f"download {pattern}\\n", encoding="utf-8")
            elif action == "upload":
                source = Path(values[-1])
                shutil.copy2(source, assets / source.name)
                log.write_text(log.read_text(encoding="utf-8") + f"upload {source.name}\\n", encoding="utf-8")
            elif action == "view":
                fields = values[values.index("--json") + 1]
                if fields == "assets":
                    print("\\n".join(path.name for path in sorted(assets.iterdir()) if path.is_file()))
                elif fields == "isDraft":
                    print(Path(os.environ["FAKE_GH_DRAFT_FILE"]).read_text(encoding="utf-8").strip())
                else:
                    raise SystemExit(f"unexpected fake gh release view fields: {fields}")
                log.write_text(log.read_text(encoding="utf-8") + f"view {fields}\\n", encoding="utf-8")
            elif action == "edit":
                Path(os.environ["FAKE_GH_DRAFT_FILE"]).write_text("false\\n", encoding="utf-8")
                log.write_text(log.read_text(encoding="utf-8") + "edit draft\\n", encoding="utf-8")
            else:
                raise SystemExit(f"unexpected fake gh release action: {action}")
            """
        ),
        encoding="utf-8",
    )
    gh.chmod(0o700)
    rm = directory / "rm"
    rm.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import sys

            if os.environ.get("FAKE_RM_RETAIN_TARGET") and sys.argv[-1] == os.environ["FAKE_RM_RETAIN_TARGET"]:
                raise SystemExit(0)
            os.execv("/bin/rm", ["/bin/rm", *sys.argv[1:]])
            """
        ),
        encoding="utf-8",
    )
    rm.chmod(0o700)


class ProductionReleaseCleanupWorkflowTests(unittest.TestCase):
    def _prepare_workspace(self, root: Path, *, dangling_dist: bool = False) -> bytes:
        shutil.copytree(ROOT / "src", root / "src")
        (root / "runner-temp").mkdir()
        return _write_published_input(root, dangling_dist=dangling_dist)

    def _run_cleanup(
        self,
        root: Path,
        fake_commands: Path,
        assets: Path,
        log: Path,
        *,
        retain_target: str | None = "published-input/dist",
        undownloadable_asset: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        draft = root / "fake-release-draft"
        draft.write_text("true\n", encoding="utf-8")
        environment = {
            **os.environ,
            "PATH": str(fake_commands) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(root / "runner-temp"),
            "OPERATION_ID": OPERATION_ID,
            "VERSION": VERSION,
            "SOURCE_SHA": SOURCE_SHA,
            "FAKE_GH_ASSET_DIR": str(assets),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_DRAFT_FILE": str(draft),
            "FAKE_GH_UNDOWNLOADABLE_ASSET": undownloadable_asset or "",
            "FAKE_RM_RETAIN_TARGET": retain_target or "",
        }
        return subprocess.run(
            ["bash", "-c", _cleanup_script()],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cleanup_pending_hydration_is_canonical_and_atomic(self) -> None:
        cleanup = _cleanup_script()

        self.assertIn('PENDING_READBACK_DIR="$(mktemp -d "$RUNNER_TEMP/ep-pending-readback-XXXXXX")"', cleanup)
        self.assertIn('PENDING_ASSET_NAMES="$(gh release view "$TAG" --json assets --jq \'.assets[].name\')"', cleanup)
        self.assertIn('if printf \'%s\\n\' "$PENDING_ASSET_NAMES" | grep -Fqx -- "$PENDING"; then', cleanup)
        self.assertIn("pending_receipt_present=1", cleanup)
        self.assertIn("published_bytes != canonical_bytes(published)", cleanup)
        self.assertIn("pending_bytes != canonical_bytes(pending)", cleanup)
        self.assertIn("pending.qualification != published.qualification", cleanup)
        self.assertIn("pending.publication_receipt != published.publication_receipt", cleanup)
        self.assertIn("store.replace(current, pending)", cleanup)
        self.assertNotIn("Path(published_path).write_bytes", cleanup)
        self.assertIn("target_exists()", cleanup)
        self.assertIn("trap cleanup_pending_readback EXIT", cleanup)
        self.assertIn('test -e "$1" || test -L "$1"', cleanup)
        self.assertIn('"$PENDING_READBACK_DIR"', cleanup)
        self.assertIn('"pending-readback"', cleanup)
        self.assertIn('gh release download "$TAG" --pattern "$PENDING" --output - | cmp "$PENDING" -', cleanup)
        self.assertIn('gh release download "$TAG" --pattern "$RECEIPT" --output - | cmp "$RECEIPT" -', cleanup)
        self.assertNotIn("COMPLETE_READBACK_DIR", cleanup)
        self.assertNotIn("cleanup-complete.json", cleanup)

    def test_first_cleanup_failure_uploads_parseable_pending_and_retry_retains_it(self) -> None:
        """Run the real workflow shell step with only local fake GitHub state."""
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            assets = temporary_root / "assets"
            assets.mkdir()
            log = temporary_root / "fake-gh.log"
            log.write_text("", encoding="utf-8")
            fake_commands = temporary_root / "fake-bin"
            fake_commands.mkdir()
            _write_fake_commands(fake_commands)
            published_name = f"engineering-platform-release-published-{VERSION}-{SOURCE_SHA}.json"
            pending_name = f"engineering-platform-cleanup-pending-{VERSION}-{SOURCE_SHA}.json"

            first_root = temporary_root / "first"
            first_root.mkdir()
            published_bytes = self._prepare_workspace(first_root)
            (assets / published_name).write_bytes(published_bytes)
            first = self._run_cleanup(first_root, fake_commands, assets, log)

            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            pending_path = assets / pending_name
            self.assertTrue(pending_path.is_file())
            pending_bytes = pending_path.read_bytes()
            pending = ReleaseOperation.parse(json.loads(pending_bytes))
            self.assertEqual(pending.state, "CLEANUP_PENDING")
            self.assertEqual(pending.operation_id, OPERATION_ID)
            self.assertEqual(pending.cleanup["failed_targets"], ["published-input/dist"])
            self.assertIn("pending-readback", pending.cleanup["targets"])
            self.assertEqual(pending.qualification["exact_main_sha"], SOURCE_SHA)
            self.assertEqual(pending.publication_receipt["readback"], "PASS")

            second_root = temporary_root / "second"
            second_root.mkdir()
            self.assertEqual(self._prepare_workspace(second_root), published_bytes)
            second = self._run_cleanup(second_root, fake_commands, assets, log)

            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            self.assertEqual(pending_path.read_bytes(), pending_bytes)
            uploads = [
                line for line in log.read_text(encoding="utf-8").splitlines()
                if line == f"upload {pending_name}"
            ]
            self.assertEqual(uploads, [f"upload {pending_name}"])

    def test_dangling_cleanup_target_is_recorded_after_successful_rm_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            assets = temporary_root / "assets"
            assets.mkdir()
            log = temporary_root / "fake-gh.log"
            log.write_text("", encoding="utf-8")
            fake_commands = temporary_root / "fake-bin"
            fake_commands.mkdir()
            _write_fake_commands(fake_commands)
            root = temporary_root / "dangling"
            root.mkdir()
            published_bytes = self._prepare_workspace(root, dangling_dist=True)
            published_name = f"engineering-platform-release-published-{VERSION}-{SOURCE_SHA}.json"
            pending_name = f"engineering-platform-cleanup-pending-{VERSION}-{SOURCE_SHA}.json"
            (assets / published_name).write_bytes(published_bytes)

            result = self._run_cleanup(root, fake_commands, assets, log)

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            pending = ReleaseOperation.parse(json.loads((assets / pending_name).read_bytes()))
            self.assertEqual(pending.cleanup["failed_targets"], ["published-input/dist"])

    def test_listed_but_unreadable_pending_fails_before_cleanup(self) -> None:
        """A remote receipt is never silently treated as absent after a failed readback."""
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            assets = temporary_root / "assets"
            assets.mkdir()
            log = temporary_root / "fake-gh.log"
            log.write_text("", encoding="utf-8")
            fake_commands = temporary_root / "fake-bin"
            fake_commands.mkdir()
            _write_fake_commands(fake_commands)
            root = temporary_root / "unreadable-pending"
            root.mkdir()
            published_bytes = self._prepare_workspace(root)
            published_name = f"engineering-platform-release-published-{VERSION}-{SOURCE_SHA}.json"
            pending_name = f"engineering-platform-cleanup-pending-{VERSION}-{SOURCE_SHA}.json"
            (assets / published_name).write_bytes(published_bytes)
            pending_path = assets / pending_name
            pending_bytes = _cleanup_pending_bytes(published_bytes)
            pending_path.write_bytes(pending_bytes)

            result = self._run_cleanup(
                root,
                fake_commands,
                assets,
                log,
                undownloadable_asset=pending_name,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / "published-input" / "dist").is_dir())
            self.assertTrue((root / "published-readback").is_dir())
            self.assertEqual(list((root / "runner-temp").iterdir()), [])
            self.assertEqual(pending_path.read_bytes(), pending_bytes)
            self.assertNotIn(f"upload {pending_name}", log.read_text(encoding="utf-8"))

    def test_successful_cleanup_removes_pending_and_complete_readback_temporaries(self) -> None:
        """The successful real shell step leaves only the durable completion receipt."""
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            assets = temporary_root / "assets"
            assets.mkdir()
            log = temporary_root / "fake-gh.log"
            log.write_text("", encoding="utf-8")
            fake_commands = temporary_root / "fake-bin"
            fake_commands.mkdir()
            _write_fake_commands(fake_commands)
            root = temporary_root / "successful-cleanup"
            root.mkdir()
            published_bytes = self._prepare_workspace(root)
            published_name = f"engineering-platform-release-published-{VERSION}-{SOURCE_SHA}.json"
            pending_name = f"engineering-platform-cleanup-pending-{VERSION}-{SOURCE_SHA}.json"
            complete_name = f"engineering-platform-release-complete-{VERSION}-{SOURCE_SHA}.json"
            (assets / published_name).write_bytes(published_bytes)

            result = self._run_cleanup(
                root,
                fake_commands,
                assets,
                log,
                retain_target=None,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / "published-readback").exists())
            self.assertFalse((root / "published-input" / "dist").exists())
            self.assertFalse(
                (root / "published-input" / "release-operation" / "registry-readback-digests.json").exists()
            )
            self.assertFalse((root / "cleanup-complete.json").exists())
            self.assertFalse((root / "cleanup-pending.json").exists())
            self.assertFalse((root / pending_name).exists())
            self.assertEqual(list((root / "runner-temp").iterdir()), [])
            self.assertFalse((assets / pending_name).exists())
            completed = ReleaseOperation.parse(json.loads((assets / complete_name).read_bytes()))
            self.assertEqual(completed.state, "RELEASE_COMPLETE")
            self.assertIn("pending-readback", completed.cleanup["temporary_paths"])
            self.assertIn(f"download {complete_name}", log.read_text(encoding="utf-8"))
            self.assertEqual((root / "fake-release-draft").read_text(encoding="utf-8"), "false\n")
