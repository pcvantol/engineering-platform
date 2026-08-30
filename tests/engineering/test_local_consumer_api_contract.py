"""Focused Local Consumer API v1 contract-foundation tests."""

from __future__ import annotations

import json
import unittest

from tools.engineering.contracts.local_consumer_api import (
    ContractError,
    ErrorCode,
    ErrorEnvelope,
    LOCAL_CONSUMER_API_CONTRACT_VERSION,
    RequestEnvelope,
    ResponseEnvelope,
    deserialize_request,
)


SECRET = "recognizable-contract-secret-DO-NOT-LEAK"


def request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": LOCAL_CONSUMER_API_CONTRACT_VERSION,
        "request_type": "contract.foundation",
        "request_id": "request-123",
        "project_id": "project-123",
        "consumer": {"consumer_id": "workspace-client"},
        "auth": {"scheme": "bearer", "credential": SECRET},
        "payload": {"label": "Cafe\u0301", "lines": "one\r\ntwo\rthree"},
    }
    value.update(overrides)
    return value


class LocalConsumerApiContractTests(unittest.TestCase):
    def assert_error(self, value: object, code: str) -> ContractError:
        with self.assertRaises(ContractError) as captured:
            RequestEnvelope.parse(value)
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    def test_valid_request_normalizes_and_serializes_deterministically(self) -> None:
        parsed = RequestEnvelope.parse(request())
        self.assertEqual(parsed.project_id, "project-123")
        self.assertEqual(parsed.consumer.consumer_id, "workspace-client")
        self.assertEqual(parsed.payload, {"label": "Café", "lines": "one\ntwo\nthree"})
        serialized = parsed.serialize()
        self.assertEqual(serialized, RequestEnvelope.parse(json.loads(serialized)).serialize())
        self.assertEqual(parsed, deserialize_request(serialized))

    def test_project_scope_is_required_canonical_and_bounded(self) -> None:
        missing = request()
        del missing["project_id"]
        self.assert_error(missing, ErrorCode.MISSING_PROJECT_ID)
        for invalid in ("", "Project-123", "project_123", "project-123 ", "project-" + "a" * 121):
            self.assert_error(request(project_id=invalid), ErrorCode.INVALID_PROJECT_ID if len(invalid) <= 128 else ErrorCode.VALUE_TOO_LARGE)

    def test_versions_and_request_types_fail_closed(self) -> None:
        self.assert_error(request(contract_version=None), ErrorCode.INVALID_CONTRACT_VERSION)
        self.assert_error(request(contract_version="1"), ErrorCode.INVALID_CONTRACT_VERSION)
        self.assert_error(request(contract_version="2.0"), ErrorCode.INVALID_CONTRACT_VERSION)
        self.assert_error(request(request_type="runs.submit"), ErrorCode.UNSUPPORTED_REQUEST_TYPE)
        self.assert_error(request(request_id="request_123"), ErrorCode.MALFORMED_REQUEST)
        self.assert_error(request(request_id="request-" + "a" * 121), ErrorCode.VALUE_TOO_LARGE)

    def test_unknown_fields_are_rejected(self) -> None:
        self.assert_error(request(unexpected="ignored-never"), ErrorCode.UNKNOWN_FIELD)
        self.assert_error(request(consumer={"consumer_id": "workspace-client", "role": "admin"}), ErrorCode.UNKNOWN_FIELD)
        self.assert_error(request(auth={"scheme": "bearer", "credential": SECRET, "header": "Authorization"}), ErrorCode.UNKNOWN_FIELD)

    def test_auth_shape_is_bounded_and_never_verified_here(self) -> None:
        self.assert_error(request(consumer={}), ErrorCode.INVALID_CONSUMER_IDENTITY)
        self.assert_error(request(auth={"scheme": "basic", "credential": SECRET}), ErrorCode.INVALID_AUTH_ENVELOPE)
        self.assert_error(request(auth={"scheme": "bearer", "credential": "bad\ncredential"}), ErrorCode.INVALID_AUTH_ENVELOPE)
        self.assert_error(request(auth={"scheme": "bearer", "credential": "a" * 4097}), ErrorCode.VALUE_TOO_LARGE)

    def test_safe_rendering_and_errors_never_leak_credential(self) -> None:
        parsed = RequestEnvelope.parse(request())
        safe = json.dumps(parsed.safe_dict())
        self.assertNotIn(SECRET, safe)
        self.assertIn("[REDACTED]", safe)
        error = self.assert_error(request(auth={"scheme": "bearer", "credential": SECRET, SECRET: "ignored"}), ErrorCode.UNKNOWN_FIELD)
        rendered_error = error.to_error_envelope("request-123").serialize()
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, rendered_error)
        self.assertNotIn(SECRET, repr(error))

    def test_duplicate_json_keys_and_oversized_payload_fail_closed(self) -> None:
        duplicate = '{"contract_version":"1.0","contract_version":"1.0"}'
        with self.assertRaises(ContractError) as captured:
            deserialize_request(duplicate)
        self.assertEqual(captured.exception.code, ErrorCode.MALFORMED_REQUEST)
        self.assert_error(request(payload={"text": "a" * 8193}), ErrorCode.VALUE_TOO_LARGE)

    def test_response_and_error_envelopes_are_versioned_and_stable(self) -> None:
        response = ResponseEnvelope(request_id="request-123", payload={"result": "ok"})
        self.assertEqual(response.serialize(), ResponseEnvelope(request_id="request-123", payload={"result": "ok"}).serialize())
        error = ErrorEnvelope(request_id="request-123", code=ErrorCode.INVALID_PROJECT_ID, field="project_id", path="project_id")
        body = json.loads(error.serialize())
        self.assertEqual(body["contract_version"], "1.0")
        self.assertEqual(body["error"]["code"], ErrorCode.INVALID_PROJECT_ID)
        self.assertNotIn(SECRET, error.serialize())
