from pathlib import Path
import tempfile
import unittest

from tools.qualification import p_central_console_authority_guard as guard


SOURCE_ROOT = Path(__file__).parents[2] / "src"


class PCentralConsoleAuthorityGuardTest(unittest.TestCase):
    def test_installed_console_source_has_no_root_authority(self) -> None:
        self.assertEqual(guard.violations(SOURCE_ROOT), [])

    def test_rejects_each_retired_console_authority_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src" / "engineering_platform"
            source.mkdir(parents=True)
            (source / "server.py").write_text(
                'selected = (parse_qs(request.query).get("project") or [None])[0]\n'
                'dashboard.handler(root)\nopen_storage(root)\nStateStore(root)\n_console_root(root)\n',
                encoding="utf-8",
            )
            self.assertEqual(guard.violations(source.parent), [
                "SUPPORTED_CONSOLE_ROOT_BOUND_ROUTES",
                "SUPPORTED_CONSOLE_DASHBOARD_DELEGATE_ROUTES",
                "SUPPORTED_CONSOLE_OPEN_STORAGE_ROOT",
                "SUPPORTED_CONSOLE_STATESTORE",
            ])
