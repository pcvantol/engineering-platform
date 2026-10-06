"""FME composition in EngineeringRunner's existing CENTRAL transaction.

The provider only proposes effects. The host owns artifacts, deterministic
controls, independent assurance and the existing managed publication service.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import site
from types import SimpleNamespace

from . import effect_contract as contract
from . import effect_evidence, effect_provider, effect_state, effect_validation, effect_workspace as workspace
from .agent_state import TransactionState, verified_commit_evidence_record
from .capability_review import ReviewerSelection, mandatory_assessment, mandatory_coverage_surfaces
from .execution_lease import LeaseHeartbeat, acquire, reconcile_stale, release
from .execution_repository import SubprocessRepositoryClient
from .live_status import owned_output, write_live_status
from .managed_publication import PublicationRecovery, publish_candidate
from .parity_context import project_context
from .provider_usage import ProviderInvocation, persist_provider_invocation
from .storage import (dismissal_for_run, load_admission_decision, load_submission_for_run, record_validation_profile,
                      record_validation_command_invocation, record_validation_command_terminal,
                      record_validation_control_result, sqlite_connection)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def binding(database: Path, root: Path, run_id: str, effects: dict[str, object]) -> dict[str, object]:
    from .submission_service import _accepted_request_digest
    with sqlite_connection(database) as connection:
        row = connection.execute(
            "SELECT s.submission_id,s.project_id,s.repository_id,s.producer_id,s.producer_type,"
            "s.producer_version,s.prompt_digest,s.constraints,s.correlation_id,s.mission_id,s.engineering_action_id "
            "FROM ep_submissions s JOIN ep_parity_lifecycle_dispatches d ON d.submission_id=s.submission_id "
            "WHERE d.run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            raise contract.EffectContractError("EFFECT_CANONICAL_BINDING_UNAVAILABLE")
        consumer = connection.execute("SELECT status FROM ep_consumer_registrations WHERE project_id=? AND consumer_id=?",
                                      (row[1], row[3])).fetchone()
        if consumer is None or consumer[0] != "ACTIVE":
            raise contract.EffectContractError("EFFECT_CONSUMER_AUTHORITY_INACTIVE")
        context = project_context(connection, data_root=database.parent,
                                  project_id=str(row[1]), repository_id=str(row[2]))
    if context.local_repository_root != root.resolve():
        raise contract.EffectContractError("EFFECT_REPOSITORY_BINDING_CHANGED")
    constraints = json.loads(row[7])
    if contract.parse(constraints) != effects:
        raise contract.EffectContractError("EFFECT_ACCEPTED_CONTRACT_CHANGED")
    revision = workspace.git(root, "rev-parse", "HEAD").decode().strip()
    if revision != effects["source_revision"]:
        raise contract.EffectContractError("EFFECT_SOURCE_REVISION_CHANGED")
    repository = SubprocessRepositoryClient().trusted_origin_identity(root)
    if repository is None:
        raise contract.EffectContractError("EFFECT_ORIGIN_UNTRUSTED")
    from .revision_binding import parse_repository_revision_binding
    selected_revision = parse_repository_revision_binding(constraints)
    if selected_revision is not None and selected_revision.repository_identity not in (None, repository):
        raise contract.EffectContractError("EFFECT_REPOSITORY_IDENTITY_CONFLICT")
    return {
        "run_id": run_id, "submission_id": row[0], "project_id": row[1], "repository_id": row[2],
        "producer_id": row[3], "producer_type": row[4], "producer_version": row[5],
        "correlation_id": row[8], "mission_id": row[9], "engineering_action_id": row[10],
        "repository": repository, "source_revision": revision,
        "accepted_request_digest": _accepted_request_digest(
            repository_id=row[2], producer_id=row[3], producer_type=row[4], producer_version=row[5],
            prompt_digest=row[6], constraints=constraints, correlation_id=row[8], mission_id=row[9],
            engineering_action_id=row[10]),
    }


def preflight(root: Path, database: Path, run_id: str, effects: dict[str, object]) -> object:
    """Read-only target admission; never run legacy target-write probes."""
    identity = binding(database, root, run_id, effects)
    if workspace.git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise contract.EffectContractError("EFFECT_TARGET_NOT_CLEAN")
    if SubprocessRepositoryClient().workspace_operation_active(root):
        raise contract.EffectContractError("EFFECT_TARGET_OPERATION_ACTIVE")
    directory = workspace.owned_directory(database, root, run_id)
    source = directory / "source"
    if not source.exists():
        manifest = workspace.snapshot(root, source, effects)
        workspace.immutable_json(directory / "source-manifest.json", manifest)
    else:
        manifest = workspace.read_json(directory / "source-manifest.json")
    workspace.verify_snapshot(source, manifest)
    # An actual sandbox child must run. Unsupported sandbox installations
    # block before provider execution, with no unrestricted fallback.
    forbidden = directory / "sandbox-write-probe"
    probe_code = """import errno, os, socket, sys
