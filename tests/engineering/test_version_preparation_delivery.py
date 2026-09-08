from __future__ import annotations

from pathlib import Path
import unittest

from engineering_platform.version_preparation_delivery import VersionPreparationDelivery, VersionPreparationError, VersionPreparationRequest


def request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": "1", "operation_id": "operation-0001", "product_id": "forge",
        "component_id": "product", "repository_id": "pcvantol/forge", "policy_revision": "v1",
        "policy_digest": "sha256:policy", "source_event_set": ["merge:1"], "source_event_policy": "main",
        "expected_source_revision": "a" * 40, "expected_target_branch_revision": None,
        "expected_version": "2.3.0", "requested_change": "minor", "determined_target_version": "2.4.0",
        "allowed_projection_paths": ["product-version.json"], "prepared_operation_digest": "sha256:diff",
        "authorization_reference": "grant:bounded", "delivery_mode": "PROTECTED_VERSION_PREPARATION_CANDIDATE",
    }
    value.update(overrides)
    return value


class VersionPreparationRequestTest(unittest.TestCase):
    def test_accepts_exact_bounded_contract(self) -> None:
        parsed = VersionPreparationRequest.parse(request())
        self.assertEqual(parsed.operation_id, "operation-0001")
        self.assertEqual(parsed.allowed_projection_paths, ("product-version.json",))

    def test_rejects_unknown_and_untrusted_paths(self) -> None:
        with self.assertRaisesRegex(VersionPreparationError, "unknown"):
            VersionPreparationRequest.parse({**request(), "shell": "rm"})
        with self.assertRaisesRegex(VersionPreparationError, "paths"):
            VersionPreparationRequest.parse(request(allowed_projection_paths=["../outside"]))

    def test_rejects_duplicate_events_and_wrong_source_sha(self) -> None:
        with self.assertRaisesRegex(VersionPreparationError, "event"):
            VersionPreparationRequest.parse(request(source_event_set=["merge:1", "merge:1"]))
        with self.assertRaisesRegex(VersionPreparationError, "exact SHA"):
            VersionPreparationRequest.parse(request(expected_source_revision="main"))

    def test_candidate_branch_is_deterministically_bound_to_operation(self) -> None:
        parsed = VersionPreparationRequest.parse(request())
        self.assertEqual(VersionPreparationDelivery.branch_name(parsed), "ep/version-preparation/operation-0001")


if __name__ == "__main__":
    unittest.main()
