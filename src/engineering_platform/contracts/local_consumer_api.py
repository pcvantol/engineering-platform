"""Deterministic, transport-free Local Consumer API v1 contract values.

This module defines only the consumer boundary.  It neither dispatches an
operation nor authenticates a credential, opens a listener, reads storage, or
depends on a consumer repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import unicodedata
from typing import Any, Mapping


LOCAL_CONSUMER_API_CONTRACT_VERSION = "1.0"
LOCAL_CONSUMER_API_REQUEST_TYPE = "contract.foundation"
MAX_PROJECT_ID_LENGTH = 128
MAX_CONSUMER_ID_LENGTH = 128
MAX_REQUEST_ID_LENGTH = 128
MAX_CREDENTIAL_LENGTH = 4096
MAX_PAYLOAD_BYTES = 8192

_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9-]*\Z")
_CREDENTIAL_PATTERN = re.compile(r"[\x21-\x7e]+\Z")
_REQUEST_FIELDS = frozenset(
    {"contract_version", "request_type", "request_id", "project_id", "consumer", "auth", "payload"}
)
_CONSUMER_FIELDS = frozenset({"consumer_id"})
_AUTH_FIELDS = frozenset({"scheme", "credential"})
_RESPONSE_FIELDS = frozenset({"contract_version", "request_id", "status", "payload"})
_ERROR_ENVELOPE_FIELDS = frozenset({"contract_version", "request_id", "status", "error"})
_ERROR_FIELDS = frozenset({"code", "message", "field", "path"})


class ErrorCode:
    """Stable, public Local Consumer API v1 error codes."""

    INVALID_CONTRACT_VERSION = "INVALID_CONTRACT_VERSION"
    MISSING_PROJECT_ID = "MISSING_PROJECT_ID"
    INVALID_PROJECT_ID = "INVALID_PROJECT_ID"
    INVALID_CONSUMER_IDENTITY = "INVALID_CONSUMER_IDENTITY"
    INVALID_AUTH_ENVELOPE = "INVALID_AUTH_ENVELOPE"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    UNSUPPORTED_REQUEST_TYPE = "UNSUPPORTED_REQUEST_TYPE"
    INVALID_NORMALIZATION = "INVALID_NORMALIZATION"
    VALUE_TOO_LARGE = "VALUE_TOO_LARGE"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PROJECT_NOT_AUTHORIZED = "PROJECT_NOT_AUTHORIZED"
    SERVICE_NOT_READY = "SERVICE_NOT_READY"


_MESSAGES = {
    ErrorCode.INVALID_CONTRACT_VERSION: "The Local Consumer API contract version is invalid or unsupported.",
    ErrorCode.MISSING_PROJECT_ID: "project_id is required.",
    ErrorCode.INVALID_PROJECT_ID: "project_id is invalid.",
    ErrorCode.INVALID_CONSUMER_IDENTITY: "consumer identity is invalid.",
    ErrorCode.INVALID_AUTH_ENVELOPE: "authentication envelope is invalid.",
    ErrorCode.UNKNOWN_FIELD: "The envelope contains an unknown field.",
    ErrorCode.UNSUPPORTED_REQUEST_TYPE: "request_type is unsupported.",
    ErrorCode.INVALID_NORMALIZATION: "The value is not in canonical normalized form.",
    ErrorCode.VALUE_TOO_LARGE: "The value exceeds the contract limit.",
    ErrorCode.MALFORMED_REQUEST: "The request is malformed.",
    ErrorCode.UNAUTHENTICATED: "Authentication is required or invalid.",
    ErrorCode.PROJECT_NOT_AUTHORIZED: "The credential is not authorized for this project.",
    ErrorCode.SERVICE_NOT_READY: "The Local Consumer API is not ready.",
}


@dataclass(frozen=True)
class ContractError(ValueError):
    """A safe, stable validation failure; submitted values are never retained."""

    code: str
    field: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        if self.code not in _MESSAGES:
            raise ValueError("unknown Local Consumer API error code")

    @property
    def message(self) -> str:
        return _MESSAGES[self.code]

    def __str__(self) -> str:
        return self.message

    def to_error_envelope(self, request_id: str | None = None) -> "ErrorEnvelope":
        return ErrorEnvelope(
            request_id=request_id, code=self.code, field=self.field, path=self.path
        )


def _require_mapping(value: object, *, code: str, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractError(code, field=field, path=field)
    return value


def _reject_unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], *, path: str) -> None:
    if set(value) - allowed:
        # Never reflect arbitrary submitted keys into a diagnostic: a key is
        # itself consumer-controlled and could carry sensitive text.
        raise ContractError(ErrorCode.UNKNOWN_FIELD, field=path, path=path)


def _identifier(
    value: object, *, field: str, invalid_code: str, missing_code: str | None = None, limit: int
) -> str:
    if value is None and missing_code:
        raise ContractError(missing_code, field=field, path=field)
    if not isinstance(value, str) or not value:
        raise ContractError(invalid_code, field=field, path=field)
    if len(value) > limit:
        raise ContractError(ErrorCode.VALUE_TOO_LARGE, field=field, path=field)
    if unicodedata.normalize("NFC", value) != value:
        raise ContractError(ErrorCode.INVALID_NORMALIZATION, field=field, path=field)
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ContractError(invalid_code, field=field, path=field)
    return value


def _normalize_payload(value: object, *, path: str = "payload", depth: int = 0) -> object:
    if depth > 4:
        raise ContractError(ErrorCode.VALUE_TOO_LARGE, field="payload", path=path)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, list):
        if len(value) > 64:
            raise ContractError(ErrorCode.VALUE_TOO_LARGE, field="payload", path=path)
        return [
            _normalize_payload(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        if len(value) > 64:
            raise ContractError(ErrorCode.VALUE_TOO_LARGE, field="payload", path=path)
        normalized: dict[str, object] = {}
        for key in sorted(value):
            normalized_key = unicodedata.normalize(
                "NFC", key.replace("\r\n", "\n").replace("\r", "\n")
            )
            if not normalized_key or normalized_key in normalized:
                raise ContractError(ErrorCode.INVALID_NORMALIZATION, field="payload", path=path)
            normalized[normalized_key] = _normalize_payload(
                value[key], path=f"{path}.{normalized_key}", depth=depth + 1
            )
        return normalized
    raise ContractError(ErrorCode.MALFORMED_REQUEST, field="payload", path=path)


def _payload(value: object) -> dict[str, object]:
    payload = _require_mapping(value, code=ErrorCode.MALFORMED_REQUEST, field="payload")
    normalized = _normalize_payload(payload)
    assert isinstance(normalized, dict)
    try:
        encoded = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ContractError(ErrorCode.MALFORMED_REQUEST, field="payload", path="payload") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ContractError(ErrorCode.VALUE_TOO_LARGE, field="payload", path="payload")
    return normalized


@dataclass(frozen=True)
class ConsumerEnvelope:
    consumer_id: str

    @classmethod
    def parse(cls, value: object) -> "ConsumerEnvelope":
        consumer = _require_mapping(
            value, code=ErrorCode.INVALID_CONSUMER_IDENTITY, field="consumer"
        )
        _reject_unknown_fields(consumer, _CONSUMER_FIELDS, path="consumer")
        return cls(
            consumer_id=_identifier(
                consumer.get("consumer_id"),
                field="consumer_id",
                invalid_code=ErrorCode.INVALID_CONSUMER_IDENTITY,
                limit=MAX_CONSUMER_ID_LENGTH,
            )
        )

    def to_dict(self) -> dict[str, str]:
        return {"consumer_id": self.consumer_id}


@dataclass(frozen=True)
class AuthEnvelope:
    scheme: str
    credential: str

    @classmethod
    def parse(cls, value: object) -> "AuthEnvelope":
        auth = _require_mapping(value, code=ErrorCode.INVALID_AUTH_ENVELOPE, field="auth")
        _reject_unknown_fields(auth, _AUTH_FIELDS, path="auth")
        scheme, credential = auth.get("scheme"), auth.get("credential")
        if scheme != "bearer" or not isinstance(credential, str) or not credential:
            raise ContractError(ErrorCode.INVALID_AUTH_ENVELOPE, field="auth", path="auth")
        if len(credential) > MAX_CREDENTIAL_LENGTH:
            raise ContractError(
                ErrorCode.VALUE_TOO_LARGE, field="credential", path="auth.credential"
            )
        if not _CREDENTIAL_PATTERN.fullmatch(credential):
            raise ContractError(
                ErrorCode.INVALID_AUTH_ENVELOPE, field="credential", path="auth.credential"
            )
        return cls(scheme=scheme, credential=credential)

    def to_dict(self, *, safe: bool = False) -> dict[str, str]:
        return {"scheme": self.scheme, "credential": "[REDACTED]" if safe else self.credential}


@dataclass(frozen=True)
class RequestEnvelope:
    contract_version: str
    request_type: str
    request_id: str
    project_id: str
    consumer: ConsumerEnvelope
    auth: AuthEnvelope
    payload: dict[str, object]

    @classmethod
    def parse(cls, value: object) -> "RequestEnvelope":
        request = _require_mapping(value, code=ErrorCode.MALFORMED_REQUEST, field="request")
        _reject_unknown_fields(request, _REQUEST_FIELDS, path="request")
        version = request.get("contract_version")
        if version != LOCAL_CONSUMER_API_CONTRACT_VERSION:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT_VERSION,
                field="contract_version",
                path="contract_version",
            )
        request_type = request.get("request_type")
        if request_type != LOCAL_CONSUMER_API_REQUEST_TYPE:
            raise ContractError(
                ErrorCode.UNSUPPORTED_REQUEST_TYPE, field="request_type", path="request_type"
            )
        request_id = _identifier(
            request.get("request_id"),
            field="request_id",
            invalid_code=ErrorCode.MALFORMED_REQUEST,
            limit=MAX_REQUEST_ID_LENGTH,
        )
        return cls(
            contract_version=version,
            request_type=request_type,
            request_id=request_id,
            project_id=_identifier(
                request.get("project_id"),
                field="project_id",
                invalid_code=ErrorCode.INVALID_PROJECT_ID,
                missing_code=ErrorCode.MISSING_PROJECT_ID,
                limit=MAX_PROJECT_ID_LENGTH,
            ),
            consumer=ConsumerEnvelope.parse(request.get("consumer")),
            auth=AuthEnvelope.parse(request.get("auth")),
            payload=_payload(request.get("payload")),
        )

    def to_dict(self, *, safe: bool = False) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "request_type": self.request_type,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "consumer": self.consumer.to_dict(),
            "auth": self.auth.to_dict(safe=safe),
            "payload": self.payload,
        }

    def serialize(self) -> str:
        return serialize(self.to_dict())

    def safe_dict(self) -> dict[str, object]:
        return self.to_dict(safe=True)


@dataclass(frozen=True)
class ResponseEnvelope:
    request_id: str
    payload: dict[str, object]
    contract_version: str = LOCAL_CONSUMER_API_CONTRACT_VERSION
    status: str = "success"

    def __post_init__(self) -> None:
        if self.contract_version != LOCAL_CONSUMER_API_CONTRACT_VERSION or self.status != "success":
            raise ContractError(ErrorCode.INVALID_CONTRACT_VERSION, field="contract_version")
        _identifier(
            self.request_id,
            field="request_id",
            invalid_code=ErrorCode.MALFORMED_REQUEST,
            limit=MAX_REQUEST_ID_LENGTH,
        )
        object.__setattr__(self, "payload", _payload(self.payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "status": self.status,
            "payload": self.payload,
        }

    def serialize(self) -> str:
        return serialize(self.to_dict())


@dataclass(frozen=True)
class ErrorEnvelope:
    request_id: str | None
    code: str
    field: str | None = None
    path: str | None = None
    contract_version: str = LOCAL_CONSUMER_API_CONTRACT_VERSION
    status: str = "error"

    def __post_init__(self) -> None:
        if (
            self.contract_version != LOCAL_CONSUMER_API_CONTRACT_VERSION
            or self.status != "error"
            or self.code not in _MESSAGES
        ):
            raise ContractError(ErrorCode.MALFORMED_REQUEST)
        if self.request_id is not None:
            _identifier(
                self.request_id,
                field="request_id",
                invalid_code=ErrorCode.MALFORMED_REQUEST,
                limit=MAX_REQUEST_ID_LENGTH,
            )
        for value in (self.field, self.path):
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 128
                or not re.fullmatch(r"[a-z][a-z0-9_.\[\]]*", value)
            ):
                raise ContractError(ErrorCode.MALFORMED_REQUEST)

    def to_dict(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": _MESSAGES[self.code]}
        if self.field is not None:
            error["field"] = self.field
        if self.path is not None:
            error["path"] = self.path
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "status": self.status,
            "error": error,
        }

    def serialize(self) -> str:
        return serialize(self.to_dict())


def serialize(value: object) -> str:
    """Return canonical JSON for an already validated contract value."""
    if isinstance(value, (RequestEnvelope, ResponseEnvelope, ErrorEnvelope)):
        value = value.to_dict()
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def deserialize_request(serialized: str | bytes | bytearray) -> RequestEnvelope:
    """Decode JSON without duplicate-key ambiguity, then validate the request."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ContractError(ErrorCode.MALFORMED_REQUEST, field=key, path=key)
            result[key] = item
        return result

    try:
        decoded = json.loads(serialized, object_pairs_hook=reject_duplicate_keys)
    except ContractError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError(ErrorCode.MALFORMED_REQUEST) from None
    return RequestEnvelope.parse(decoded)
