from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.qualification import p_central_core_authority_guard as guard


SOURCE_ROOT = Path(__file__).parents[2] / "src"


class PCentralCoreAuthorityGuardTest(unittest.TestCase):
    def test_current_classified_product_source_passes(self) -> None:
        self.assertEqual(guard.violations(SOURCE_ROOT), [])

    def test_new_unclassified_active_root_storage_consumer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src"
            package = source / "engineering_platform"
            package.mkdir(parents=True)
            (package / "execution_host.py").write_text(
                'raise SystemExit("CENTRAL_OPERATIONAL_DATABASE_REQUIRED")\n', encoding="utf-8"
            )
            (package / "new_active_consumer.py").write_text(
                'from engineering_platform.storage import open_storage\nopen_storage(repository_root)\n', encoding="utf-8"
            )
            self.assertEqual(
                guard.violations(source),
                ["UNCLASSIFIED_OPERATIONAL_STORAGE:engineering_platform/new_active_consumer.py"],
            )

    def test_exact_historical_compatibility_path_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src"
            package = source / "engineering_platform"
            package.mkdir(parents=True)
            (package / "execution_host.py").write_text(
                'raise SystemExit("CENTRAL_OPERATIONAL_DATABASE_REQUIRED")\n', encoding="utf-8"
            )
            (package / "telemetry.py").write_text(
                'from engineering_platform.storage import open_storage\nopen_storage(root)\n', encoding="utf-8"
            )
            self.assertEqual(guard.violations(source), [])
