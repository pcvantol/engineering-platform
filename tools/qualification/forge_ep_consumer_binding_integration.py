#!/usr/bin/env python3
"""Qualify the real Forge HTTP adapter against a real isolated EP server.

This cross-repository qualification intentionally accepts a Forge source root;
it does not make Forge a package dependency of EP.  Both runtime data roots,
both credentials and the loopback server are temporary.  No transport, EP
route, verifier, Forge binding store or HTTP adapter is mocked.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile

from engineering_platform import (
    local_repository_binding,
    project_topology,
    repository_attachment,
    server,
    submission_service,
)
from engineering_platform.storage import sqlite_connection


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request():
    from forge.models import (
        ForgeActionContextEnvelope,
        ForgePlanningContextEnvelope,
        Producer,
        ProducerContract,
        ProducerIdentity,
        ProviderPromptDefinition,
        RepositoryRevisionBinding,
        RuntimePrompt,
        RuntimePromptEnvelope,
        RuntimePromptSection,
        RuntimePromptSectionKind,
    )
    from forge.models.execution_host import ExecutionRequest

    prompt = RuntimePrompt(
        "runtime-prompt-consumer-binding", "intent-consumer-binding", "1",
        "action-consumer-binding", ProviderPromptDefinition("provider", "1"),
        "sha256:" + "a" * 64,
        tuple(RuntimePromptSection(kind, (kind.value,)) for kind in RuntimePromptSectionKind),
    )
    context = ForgeActionContextEnvelope.create(
        action_id="action-consumer-binding",
        summary="Qualify the isolated consumer identity boundary.",
        source_digest=prompt.generation_request_digest,
    )
    planning = ForgePlanningContextEnvelope.create(
        mission_id="mission-consumer-binding", mission_revision="1",
        intent_id="intent-consumer-binding", intent_revision="1",
        action_id="action-consumer-binding", mission_title="Consumer binding qualification",
        business_summary="Prove exact credential identity.",
        engineering_summary="Exercise the real Forge HTTP adapter and EP route.",
        mission_lifecycle="ACTIVE", decision_evidence_reference="qualification:isolated",
    )
    revision = RepositoryRevisionBinding(
        "a" * 40, None, "repository-truth:isolated", "sha256:" + "f" * 64,
    )
    contract = ProducerContract(
        Producer(ProducerIdentity("forge", "FORGE", "qualification")),
        "correlation-consumer-binding", "action-consumer-binding",
        RuntimePromptEnvelope(
            prompt.id, "1.0", "text/markdown", "exact isolated prompt",
            "sha256:" + "a" * 64,
        ),
        ("Execute only the supplied Runtime Prompt.",),
        (
            ("intent_id", "intent-consumer-binding"), ("intent_revision", "1"),
            ("mission_revision", "1"), ("repository_id", "forge"),
            ("workspace_id", "workspace-isolated"),
        ),
        action_context=context, planning_context=planning,
        mission_id="mission-consumer-binding", repository_revision_binding=revision,
    )
    return ExecutionRequest(
        "engineering-platform", "mission-consumer-binding", "intent-consumer-binding", "1",
        "action-consumer-binding", prompt, "workspace-isolated", "forge",
        "correlation-consumer-binding", "2026-09-16T00:00:00Z",
        producer_contract=contract, repository_revision_binding=revision,
    )


def qualify(forge_source: Path) -> dict[str, object]:
    source = forge_source.expanduser().resolve(strict=True)
    if not (source / "forge" / "scheduler" / "ep_http_adapter.py").is_file():
        raise RuntimeError("FORGE_SOURCE_INVALID")
    sys.path.insert(0, str(source))
    from forge.runtime.database import RuntimeDatabase
    from forge.scheduler.ep_http_adapter import (
        EngineeringPlatformHttpConfiguration,
        EngineeringPlatformHttpExecutionHost,
    )

    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        ep_root, forge_root = temporary_root / "ep", temporary_root / "forge"
        checkout = temporary_root / "checkout"
        attachment = {
            "schema_version": "1.0",
            "project": {"id": "forge", "authority_repository_id": "forge"},
            "repository": {"id": "forge", "role": "authority"},
            "validation": {"kind": "none", "entrypoint": None, "description": None},
            "requirements": {"host": {}, "tools": {}},
            "integrations": {},
        }
        attachment_path = repository_attachment.config_path(checkout)
        attachment_path.parent.mkdir(parents=True)
        attachment_path.write_text(json.dumps(attachment, sort_keys=True), encoding="utf-8")
        port = _port()
        identity = server.initialize(ep_root, bind_port=port)
        with sqlite_connection(ep_root / server.SERVER_DATABASE_FILENAME) as connection:
            project_topology.register_server_local_topology(connection, declaration=attachment)
            local_repository_binding.bind_local_repository(
                connection, project_id="forge", repository_id="forge",
                local_root=checkout, data_root=ep_root,
            )
            credential_a = str(submission_service.issue_consumer_credential(
                connection, consumer_id="consumer-a", project_id="forge",
            )["credential"])
            credential_b = str(submission_service.issue_consumer_credential(
                connection, consumer_id="consumer-b", project_id="forge",
            )["credential"])
        # Reuse the Server's explicit source-qualification child bridge.  This
        # changes only the child import root; the spawned server, lifecycle and
        # loopback transport remain the real implementations.
        original_argv0 = sys.argv[0]
        try:
            sys.argv[0] = "unittest-forge-ep-consumer-binding"
            server.start(ep_root)
        finally:
            sys.argv[0] = original_argv0
        bindings = RuntimeDatabase(".", path=forge_root / "forge.db", forge_version="qualification")
        try:
            common = {
                "base_url": f"http://127.0.0.1:{port}",
                "project_id": "forge", "expected_instance_id": identity.instance_id,
                "expected_consumer_id": "consumer-a", "repository_id": "forge",
                "repository_identity": "forge", "allow_loopback_http": True,
                "peer_binding_id": "isolated-ep", "peer_configuration_revision": 1,
                "peer_configuration_digest": "sha256:" + "d" * 64,
            }
            correct = EngineeringPlatformHttpExecutionHost(
                EngineeringPlatformHttpConfiguration(bearer_token=credential_a, **common),
                bindings,
            ).preflight()
            try:
                EngineeringPlatformHttpExecutionHost(
                    EngineeringPlatformHttpConfiguration(bearer_token=credential_b, **common),
                    bindings,
                ).dispatch(_request())
            except ValueError as error:
                if str(error) != "EP_AUTHENTICATED_CONSUMER_IDENTITY_MISMATCH":
                    raise
            else:
                raise RuntimeError("WRONG_CONSUMER_WAS_ACCEPTED")
            try:
                EngineeringPlatformHttpExecutionHost(
                    EngineeringPlatformHttpConfiguration(
                        bearer_token=credential_a, **{**common, "expected_consumer_id": ""},
                    ),
                    bindings,
                )
            except ValueError:
                pass
            else:
                raise RuntimeError("MISSING_EXPECTED_CONSUMER_WAS_ACCEPTED")
            with sqlite_connection(ep_root / server.SERVER_DATABASE_FILENAME) as connection:
                submissions = int(connection.execute(
                    "SELECT count(*) FROM ep_submissions"
                ).fetchone()[0])
            if submissions != 0:
                raise RuntimeError("NEGATIVE_IDENTITY_TEST_CREATED_SUBMISSION")
            return {
                "status": "PASS",
                "real_components": [
                    "engineering_platform.server",
                    "engineering_platform credential verifier and scoped compatibility route",
                    "forge.scheduler.EngineeringPlatformHttpExecutionHost",
                    "forge.runtime.RuntimeDatabase binding store",
                    "loopback HTTP transport",
                ],
                "test_doubles": [],
                "correct_consumer": correct["authentication"]["consumer_id"],
                "wrong_consumer_rejected_before_submission": True,
                "missing_expected_consumer_rejected": True,
                "ep_submission_count": submissions,
                "secret_material_reported": False,
            }
        finally:
            bindings.close()
            server.stop(ep_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forge-source", type=Path, required=True)
    result = qualify(parser.parse_args(argv).forge_source)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
