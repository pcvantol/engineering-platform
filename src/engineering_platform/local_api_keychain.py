"""Consumer-side macOS Keychain credential store; never an EP auth authority."""

from __future__ import annotations

import subprocess

SERVICE = "Engineering Platform Local Consumer API"


class KeychainError(RuntimeError):
    """Safe failure category; command diagnostics can contain secret material."""


def _account(consumer_id: str, project_id: str) -> str:
    return f"{consumer_id}:{project_id}"


class MacOSKeychainCredentialStore:
    """Bounded current-user Keychain adapter with no plaintext fallback."""

    def __init__(self, executable: str = "security", service: str = SERVICE) -> None:
        self.executable, self.service = executable, service

    def _run(self, arguments: list[str], *, input_value: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run([self.executable, *arguments], input=input_value, text=True, capture_output=True, check=False)
        except OSError as error:
            raise KeychainError("macOS Keychain is unavailable.") from error

    def put_credential(self, consumer_id: str, project_id: str, token: str) -> None:
        result = self._run(["add-generic-password", "-U", "-s", self.service, "-a", _account(consumer_id, project_id), "-w", token])
        if result.returncode:
            raise KeychainError("macOS Keychain could not store the credential.")

    def get_credential(self, consumer_id: str, project_id: str) -> str:
        result = self._run(["find-generic-password", "-w", "-s", self.service, "-a", _account(consumer_id, project_id)])
        if result.returncode or not result.stdout:
            raise KeychainError("macOS Keychain credential is missing or unavailable.")
        return result.stdout.rstrip("\n")

    def delete_credential(self, consumer_id: str, project_id: str) -> None:
        result = self._run(["delete-generic-password", "-s", self.service, "-a", _account(consumer_id, project_id)])
        if result.returncode:
            raise KeychainError("macOS Keychain credential could not be deleted.")

    def credential_present(self, consumer_id: str, project_id: str) -> bool:
        try:
            self.get_credential(consumer_id, project_id)
        except KeychainError:
            return False
        return True
