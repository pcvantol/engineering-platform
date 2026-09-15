from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tools" / "qualification" / "github_draft_release.sh"
SOURCE_SHA = "a" * 40
TITLE = "Engineering Platform 2.3.57"


class GitHubDraftReleaseHelperTests(unittest.TestCase):
    def test_selects_actual_draft_id_without_relying_on_eventual_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_gh = root / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    print(json.dumps([
                        {{"id": 77, "draft": True, "tag_name": "untagged-123", "target_commitish": "{SOURCE_SHA}", "name": "{TITLE}"}},
                        {{"id": 78, "draft": False, "tag_name": "engineering-platform-v2.3.57", "target_commitish": "{SOURCE_SHA}", "name": "{TITLE}"}},
                        {{"id": 79, "draft": True, "tag_name": "untagged-456", "target_commitish": "{'b' * 40}", "name": "{TITLE}"}},
                    ]))
                    """
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_release_id"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "EP_RELEASE_TAG": "engineering-platform-v2.3.57",
                    "EP_RELEASE_SOURCE_SHA": SOURCE_SHA,
                    "EP_RELEASE_TITLE": TITLE,
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "77\n")

    def test_create_returns_the_github_release_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_gh = root / "gh"
            invocation = root / "invocation.json"
            fake_gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    Path(os.environ["INVOCATION"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
                    print("389004833")
                    """
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_create_draft_release"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "EP_RELEASE_TAG": "engineering-platform-v2.3.57",
                    "EP_RELEASE_SOURCE_SHA": SOURCE_SHA,
                    "EP_RELEASE_TITLE": TITLE,
                    "INVOCATION": str(invocation),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            arguments = json.loads(invocation.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "389004833\n")
        self.assertIn("repos/example/repository/releases", arguments)
        self.assertIn("tag_name=engineering-platform-v2.3.57", arguments)
        self.assertIn("target_commitish=" + SOURCE_SHA, arguments)
        self.assertIn("name=" + TITLE, arguments)
        self.assertIn("draft=true", arguments)