try:
    descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except PermissionError:
    pass
else:
    os.close(descriptor)
    raise SystemExit(42)
try:
    socket.create_connection(('127.0.0.1', 1), timeout=1)
except PermissionError:
    pass
except OSError:
    raise SystemExit(43)
else:
    raise SystemExit(44)
"""
    probe = subprocess.run(workspace.sandbox_command(source, (sys.executable, "-c", probe_code, str(forbidden))),
                           env=workspace.child_environment(), capture_output=True,
                           timeout=30, check=False)
    forbidden.unlink(missing_ok=True)
    if probe.returncode:
        raise contract.EffectContractError("EFFECT_SANDBOX_UNAVAILABLE")
    bindings = []
    if effects["mode"] == "BOUNDED_REPOSITORY_CHANGE":
        bindings.append({"validation_id": "repository_json", "category": "repository_json",
                         "command": list(effect_validation.repository_command(root))})
    accepted = load_submission_for_run(root, run_id, central_database=database)
    selected = (accepted.get("constraints") or {}).get("validation_profile")
    if selected is not None:
        from .validation_profile import resolve_producer_profile, profile_control_bindings
        profile, _ = resolve_producer_profile(selected)
        bindings.extend({"validation_id": item["validation_id"], "category": item["category"],
                         "command": item["command"]} for item in profile_control_bindings(profile, repository_root=root))
    if effects["delivery"] == "EVIDENCE_ONLY" and bindings:
        # Required consumer controls need their own pinned checkout. Explicit
        # producer profiles are preserved; admission cannot silently omit them.
        raise contract.EffectContractError("EFFECT_REPORT_PROFILE_REQUIRES_REPOSITORY_CONTROLS")
    ids = [item["validation_id"] for item in bindings]
    if len(set(ids)) != len(ids) or set(ids).intersection(effect_validation.control_ids(effects)):
        raise contract.EffectContractError("EFFECT_CONTROL_IDENTITY_CONFLICT")
    return SimpleNamespace(timestamp=now(), identity=identity, manifest=manifest, validation_bindings=bindings, checks=tuple(
        SimpleNamespace(identifier=name, outcome="PASS", reason="Observed before provider dispatch")
        for name in ("effect_binding", "effect_clean_source", "effect_owned_storage", "effect_sandbox")
    ))


def _save(host, state: TransactionState, **changes) -> TransactionState:
    state = replace(state, **changes)
    host.store.save(state)
    write_live_status(host.root, state, state.next_action)
    return state


def _update_attempt(host, state, **changes):
    checkpoint = json.loads(json.dumps(state.effect_execution))
    checkpoint["attempts"][-1].update(changes)
    return _save(host, state, effect_execution=checkpoint)


def _report_path(directory: Path, ordinal: int) -> Path:
    return directory / f"result-{ordinal}.json"


def _load_result(directory: Path, state: TransactionState) -> dict[str, object]:
    checkpoint = state.effect_execution
    attempt = checkpoint["attempts"][-1]
    receipt = attempt["result"]
    if receipt is None:
        raise contract.EffectContractError("EFFECT_PROVIDER_RESULT_UNCERTAIN")
    envelope = workspace.read_json(_report_path(directory, attempt["ordinal"]), receipt["sha256"])
    if (not isinstance(envelope, dict) or set(envelope) != {"contract_version", "artifact_type", "binding", "contract",
            "contract_digest", "source_manifest", "source_manifest_digest", "invocation_id", "repair_ordinal", "result"}
            or envelope["contract_version"] != "1.0" or envelope["artifact_type"] != "EP_EFFECT_RESULT"
            or envelope["contract_digest"] != contract.digest(checkpoint["contract"])
            or envelope["source_manifest_digest"] != contract.digest(checkpoint["manifest"])
            or envelope["binding"] != checkpoint["identity"] or envelope["contract"] != checkpoint["contract"]
            or envelope["source_manifest"] != checkpoint["manifest"]
            or envelope["invocation_id"] != attempt["invocation_id"]
            or envelope["repair_ordinal"] != attempt["ordinal"]):
        raise contract.EffectContractError("EFFECT_RESULT_BINDING_MISMATCH")
    contract.validate_result(checkpoint["contract"], envelope["result"], set(checkpoint["manifest"]))
    return envelope


def _candidate(directory: Path, root: Path, state: TransactionState, result: dict[str, object]) -> tuple[Path, str]:
    attempt = state.effect_execution["attempts"][-1]
    destination = directory / f"delivery-{attempt['ordinal']}"
    branch = f"codex/effect-{state.run_id}-{attempt['ordinal']}"
    revision = state.effect_execution["contract"]["source_revision"]
    if not destination.exists():
        workspace.git(directory, "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(root), str(destination))
        workspace.git(destination, "config", "core.hooksPath", "/dev/null")
        workspace.git(destination, "config", "user.name", "Engineering Platform")
        workspace.git(destination, "config", "user.email", "engineering-platform@users.noreply.github.com")
        workspace.git(destination, "remote", "set-url", "origin", f"https://github.com/{state.repository}.git")
        workspace.git(destination, "checkout", "-b", branch, revision)
        for item in result["files"]:
            workspace.write_file(destination, item["path"], item["content"].encode("utf-8"))
            blob = workspace.git(destination, "hash-object", "-w", "--stdin", input_bytes=item["content"].encode()).decode().strip()
            workspace.git(destination, "update-index", "--add", "--cacheinfo", "100644", blob, item["path"])
        tree = workspace.git(destination, "write-tree").decode().strip()
        if tree == workspace.git(destination, "rev-parse", f"{revision}^{{tree}}").decode().strip():
            raise contract.EffectContractError("EFFECT_GIT_OUTPUT_UNCHANGED")
        commit = workspace.git(destination, "commit-tree", tree, "-p", revision,
                               input_bytes=f"Bounded effect result for {state.run_id}\n".encode()).decode().strip()
        workspace.git(destination, "update-ref", f"refs/heads/{branch}", commit, revision)
    evidence = SubprocessRepositoryClient().inspect(destination)
    paths = workspace.git(destination, "diff", "--name-only", revision, "HEAD").decode().splitlines()
    if (not evidence.clean or evidence.branch != branch
            or set(paths) != {item["path"] for item in result["files"]}
            or (attempt["candidate_sha"] is not None and attempt["candidate_sha"] != evidence.head_sha)):
        raise contract.EffectContractError("EFFECT_CANDIDATE_CHANGED")
    for item in result["files"]:
        if workspace.git(destination, "show", f"HEAD:{item['path']}") != item["content"].encode():
            raise contract.EffectContractError("EFFECT_CANDIDATE_BYTES_CHANGED")
    return destination, evidence.head_sha


def _provider_receipt(host, state, role: str, invocation_id: str, started: str, ordinal: int):
    ended = now()
    persist_provider_invocation(host.root, ProviderInvocation(
        run_id=state.run_id, ordinal=ordinal, provider="codex_cli", model=None,
        phase=state.phase, role=role, started_at=started, completed_at=ended,
        duration_ms=max(0, round((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds() * 1000)),
        usage=getattr(host.agent, "last_usage", {}), invocation_id=invocation_id,
        runtime_metadata=getattr(host.agent, "last_runtime_metadata", {}),
        retry_ordinal=state.repair_iterations,
    ), central_database=host.store.central_database)
    return ended


def _controls(host, state, directory, delivery):
    checkpoint = state.effect_execution
    attempt = checkpoint["attempts"][-1]
    source = directory / "source"
    report = _report_path(directory, attempt["ordinal"])
    package = Path(__file__).resolve().parent.parent
    commands = [(name, "host_control", (sys.executable, "-m", "engineering_platform.effect_validation", name,
                 str(report), attempt["result"]["sha256"], str(source)), directory)
                for name in effect_validation.control_ids(checkpoint["contract"])]
    for selected in checkpoint["validation_bindings"]:
        if delivery is None:
            raise contract.EffectContractError("EFFECT_REQUIRED_CONTROL_WORKSPACE_UNAVAILABLE")
        commands.append((selected["validation_id"], selected["category"], tuple(selected["command"]), delivery))
    bindings = tuple({"validation_id": name, "required": True, "category": authority,
                      "control_identity": name, "command": list(command)} for name, authority, command, _ in commands)
    profile_digest = effect_evidence.profile_digest(checkpoint, attempt)
    effect_evidence.verify(host.store.central_database, checkpoint, directory)
    record_validation_profile(host.root, run_id=state.run_id, selected_validation_tier=checkpoint["contract"]["mode"],
        validation_profile_version="effect-validation@1.0", required_validation_controls=tuple(item[0] for item in commands),
        control_bindings=bindings, profile_reference="effect-validation@1.0", profile_selection_source="accepted_effect_contract",
        recorded_at=now(), central_database=host.store.central_database)
    for index, (name, authority, command, cwd) in enumerate(commands):
        if index < len(attempt["controls"]):
            if attempt["controls"][index]["validation_id"] != name:
                raise contract.EffectContractError("EFFECT_CONTROL_IDENTITY_CHANGED")
            continue
        started = now()
        command_id = f"{state.run_id}:effect:{attempt['ordinal']}:{index}"
        with sqlite_connection(host.store.central_database) as connection:
            prior = connection.execute("SELECT 1 FROM execution_validation_command_invocations WHERE command_id=?", (command_id,)).fetchone()
        if prior:
            raise contract.EffectContractError("EFFECT_VALIDATION_RESULT_UNCERTAIN")
        record_validation_command_invocation(host.root, run_id=state.run_id, validation_id=name,
            command_id=command_id, category=authority, control_identity=name, required_for_profile=True,
            started_at=started, currentness=attempt["ordinal"], central_database=host.store.central_database)
        scratch = directory / f"validation-scratch-{attempt['ordinal']}-{index}"
        scratch.mkdir(mode=0o700, exist_ok=True)
        environment = workspace.child_environment() | {
            "PYTHONPATH": os.pathsep.join((str(package), *site.getsitepackages())), "TMPDIR": str(scratch)}
        exit_code = workspace.run_control(workspace.sandbox_command(cwd, command, scratch=scratch, readable=(directory, package)),
                                          environment)
        ended = now()
        record_validation_command_terminal(host.root, run_id=state.run_id, command_id=command_id, completed_at=ended,
            exit_code=exit_code, central_database=host.store.central_database)
        passed = exit_code == 0
        record_validation_control_result(host.root, run_id=state.run_id, validation_id=name, category=authority,
            control_identity=name, required_for_profile=True, execution_status="EXECUTED", result="PASS" if passed else "FAIL",
            evidence_ref="command_terminal", observed_at=ended, currentness=attempt["ordinal"], central_database=host.store.central_database)
        receipt = {"validation_id": name, "authority": authority, "command_id": command_id,
                   "started_at": started, "completed_at": ended, "exit_code": exit_code,
                   "profile_digest": profile_digest, "status": "PASS" if passed else "FAIL"}
        state = _update_attempt(host, state, controls=[*state.effect_execution["attempts"][-1]["controls"], receipt])
    return state, profile_digest


def run(host, prompt_path: Path, run_id: str, accepted: dict[str, object], *, resume: bool, owner_authorized: bool):
    effects = contract.parse(accepted["constraints"])
    database = host.store.central_database
    if database is None or effects is None:
        raise contract.EffectContractError("EFFECT_CENTRAL_AUTHORITY_REQUIRED")
    directory = workspace.owned_directory(database, host.root, run_id)
    state = host.store.load(run_id) if resume else None
    with owned_output(directory / "status"):
        if dismissal_for_run(host.root, run_id):
            raise contract.EffectContractError("EFFECT_EXECUTION_DISMISSED")
        if state is not None and state.terminal:
            if state.phase == "COMPLETE":
                _load_result(directory, state)
                effect_evidence.verify(database, state.effect_execution, directory)
            workspace.cleanup_scratch(directory)
            return state
        admission = load_admission_decision(host.root, run_id, central_database=database)
        if not admission or admission["decision"] != "PASS":
            raise contract.EffectContractError("EFFECT_ADMISSION_REQUIRED")
        evidence = preflight(host.root, database, run_id, effects)
        checkpoint = {"version": "1.0", "identity": evidence.identity, "contract": effects,
                      "manifest": evidence.manifest, "validation_bindings": evidence.validation_bindings, "attempts": []}
        if state is None:
            if run_id in host.store.run_ids():
                raise contract.EffectContractError("EFFECT_EXISTING_RUN_REQUIRES_RESUME")
            state = TransactionState(run_id, evidence.identity["repository"], str(prompt_path), "INITIALIZE",
                owner_authorized=owner_authorized, effect_execution=checkpoint,
                action_intent="VALIDATION_ONLY" if effects["delivery"] == "EVIDENCE_ONLY" else "MUTATING_DELIVERY",
                requested_repository_revision=effects["source_revision"], execution_baseline_sha=effects["source_revision"],
                admission_decision="PASS", admission_completed_at=admission["observed_at"], admission_evidence_source="WATCHER",
                merge_delegation_id=host._accepted_merge_delegation_id(accepted))
            host.store.save(state)
        elif (state.effect_execution is None or state.effect_execution["identity"] != evidence.identity
              or state.effect_execution["contract"] != effects or state.effect_execution["manifest"] != evidence.manifest
              or state.effect_execution["validation_bindings"] != evidence.validation_bindings):
            raise contract.EffectContractError("EFFECT_CHECKPOINT_BINDING_CHANGED")
        reconcile_stale(host.root, central_database=database)
        lease = acquire(host.root, run_id, identity=host.host_identity, instance_id=host.host_instance_id,
                        process_id=os.getpid(), central_database=database)
        heartbeat = LeaseHeartbeat(host.root, lease, central_database=database)
        heartbeat.start()
        host.lease_heartbeat = heartbeat
        try:
            state = host.store.load(run_id)
            return _execute(host, state, directory, prompt_path, accepted)
        except (contract.EffectContractError, OSError, ValueError, subprocess.SubprocessError) as error:
            state = host.store.load(run_id)
            return _save(host, state, phase="BLOCKED", terminal=True,
                         next_action="inspect_effect_execution", diagnostic=(str(error) if isinstance(error, contract.EffectContractError)
                                                                             else "EFFECT_EXECUTION_EVIDENCE_UNAVAILABLE"))
        finally:
            host.lease_heartbeat = None
            lease = heartbeat.stop()
            release(host.root, lease, central_database=database)
            workspace.cleanup_scratch(directory)


def _authority(host, state):
    host._heartbeat()
    host._require_provider_dispatch_admission(state)
    if (dismissal_for_run(host.root, state.run_id)
            or binding(host.store.central_database, host.root, state.run_id, state.effect_execution["contract"])
                != state.effect_execution["identity"]):
        raise contract.EffectContractError("EFFECT_EXECUTION_AUTHORITY_CHANGED")


def _execute(host, state, directory, prompt_path, accepted):
    effects = state.effect_execution["contract"]
    source = directory / "source"
    options = None
    while True:
        _authority(host, state)
        checkpoint = state.effect_execution
        if not checkpoint["attempts"] or len(checkpoint["attempts"]) <= state.repair_iterations:
            binding(host.store.central_database, host.root, state.run_id, effects)
            state = host._provider_readiness_gate(state, require_codex=True, require_github=effects["delivery"] == "GIT")
            if state.next_action == "provider_auth_repair_required":
                return state
            options = effect_provider.policy(host.agent, source)
            workspace.verify_snapshot(source, checkpoint["manifest"])
            ordinal = state.repair_iterations
            invocation = f"{state.run_id}:effect:{ordinal}"
            attempt = {"ordinal": ordinal, "invocation_id": invocation, "started_at": now(),
                       "result": None, "controls": [], "reviews": [], "candidate_sha": None}
            checkpoint = {**checkpoint, "attempts": [*checkpoint["attempts"], attempt]}
            state = _save(host, state, effect_execution=checkpoint, phase="EXECUTE_AGENT", next_action="propose_bounded_effects")
            prior = checkpoint["attempts"][:-1]
            result = effect_provider.propose(host.agent, source, directory, options, {
                "objective": prompt_path.read_text(), "effect_contract": effects, "binding": checkpoint["identity"],
                "source_manifest": checkpoint["manifest"], "prior_reviews": [item["reviews"] for item in prior],
                "prior_controls": [item["controls"] for item in prior],
            })
            _provider_receipt(host, state, "implementation", invocation, attempt["started_at"], ordinal * 3 + 1)
            result = contract.validate_result(effects, result, set(checkpoint["manifest"]))
            envelope = {"contract_version": "1.0", "artifact_type": "EP_EFFECT_RESULT", "binding": checkpoint["identity"],
                        "contract": effects, "contract_digest": contract.digest(effects), "source_manifest": checkpoint["manifest"],
                        "source_manifest_digest": contract.digest(checkpoint["manifest"]), "invocation_id": invocation,
                        "repair_ordinal": ordinal, "result": result}
            digest = workspace.immutable_json(_report_path(directory, ordinal), envelope)
            effect_evidence.register(host.store.central_database, checkpoint, _report_path(directory, ordinal),
                artifact_id=f"effect-result:{state.run_id}:{ordinal}", artifact_type="EP_EFFECT_RESULT",
                fingerprint=digest, created_at=attempt["started_at"])
            state = _update_attempt(host, state, result={"artifact_id": f"effect-result:{state.run_id}:{ordinal}", "sha256": digest})
        envelope = _load_result(directory, state)
        delivery = None
        if effects["delivery"] == "GIT":
            delivery, sha = _candidate(directory, host.root, state, envelope["result"])
            state = _update_attempt(host, state, candidate_sha=sha)
            state = _save(host, state, branch=f"codex/effect-{state.run_id}-{state.repair_iterations}", implementation_head_sha=sha)
        state = _save(host, state, phase="LOCAL_REPOSITORY_VALIDATION", next_action="validate_effect_result")
        state, profile_digest = _controls(host, state, directory, delivery)
        state = _reviews(host, state, directory, envelope, profile_digest, options)
        attempt = state.effect_execution["attempts"][-1]
        effect_evidence.verify(host.store.central_database, state.effect_execution, directory)
        passed = effect_evidence.qualified(state.effect_execution)
        if not passed:
            if state.repair_iterations >= 3:
                return _save(host, state, phase="FAILED", terminal=True, next_action="effect_repair_budget_exhausted")
            state = _save(host, state, repair_iterations=state.repair_iterations + 1, phase="REPAIR_AGENT", next_action="repair_bounded_effects")
            continue
        binding(host.store.central_database, host.root, state.run_id, effects)
        _load_result(directory, state)
        if effects["delivery"] == "EVIDENCE_ONLY":
            return _save(host, state, phase="COMPLETE", terminal=True, next_action="effect_report_qualified",
                         terminal_condition="effect_report_qualified")
        return _publish(host, state, directory, delivery, profile_digest)


def _reviews(host, state, directory, envelope, profile_digest, options):
    state = _save(host, state, phase="QUALITY_CONTROL_AGENT", next_action="review_effect_result")
    for index, role in enumerate(("quality", "security")):
        _authority(host, state)
        checkpoint = state.effect_execution
        attempt = checkpoint["attempts"][-1]
        subject = effect_state.subject(checkpoint, attempt)
        if index < len(attempt["reviews"]):
            existing = attempt["reviews"][index]
            if existing["reviewer"] != role or existing["subject"] != subject or existing["profile_digest"] != profile_digest:
                raise contract.EffectContractError("EFFECT_REVIEW_IDENTITY_CHANGED")
            continue
        state = host._provider_readiness_gate(state, require_codex=True, require_github=False)
        if state.next_action == "provider_auth_repair_required":
            raise contract.EffectContractError("EFFECT_REVIEW_PROVIDER_UNAVAILABLE")
        options = options or effect_provider.policy(host.agent, directory / "source")
        workspace.verify_snapshot(directory / "source", checkpoint["manifest"])
        prior_ids = tuple(sorted({finding["id"] for previous in checkpoint["attempts"][:-1]
            for review in previous["reviews"] if review["reviewer"] == role
            for finding in review["findings"] if finding["blocking"]}))
        selection = ReviewerSelection(role, "Independent effect-result assurance", 1.0,
            required_coverage_surfaces=mandatory_coverage_surfaces(role, "EFFECT_RESULT"),
            required_finding_ids=prior_ids)
        invocation = f"{state.run_id}:{role}:effect:{attempt['ordinal']}"
        fence = directory / f"review-{attempt['ordinal']}-{role}-started.json"
        if fence.exists():
            raise contract.EffectContractError("EFFECT_REVIEW_RESULT_UNCERTAIN")
        started = now()
        workspace.immutable_json(fence, {"invocation_id": invocation, "subject": subject, "started_at": started})
        objective = json.dumps({"reviewer": role, "instruction": "Review this exact effect result against every accepted criterion. "
            "Assess substantive usefulness, source relevance, completeness, prohibited effects, sensitive content "
            "and the required controls. Generic COMPLETE, empty/no-op output, irrelevant evidence and omitted "
            "design alternatives are findings. Every coverage evidence_ref must start with the subject_digest. "
            "Prior blockers need explicit dispositions. Do not change files or invoke remote services.",
            "subject": subject, "result_envelope": envelope, "controls": attempt["controls"],
            "prior_reviews": [item["reviews"] for item in checkpoint["attempts"][:-1]]})
        with effect_provider.scoped(directory, options, objective):
            observed = host.agent.review(directory / "source", selection, objective)
        ended = _provider_receipt(host, state, role, invocation, started, attempt["ordinal"] * 3 + index + 2)
        _load_result(directory, state)
        assessment = mandatory_assessment(observed, delivery_role="EFFECT_RESULT", prior_finding_ids=prior_ids)
        findings, coverage, dispositions = assessment or ((), (), ())
        normalized = [{**finding, "fingerprint": contract.digest(finding), "blocking": True,
                       "disposition": "OPEN"} for finding in findings]
        passed = (assessment is not None and not findings
                  and all(item["disposition"] == "RESOLVED" for item in dispositions)
                  and all(item["status"] == "REVIEWED" and item["evidence_ref"].startswith(subject["subject_digest"])
                          for item in coverage))
        receipt = {"reviewer": role, "status": "PASS" if passed else "FAIL", "subject": subject,
                   "profile_digest": profile_digest, "invocation_id": invocation, "contract_version": "3.0",
                   "started_at": started, "completed_at": ended, "findings": normalized,
                   "coverage": list(coverage), "finding_dispositions": list(dispositions)}
        path = directory / f"review-{attempt['ordinal']}-{role}.json"
        fingerprint = workspace.immutable_json(path, receipt)
        effect_evidence.register(host.store.central_database, checkpoint, path,
            artifact_id=f"effect-review:{state.run_id}:{attempt['ordinal']}:{role}", artifact_type="EP_EFFECT_REVIEW",
            fingerprint=fingerprint, created_at=started)
        state = _update_attempt(host, state, reviews=[*attempt["reviews"], receipt])
    return state


def _publish(host, state, directory, delivery, profile_digest):
    _authority(host, state)
    attempt = state.effect_execution["attempts"][-1]
    subject = effect_state.subject(state.effect_execution, attempt)
    assurance_digest = "sha256:" + contract.digest({"subject": subject, "validation_profile_digest": profile_digest})
    profile = {"version": "effect-assurance@1.0", "candidate_sha": attempt["candidate_sha"],
               "digest": assurance_digest, "criteria_digest": subject["criteria_digest"],
               "validation_profile_digest": profile_digest, "base_sha": subject["source_revision"]}
    reviews = tuple({**{key: value for key, value in receipt.items() if key not in {"subject", "profile_digest"}},
                     "candidate_sha": attempt["candidate_sha"], "profile_digest": assurance_digest}
                    for receipt in attempt["reviews"])
    state = _save(host, state, assurance_profile=profile, assurance_reviews=reviews)
    if state.pull_request is None:
        try:
            state, number = publish_candidate(state=state, store=host.store, root=delivery,
                                              repository=host.repository, github=host.github)
        except PublicationRecovery as error:
            return _save(host, error.state, phase="WAIT_FOR_TERMINAL_EVIDENCE" if error.pending else "BLOCKED",
                         terminal=not error.pending, next_action=str(error))
        state = _save(host, state, pull_request=number, implementation_pull_request=number,
                      implementation_branch=state.branch, phase="WAIT_FOR_OPERATOR_MERGE", next_action="wait_for_protected_merge")
    observed = host.github.pull_request(state.pull_request)
    if (observed.head_sha != attempt["candidate_sha"] or observed.head_branch != state.branch
            or observed.base_branch != "main"):
        raise contract.EffectContractError("EFFECT_PUBLICATION_IDENTITY_CHANGED")
    if observed.state == "OPEN":
        if observed.is_draft:
            host.github.ready(state.pull_request)
            observed = host.github.pull_request(state.pull_request)
        delegated = host._attempt_delegated_merge(state, observed)
        if delegated is not None:
            state = delegated
            observed = host.github.pull_request(state.pull_request)
        if observed.state == "OPEN":
            return _save(host, state, phase="WAIT_FOR_OPERATOR_MERGE", next_action="wait_for_protected_merge")
    if (observed.state != "MERGED" or not observed.merge_commit
            or not observed.checks_terminal or not observed.checks_passed):
        raise contract.EffectContractError("EFFECT_PROTECTED_MERGE_UNVERIFIED")
    # Reconciliation reads the protected main in the owned checkout. The
    # accepted target checkout and its branch remain untouched.
    host.repository.refresh_main_reference(delivery)
    if not host.repository.remote_main_contains(delivery, observed.merge_commit):
        raise contract.EffectContractError("EFFECT_MERGE_NOT_ON_PROTECTED_MAIN")
    envelope = _load_result(directory, state)
    for item in envelope["result"]["files"]:
        if workspace.git(delivery, "show", f"{observed.merge_commit}:{item['path']}") != item["content"].encode():
            raise contract.EffectContractError("EFFECT_MERGED_OUTPUT_MISMATCH")
    return _save(host, state, implementation_merge_commit=observed.merge_commit,
                 commit_evidence=(*state.commit_evidence, verified_commit_evidence_record(
                     phase="WAIT_FOR_OPERATOR_MERGE", observed_at=now(), commit_sha=observed.merge_commit,
                     description="implementation_merge_verified")),
                 implementation_changed_paths=tuple(item["path"] for item in envelope["result"]["files"]),
                 last_verified_sha=observed.merge_commit, phase="COMPLETE", terminal=True,
                 next_action="effect_delivery_qualified", terminal_condition="effect_delivery_qualified")
