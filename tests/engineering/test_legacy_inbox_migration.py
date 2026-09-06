from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from engineering_platform.legacy_inbox_migration import main, migrate_icloud_archives


class LegacyInboxMigrationTest(unittest.TestCase):
    def test_migration_moves_archives_without_creating_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo, root = base / "repo", base / "icloud"
            repo.mkdir()
            root.mkdir()
            (root / "Completed").mkdir()
            (root / "Reports").mkdir()
            (root / "Completed" / "old.txt").write_text("# old", encoding="utf-8")
            (root / "Reports" / "old.md").write_text("# report", encoding="utf-8")
            (root / "status.json").write_text('{"watcher_state":"WATCHER_IDLE"}', encoding="utf-8")

            self.assertEqual(migrate_icloud_archives(repo, root), {"moved": 3, "deleted_duplicates": 0})
            self.assertTrue((repo / ".engineering" / "inbox" / "Completed" / "old.txt").exists())
            self.assertTrue((repo / ".engineering" / "reports" / "old.md").exists())
            self.assertTrue((repo / ".engineering" / "status" / "status.json").exists())
            self.assertFalse((root / "Completed").exists())
            self.assertFalse((root / "Reports").exists())
            self.assertFalse((root / "status.json").exists())

    def test_migration_discards_duplicates_skips_links_and_supports_its_operator_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); repo, root = base / "repo", base / "icloud"; repo.mkdir(); root.mkdir()
            source = root / "Completed"; source.mkdir(); (source / "same.md").write_text("new")
            target = repo / ".engineering" / "inbox" / "Completed"; target.mkdir(parents=True); (target / "same.md").write_text("old")
            (source / "linked.md").symlink_to(source / "same.md")
            self.assertEqual(migrate_icloud_archives(repo, root), {"moved": 0, "deleted_duplicates": 1})
            self.assertEqual((target / "same.md").read_text(), "old")
            import io
            from contextlib import redirect_stdout
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--repo", str(repo), "--icloud-root", str(root)]), 0)
            self.assertIn('"moved": 0', output.getvalue())
