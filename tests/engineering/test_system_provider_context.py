from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engineering_platform import system_installation_topology as topology
from engineering_platform import system_provider_context as providers


class SystemProviderContextTests(unittest.TestCase):
    def _instance(self, root: Path, identity: str) -> topology.SystemInstanceTopology:
        return topology.system_instance_topology(
            topology.system_installation_topology(root),
            instance_id=identity,
            display_label=identity.replace("-", " "),
        )

    @staticmethod
    def _executable(context: providers.ProviderContext) -> str:
        context.executable.parent.mkdir(parents=True, exist_ok=True)
        context.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        context.executable.chmod(0o755)
        return providers.executable_digest(context.executable)

    def test_instance_owned_codex_and_github_are_independently_cold_boot_ready(self) -> None:
        with TemporaryDirectory() as temporary:
            product = Path(temporary) / "EP"
            first = self._instance(product, "ep-production-0001")
            second = self._instance(product, "ep-development-0001")
            readbacks = []
            for instance in (first, second):
                for context in providers.provider_contexts(instance):
                    digest = self._executable(context)
                    readbacks.append(providers.record_context(
                        context,
                        executable_sha256=digest,
                        version="1.2.3",
                        auth_reference=f"{context.provider}:auth:{instance.instance_id}",
                        auth_bootstrap_receipt=f"{context.provider}:receipt:{instance.instance_id}",
                    ))
            self.assertTrue(all(item["cold_boot_ready"] for item in readbacks))
            self.assertEqual(len({item["home"] for item in readbacks}), 4)
            self.assertEqual(len({item["executable"] for item in readbacks}), 4)
            providers.assert_distinct(first, second)
            environment = providers.server_environment(first)
            self.assertEqual(environment["CODEX_HOME"], str(providers.provider_context(first, "codex").home))
            self.assertEqual(environment["GH_CONFIG_DIR"], str(providers.provider_context(first, "github").home))
            self.assertNotIn("HOME", environment)

    def test_missing_auth_and_executable_substitution_fail_closed_without_secret_output(self) -> None:
        with TemporaryDirectory() as temporary:
            instance = self._instance(Path(temporary) / "EP", "ep-production-0001")
            context = providers.provider_context(instance, "codex")
            digest = self._executable(context)
            providers.record_context(
                context,
                executable_sha256=digest,
                version="1.2.3",
                auth_reference="codex:auth:production",
                auth_bootstrap_receipt="codex:receipt:production",
            )
            context.auth_receipt.unlink()
            with self.assertRaisesRegex(providers.SystemProviderContextError, "authentication receipt"):
                providers.readback(context)
            providers.record_context(
                context,
                executable_sha256=digest,
                version="1.2.3",
                auth_reference="codex:auth:production",
                auth_bootstrap_receipt="codex:receipt:production",
            )
            context.executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            context.executable.chmod(0o755)
            with self.assertRaisesRegex(providers.SystemProviderContextError, "substituted"):
                providers.readback(context)

    def test_provider_executable_must_be_regular_not_symlinked(self) -> None:
        with TemporaryDirectory() as temporary:
            instance = self._instance(Path(temporary) / "EP", "ep-production-0001")
            context = providers.provider_context(instance, "github")
            context.executable.parent.mkdir(parents=True, exist_ok=True)
            context.executable.symlink_to(Path("/bin/sh"))
            with self.assertRaisesRegex(providers.SystemProviderContextError, "unavailable"):
                providers.executable_digest(context.executable)


if __name__ == "__main__":
    unittest.main()
