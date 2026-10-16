"""Finite R50 input binding for the first absent local Genesis operation.

This validates untrusted transport data. It neither renders documents nor
confers effect authority; the installation owner independently pins the request.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .repository_attachment import parse_repository_attachment, RepositoryAttachmentError

CONTRACT = "1.0"
BASELINE = "portable-docs/1.0.0"
VALIDATOR_SHA256 = "3ce4eca67c659d65ea2df70439d7f61bc8e4eb16b3d4b64b397d7fc3bdc16ae2"
RENDERER_SHA256 = "a7a16db4d735d9411ff3a3c7997d8aff82be2d4b6f2059aa56bf250b7e104536"
BASE_PATH = "engineering/baselines/portable-docs/1.0.0/"
MAX_BODY = 524288
MAX_PAYLOAD = 262144
EFFECTS = ("CREATE_TARGET", "WRITE_MANIFEST_FILES", "TRUSTED_VALIDATE", "LOCAL_GIT_COMMIT", "COMMITTED_B8R_ATTACH")
PLAN_FIELDS = frozenset({"plan_id", "plan_revision", "plan_digest", "decision_id", "decision_digest",
    "proposal_id", "proposal_digest", "source_instance_id", "source_actor_id", "context_id"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BASE_FILES = ("baseline-lock.schema.json", "capability.json", "capability.schema.json", "catalog.json",
    "catalog.schema.json", "dag.schema.json", "governance.schema.json", "input.schema.json",
    "manifest.schema.json", "operation-proposal.schema.json", "project-contract.schema.json",
    "project.schema.json", "render_core.py", "safe_io.py", "schema.py", "validate.py", "validation.yml")
PATHS = frozenset({BASE_PATH + name for name in _BASE_FILES} | {
    ".engineering-platform/repository.json", ".gitattributes", ".gitignore", "AGENTS.md", "BOOTSTRAP.md", "README.md",
    "docs/architecture/ARCHITECTURE.md", "docs/architecture/decisions/README.md", "docs/engineering/HANDOFF.md",
    "docs/engineering/VALIDATION.md", "docs/product/VISION.md", "docs/roadmap/ROADMAP.md", "docs/roadmap/capability-dag.json",
    "engineering/baseline.lock.json", "engineering/bootstrap-manifest.json", "engineering/project-contract.json",
    "engineering/project.json", "engineering/render-input.json", "engineering/repository-governance.json", "scripts/validate.sh"})


class BootstrapError(ValueError):
    """Stable redacted transport error; original private cause is separate."""
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise BootstrapError("BOOTSTRAP_ID_INVALID")
    return value


def sha256(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BootstrapError("BOOTSTRAP_DIGEST_INVALID")
    return value


def closed(value: object, fields: set[str] | frozenset[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise BootstrapError("BOOTSTRAP_FIELDS_INVALID")
    return value


def load(raw: bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BootstrapError("BOOTSTRAP_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        if len(raw) > MAX_BODY:
            raise BootstrapError("BOOTSTRAP_BODY_LIMIT", 413)
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(BootstrapError("BOOTSTRAP_NONFINITE_JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise BootstrapError("BOOTSTRAP_JSON_INVALID") from error


@dataclass(frozen=True)
class Bundle:
    files: dict[str, bytes]
    bundle_digest: str
    manifest_digest: str
    input_digest: str
    project_id: str
    authority_repository_id: str
    repository_id: str


def bundle(value: object) -> Bundle:
    """Structural/hash gate; trusted external validator supplies semantic proof."""
    root = closed(value, {"contract_version", "kind", "files"})
    if root["contract_version"] != "pb-render/1.0" or root["kind"] != "RENDERED_PROPOSAL_EXAMPLE":
        raise BootstrapError("BOOTSTRAP_BUNDLE_FORMAT_UNSUPPORTED", 422)
    entries = root["files"]
    if not isinstance(entries, dict) or set(entries) != PATHS:
        raise BootstrapError("BOOTSTRAP_FINITE_PATH_SET_INVALID", 422)
    files = {}
    total = 0
    for name, entry in entries.items():
        row = closed(entry, {"sha256", "utf8"})
        if not isinstance(row["utf8"], str):
            raise BootstrapError("BOOTSTRAP_PAYLOAD_INVALID")
        try:
            raw = row["utf8"].encode("utf-8")
        except UnicodeError as error:
            raise BootstrapError("BOOTSTRAP_PAYLOAD_INVALID") from error
        total += len(raw)
        if len(raw) > 131072 or total > MAX_PAYLOAD:
            raise BootstrapError("BOOTSTRAP_PAYLOAD_LIMIT", 413)
        if digest(raw) != sha256(row["sha256"]):
            raise BootstrapError("BOOTSTRAP_PAYLOAD_DIGEST_CONFLICT", 409)
        files[name] = raw
    manifest = closed(load(files["engineering/bootstrap-manifest.json"]), {"artifacts", "baseline", "contract_version",
        "input_digest", "manifest_digest", "renderer_source_digest", "renderer_version", "self_reference", "state"})
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    if digest(canonical(unsigned)) != sha256(manifest["manifest_digest"]):
        raise BootstrapError("BOOTSTRAP_MANIFEST_DIGEST_CONFLICT", 409)
    if (manifest["baseline"] != BASELINE or manifest["contract_version"] != "pb-render/1.0"
            or manifest["renderer_version"] != "1.0.0" or manifest["renderer_source_digest"] != RENDERER_SHA256
            or manifest["state"] != "RENDERED_PROPOSAL"
            or manifest["self_reference"] != "manifest excluded from artifact hashes; digest excludes manifest_digest"):
        raise BootstrapError("BOOTSTRAP_PROFILE_UNSUPPORTED", 422)
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != len(PATHS) - 1:
        raise BootstrapError("BOOTSTRAP_ARTIFACT_SET_INVALID")
    seen = set()
    fields = {"artifact_id", "bytes", "condition", "encoding", "expected_after_digest", "expected_before", "file_mode",
        "input_digest", "maximum_bytes", "physical_writer", "relative_path", "schema_or_template_version", "semantic_owner",
        "source_digest", "update_class", "validation_requirements"}
    for entry in artifacts:
        row = closed(entry, fields)
        path = row["relative_path"]
        if not isinstance(path, str) or path not in PATHS - {"engineering/bootstrap-manifest.json"} or path in seen:
            raise BootstrapError("BOOTSTRAP_ARTIFACT_SET_INVALID")
        seen.add(path)
        if (type(row["bytes"]) is not int or row["bytes"] != len(files[path]) or row["maximum_bytes"] != 131072
                or row["file_mode"] != "0644" or row["encoding"] != "UTF-8" or row["expected_before"] != "ABSENT"
                or row["physical_writer"] != "engineering-platform" or row["schema_or_template_version"] != "pb-render/1.0"
                or row["expected_after_digest"] != digest(files[path]) or row["source_digest"] != RENDERER_SHA256
                or row["input_digest"] != manifest["input_digest"]
                or row["validation_requirements"] != ["PORTABLE_HASH_SCHEMA_PROFILE"]):
            raise BootstrapError("BOOTSTRAP_MANIFEST_FILE_CONFLICT", 409)
    source = load(files["engineering/render-input.json"])
    if (not isinstance(source, dict) or source.get("mode") != "GENESIS" or source.get("host_profile") != "LOCAL_POSIX"
            or source.get("role") != "authority" or source.get("security_profile") != "DOCUMENT_ONLY"
            or source.get("stack_profile") != "NO_PRODUCT_CODE"):
        raise BootstrapError("BOOTSTRAP_PROFILE_UNSUPPORTED", 422)
    if digest(files["engineering/render-input.json"]) != sha256(manifest["input_digest"]):
        raise BootstrapError("BOOTSTRAP_INPUT_DIGEST_CONFLICT", 409)
    if digest(files[BASE_PATH + "validate.py"]) != VALIDATOR_SHA256:
        raise BootstrapError("BOOTSTRAP_VALIDATOR_PIN_CONFLICT", 409)
    try:
        declaration = parse_repository_attachment(load(files[".engineering-platform/repository.json"]))
    except RepositoryAttachmentError as error:
        raise BootstrapError("BOOTSTRAP_DECLARATION_INVALID", 422) from error
    ids = dict(project_id=declaration.project_id, authority_repository_id=declaration.authority_repository_id,
        repository_id=declaration.repository_id)
    identity = source.get("identity")
    if (declaration.repository_role != "authority" or not isinstance(identity, dict)
            or {key: identity.get(key) for key in ids} != ids):
        raise BootstrapError("BOOTSTRAP_IDENTITY_CONFLICT", 409)
    return Bundle(files, digest(canonical(root)), manifest["manifest_digest"], manifest["input_digest"], **ids)


def request(value: object) -> tuple[dict, Bundle]:
    root = closed(value, {"contract_version", "operation_id", "target_ref", "effect_plan_binding", "bundle"})
    if root["contract_version"] != CONTRACT:
        raise BootstrapError("BOOTSTRAP_CONTRACT_UNSUPPORTED", 422)
    identifier(root["operation_id"])
    identifier(root["target_ref"])
    plan = closed(root["effect_plan_binding"], PLAN_FIELDS)
    if type(plan["plan_revision"]) is not int or not 1 <= plan["plan_revision"] <= 1000000:
        raise BootstrapError("BOOTSTRAP_PLAN_REVISION_INVALID")
    for key in PLAN_FIELDS - {"plan_revision"}:
        (sha256 if key.endswith("_digest") else identifier)(plan[key])
    return root, bundle(root["bundle"])
