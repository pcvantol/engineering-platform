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
            fake_curl = root / "curl"
            invocation = root / "invocation.json"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    Path(os.environ["INVOCATION"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
                    print(json.dumps({"id": 389004833}))
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_create_draft_release"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "EP_RELEASE_TAG": "engineering-platform-v2.3.57",
                    "EP_RELEASE_SOURCE_SHA": SOURCE_SHA,
                    "EP_RELEASE_TITLE": TITLE,
                    "GH_TOKEN": "test-token",
                    "INVOCATION": str(invocation),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            arguments = json.loads(invocation.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "389004833\n")
        self.assertIn("https://api.github.com/repos/example/repository/releases", arguments)
        self.assertIn("Authorization: Bearer test-token", arguments)
        self.assertIn("Accept: application/vnd.github+json", arguments)
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", arguments)
        payload = json.loads(arguments[arguments.index("--data") + 1])
        self.assertEqual(
            payload,
            {
                "tag_name": "engineering-platform-v2.3.57",
                "target_commitish": SOURCE_SHA,
                "name": TITLE,
                "draft": True,
            },
        )

    def test_create_rejects_an_empty_or_non_json_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_curl = root / "curl"
            fake_curl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_curl.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_create_draft_release"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "EP_RELEASE_TAG": "engineering-platform-v2.3.57",
                    "EP_RELEASE_SOURCE_SHA": SOURCE_SHA,
                    "EP_RELEASE_TITLE": TITLE,
                    "GH_TOKEN": "test-token",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("draft release creation returned invalid JSON", result.stderr)

    def test_upload_uses_the_github_uploads_endpoint_with_encoded_asset_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_curl = root / "curl"
            invocation = root / "invocation.json"
            receipt = root / "receipt.json"
            receipt.write_text("receipt", encoding="utf-8")
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    Path(os.environ["INVOCATION"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
                    print("201", end="")
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_upload 389 '{receipt}' 'qualified receipt.json'"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "GH_TOKEN": "test-token",
                    "INVOCATION": str(invocation),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            arguments = json.loads(invocation.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--request", arguments)
        self.assertIn("POST", arguments)
        self.assertIn("Authorization: Bearer test-token", arguments)
        self.assertNotIn("--retry", arguments)
        self.assertNotIn("--retry-all-errors", arguments)
        self.assertIn("X-GitHub-Api-Version: 2022-11-28", arguments)
        self.assertIn("--data-binary", arguments)
        self.assertIn("@" + str(receipt), arguments)
        self.assertIn(
            "https://uploads.github.com/repos/example/repository/releases/389/assets?name=qualified%20receipt.json",
            arguments,
        )

    def test_upload_reconciles_an_accepted_asset_after_ambiguous_502_without_reposting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            receipt.write_text("exact receipt", encoding="utf-8")
            counter = root / "curl-count"
            fake_curl = root / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    from pathlib import Path

                    counter = Path(os.environ["COUNTER"])
                    counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
                    print("502", end="")
                    raise SystemExit(22)
                    """
                ),
                encoding="utf-8",
            )
            fake_gh = root / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    endpoint = sys.argv[-1]
                    if endpoint.endswith("/releases/389"):
                        print(json.dumps({"assets": [{"id": 991, "name": "receipt.json", "state": "uploaded"}]}))
                    elif endpoint.endswith("/releases/assets/991"):
                        sys.stdout.buffer.write(Path(os.environ["RECEIPT"]).read_bytes())
                    else:
                        raise SystemExit(f"unexpected gh invocation: {sys.argv}")
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            fake_gh.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_upload 389 '{receipt}' receipt.json"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "GH_TOKEN": "test-token",
                    "COUNTER": str(counter),
                    "RECEIPT": str(receipt),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            counter_value = counter.read_text()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(counter_value, "1")

    def test_upload_removes_a_starter_asset_before_one_controlled_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            receipt.write_text("exact receipt", encoding="utf-8")
            counter = root / "curl-count"
            state = root / "asset-state"
            fake_curl = root / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    from pathlib import Path

                    counter = Path(os.environ["COUNTER"])
                    count = int(counter.read_text()) + 1 if counter.exists() else 1
                    counter.write_text(str(count))
                    if count == 1:
                        Path(os.environ["STATE"]).write_text("starter")
                        print("502", end="")
                        raise SystemExit(22)
                    print("201", end="")
                    """
                ),
                encoding="utf-8",
            )
            fake_gh = root / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    endpoint = sys.argv[-1]
                    state = Path(os.environ["STATE"])
                    if endpoint.endswith("/releases/389"):
                        assets = [{"id": 992, "name": "receipt.json", "state": "starter"}] if state.exists() else []
                        print(json.dumps({"assets": assets}))
                    elif "--method" in sys.argv and endpoint.endswith("/releases/assets/992"):
                        state.unlink()
                    else:
                        raise SystemExit(f"unexpected gh invocation: {sys.argv}")
                    """
                ),
                encoding="utf-8",
            )
            fake_sleep = root / "sleep"
            fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for executable in (fake_curl, fake_gh, fake_sleep):
                executable.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_upload 389 '{receipt}' receipt.json"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "GH_TOKEN": "test-token",
                    "COUNTER": str(counter),
                    "STATE": str(state),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            counter_value = counter.read_text()
            state_exists = state.exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(counter_value, "2")
        self.assertFalse(state_exists)

    def test_upload_does_not_retry_or_read_assets_after_permanent_authorization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            receipt.write_text("exact receipt", encoding="utf-8")
            counter = root / "curl-count"
            gh_invoked = root / "gh-invoked"
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env python3\n"
                "import os\nfrom pathlib import Path\n"
                "counter=Path(os.environ['COUNTER']); counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
                "print('403', end='')\n",
                encoding="utf-8",
            )
            fake_gh = root / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
                "Path(os.environ['GH_INVOKED']).write_text('yes')\nraise SystemExit(1)\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            fake_gh.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_upload 389 '{receipt}' receipt.json"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "GH_TOKEN": "test-token",
                    "COUNTER": str(counter),
                    "GH_INVOKED": str(gh_invoked),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            counter_value = counter.read_text()
            gh_was_invoked = gh_invoked.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(counter_value, "1")
        self.assertFalse(gh_was_invoked)
        self.assertIn("failed permanently with HTTP 403", result.stderr)

    def test_upload_reconciles_but_rejects_an_undocumented_204_without_asset_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            receipt.write_text("exact receipt", encoding="utf-8")
            counter = root / "curl-count"
            gh_counter = root / "gh-count"
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env python3\n"
                "import os\nfrom pathlib import Path\n"
                "counter=Path(os.environ['COUNTER']); counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
                "print('204', end='')\n",
                encoding="utf-8",
            )
            fake_gh = root / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env python3\nimport json, os\nfrom pathlib import Path\n"
                "counter=Path(os.environ['GH_COUNTER']); counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
                "print(json.dumps({'assets': []}))\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            fake_gh.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_upload 389 '{receipt}' receipt.json"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "GH_TOKEN": "test-token",
                    "COUNTER": str(counter),
                    "GH_COUNTER": str(gh_counter),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            counter_value = counter.read_text()
            gh_counter_value = gh_counter.read_text()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(counter_value, "1")
        self.assertEqual(gh_counter_value, "1")
        self.assertIn("unexpected HTTP 204 without asset evidence", result.stderr)

    def test_upload_rejects_an_uploaded_asset_with_different_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            receipt.write_text("expected", encoding="utf-8")
            fake_curl = root / "curl"
            fake_curl.write_text("#!/usr/bin/env bash\nprintf 502\nexit 22\n", encoding="utf-8")
            fake_gh = root / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    endpoint = sys.argv[-1]
                    if endpoint.endswith("/releases/389"):
                        print(json.dumps({"assets": [{"id": 993, "name": "receipt.json", "state": "uploaded"}]}))
                    elif endpoint.endswith("/releases/assets/993"):
                        print("different", end="")
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o700)
            fake_gh.chmod(0o700)
            result = subprocess.run(
                ["bash", "-c", f"source '{HELPER}'; ep_draft_upload 389 '{receipt}' receipt.json"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/repository",
                    "GH_TOKEN": "test-token",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected bytes", result.stderr)
