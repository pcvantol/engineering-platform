"""Synthetic owner fixture, using real admission/claim/lifecycle writers.

No live model/provider or target execution is claimed. Read consumers only use
HTTP; fixture setup is explicitly separate from the measured no-effect reads.
"""
from pathlib import Path
import json
from engineering_platform import (server, storage, project_topology,
    local_repository_binding, submission_service, execution_progress_authority as authority)
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.parity_lifecycle_dispatcher import ParityLifecycleDispatcher, _historical_admission_environment


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.data = self.base / "server"
        self.instance = server.initialize(self.data).instance_id
        self.database = self.data / "epdata.sqlite"
        self.targets = {}
        for name in ("alpha", "beta"):
            root = self.base / name
            (root / ".engineering-platform").mkdir(parents=True)
            declaration = {"schema_version": "1.0", "project": {"id": name, "authority_repository_id": name},
                           "repository": {"id": name, "role": "authority"}, "validation": {"kind": "none"}}
            (root / ".engineering-platform/repository.json").write_text(json.dumps(declaration))
            with storage.sqlite_connection(self.database) as c:
                project_topology.register_server_local_topology(c, declaration=declaration)
                local_repository_binding.bind_local_repository(c, project_id=name, repository_id=name, local_root=root, data_root=self.data)
            self.targets[name] = root
        self.headers = {"EP-Progress-Contract": "1.0", "EP-Instance-ID": self.instance,
                        "EP-Consumer-ID": "workspace", "Workspace-Instance-ID": "workspace-instance", "EP-Audience": "workspace"}
        self.selection = {"project_id": "alpha", "authority_repository_id": "alpha", "repository_ids": ["alpha"],
                          "mission_ids": ["mission", "empty"], "producer_ids": ["forge"]}

    def issue(self, grant="grant", ttl=900, selections=None):
        config = dict(grant_id=grant, consumer_id="workspace", workspace_instance_id="workspace-instance",
                      selections=[self.selection] if selections is None else selections,
                      fields=sorted(authority.FIELDS), operation_id="issue-" + grant, ttl_seconds=ttl)
        with storage.sqlite_connection(self.database) as c:
            return authority.issue(c, instance_id=self.instance, **config)

    def submit(self, action="ea", project="alpha", correlation="correlation", mission="mission", modern=False, summary=None):
        provenance = {"contract_version": "1.0", "host_id": "fixture-host", "repository_id": project,
                      "correlation_id": correlation, "mission_id": mission, "mission_revision": "1",
                      "intent_id": "intent", "intent_revision": "1", "action_id": action,
                      "runtime_prompt": {"id": "runtime-prompt", "content_digest": "sha256:" + "a" * 64},
                      "retry_of_correlation_id": None}
        if modern or summary is not None:
            provenance.update(contract_version="1.1", producer_contract_version="1.0", forge_application_version="1.0")
        if summary is not None:
            envelope = {"envelope_version":"1.0","action_id":action,"summary":summary,
                        "summary_digest":submission_service._action_context_digest(summary),
                        "generator":{"id":"forge-redacted-action-summary","model":"deterministic-template","version":"1.0","source_digest":"sha256:"+"a"*64}}
            envelope["envelope_digest"]=submission_service._action_context_digest(envelope)
            provenance.update(contract_version="1.2",action_context_envelope=envelope)
        with storage.sqlite_connection(self.database) as c:
            return submission_service.submit(c, submission_service.SubmissionRequest(
                project, project, "forge", "FORGE", "1.0", "Private fixture prompt sentinel", "HTTP",
                correlation_id=correlation, mission_id=mission, engineering_action_id=action,
                constraints={"forge_execution": provenance})).submission_id

    def claim(self, submission):
        dispatcher = ParityLifecycleDispatcher(self.data)
        context, candidate, run, prompt, duplicate = dispatcher._claim(submission)
        with _historical_admission_environment(self.targets[context.project_id], self.data):
            storage.record_submission(self.targets[context.project_id], submission_id=submission,
                producer_id=candidate.producer_id, producer_type=candidate.producer_type,
                producer_version=candidate.producer_version, contract_version="1.0",
                prompt_content=candidate.prompt, prompt_metadata={"constraints": candidate.constraints},
                target_identity={"project_id": context.project_id, "repository_id": context.repository_id},
                original_envelope=candidate.producer_envelope(), correlation_id=candidate.correlation_id,
                mission_id=candidate.mission_id, engineering_action_id=candidate.engineering_action_id,
                link_run_id=run, received_at=candidate.submitted_at,
                retry_parent_submission_id=candidate.retry_parent_submission_id)
            storage.record_run_qualification_context(self.targets[context.project_id],run_id=run,
                submission_id=submission,fresh_submission=candidate.retry_parent_run_id is None,
                retry_parent_run_id=candidate.retry_parent_run_id,resume_parent_run_id=None,
                recorded_at=candidate.submitted_at)
        dispatcher._set_state(submission, run, "RUNNING")
        return run

    def phase(self, run, phase, **kwargs):
        store = StateStore(self.data / "no-checkout", central_database=self.database, emit_local_projection=False)
        store.save(TransactionState(run, "synthetic-owner-fixture", "CENTRAL:fixture", phase,
                                    terminal=phase in {"COMPLETE", "BLOCKED", "FAILED"}, **kwargs))

    def terminal(self, submission, run, state):
        self.phase(run, state)
        ParityLifecycleDispatcher(self.data)._set_state(submission, run, state)
