from pathlib import Path
import json
from contextlib import contextmanager, redirect_stdout
import hashlib
import http.client
import io
import os
import threading
import time
import tempfile
import unittest
from unittest.mock import patch
from engineering_platform import execution_progress as progress, storage, server, execution_progress_authority as authority
from engineering_platform import execution_progress_cli as cli
from importlib.resources import files
from jsonschema import Draft202012Validator, FormatChecker
from execution_progress_fixture import Fixture


class ExecutionProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(delete=False)
        self.addCleanup(self.cleanup_store)
        self.fixture = Fixture(Path(self.tmp.name))
        self.token = self.fixture.issue()["credential"]

    def cleanup_store(self):
        if getattr(self,"listener_stopped",True):
            self.tmp.cleanup()

    def read(self, project="alpha", mission="mission", run=None, **kwargs):
        out = []
        progress.read(self.fixture.data, token=self.token, headers=self.fixture.headers,
                      query={"mission_id": [mission], **kwargs}, project_id=project, run_id=run,
                      publish=lambda body, instance, check: (check(), out.append(body)))
        Draft202012Validator(json.loads(files("engineering_platform").joinpath("schemas/workspace-execution-progress-v1.schema.json").read_text()), format_checker=FormatChecker()).validate(out[0])
        return out[0]

    def test_queue_and_healthy_empty_are_different_from_denied(self):
        sub = self.fixture.submit()
        before = self.fixture.database.read_bytes()
        value = self.read()
        self.assertEqual(value["records"][0]["submission_id"], sub)
        self.assertIsNone(value["records"][0]["run_id"])
        self.assertEqual(value["records"][0]["progress"]["activity"], "QUEUED")
        self.assertEqual(self.read(mission="empty")["records"], [])
        with self.assertRaises(progress.ProgressError):
            self.read(project="beta")
        self.assertEqual(before, self.fixture.database.read_bytes())

    def test_actual_owning_writer_phases_and_operator_wait(self):
        run = self.fixture.claim(self.fixture.submit())
        for phase in ("INITIALIZE", "EXECUTE_AGENT", "QUALITY_CONTROL_AGENT"):
            self.fixture.phase(run, phase)
        value = self.read(run=run)["records"][0]
        self.assertEqual(value["progress"]["current_phase"], "QUALITY_CONTROL_AGENT")
        self.assertEqual(value["progress"]["activity"], "PROCESSING")
        self.fixture.phase(run, "WAIT_FOR_OPERATOR_MERGE", pull_request=1)
        value = self.read(run=run)["records"][0]
        self.assertEqual(value["progress"]["activity"], "WAITING")
        self.assertEqual(value["progress"]["responsible_role"], "OPERATOR")
        self.fixture.phase(run, "FINALIZE_AGENT", implementation_pull_request=1)
        self.assertEqual(self.read(run=run)["records"][0]["progress"]["current_phase"], "FINALIZE_AGENT")

    def test_overview_snapshot_selects_same_exact_run(self):
        run = self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run, "INITIALIZE")
        self.fixture.submit(action="queued", correlation="other")
        page = self.read()
        detail = self.read(run=run, expected_snapshot=[page["snapshot_id"]])
        self.assertEqual(page["snapshot_id"], detail["snapshot_id"])

    def test_qualification_stays_separate_and_known_states_preserved(self):
        sub = self.fixture.submit()
        run = self.fixture.claim(sub)
        self.fixture.phase(run, "INITIALIZE")
        self.fixture.terminal(sub, run, "COMPLETE")
        with storage.sqlite_connection(self.fixture.database) as c:
            # Explicit legacy/boundary fixture. Production insert-only writer
            # validations are exercised by their existing owning tests.
            c.execute("INSERT INTO execution_run_qualification_snapshots VALUES(?,?,?,?,?,?,?)", (run,1,"snapshot","control","terminal",json.dumps({"run_id":run,"required_validation_state":"UNRESOLVED","run_qualification":"NOT_QUALIFIED"}),"2026-10-15T00:00:00+00:00"))
        result = self.read(run=run)["records"][0]["result"]
        self.assertEqual(result["technical_outcome"], "COMPLETE")
        self.assertEqual(result["qualification"]["run_qualification"], "NOT_QUALIFIED")
        self.assertEqual(result["qualification"]["required_validation_state"], "UNRESOLVED")
        self.assertEqual(result["forge_mission_acceptance"], "NOT_OWNED_BY_EP")

    def test_checkpoint_terminal_precedes_dispatcher_commit(self):
        run = self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run, "INITIALIZE")
        self.fixture.phase(run, "COMPLETE")
        self.assertEqual(self.read(run=run)["records"][0]["result"]["technical_outcome"], "COMPLETE")

    def test_conflicting_event_phase_is_not_a_reached_step(self):
        run = self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run, "INITIALIZE")
        with storage.sqlite_connection(self.fixture.database) as c:
            c.execute("INSERT INTO execution_lifecycle_events(run_id,phase,checkpoint,recorded_at) VALUES(?,?,?,?)", (run,"QUALITY_CONTROL_AGENT",json.dumps({"run_id":run,"phase":"FINALIZE_AGENT"}),"2026-10-15T00:00:00+00:00"))
        with self.assertRaisesRegex(progress.ProgressError, "PROGRESS_EVENT_CONFLICT"):
            self.read(run=run)

    def test_conflicting_root_parent_is_not_available_lineage(self):
        sub = self.fixture.submit()
        run = self.fixture.claim(sub)
        self.fixture.phase(run, "INITIALIZE")
        with storage.sqlite_connection(self.fixture.database) as c:
            # Deliberately corrupt lineage boundary; never called live execution.
            c.execute("UPDATE execution_submission_attempts SET retry_parent_submission_id=submission_id WHERE submission_id=?", (sub,))
        with self.assertRaisesRegex(progress.ProgressError, "PROGRESS_.*CONFLICT"):
            self.read(run=run)

    def test_missing_lifecycle_is_unavailable_and_never_completes_steps(self):
        run = self.fixture.claim(self.fixture.submit())
        value = self.read(run=run)["records"][0]
        self.assertFalse(value["progress"]["available"])
        self.assertEqual(value["progress"]["steps"], [])
        self.assertEqual(value["progress"]["history_completeness"], "UNAVAILABLE")

    def test_modern_acceptance_summary_and_corruption_refusal(self):
        sub=self.fixture.submit(modern=True,summary="Bounded safe engineering action")
        value=self.read()["records"][0]
        self.assertEqual(value["description"],"Bounded safe engineering action")
        with storage.sqlite_connection(self.fixture.database) as c:
            # Deliberately modified accepted provenance; immutable receipt is
            # retained and must detect the mismatch instead of reauthorizing it.
            c.execute("UPDATE ep_submissions SET constraints=json_set(constraints,'$.forge_execution.mission_revision','2') WHERE submission_id=?",(sub,))
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_ACCEPTANCE_CONFLICT"):
            self.read()

    def test_accepted_context_private_paths_are_not_exposed(self):
        self.fixture.submit(summary="Read /Users/synthetic/private/token.txt")
        value=self.read()["records"][0]
        self.assertIsNone(value["description"])
        self.assertNotIn("/Users/",json.dumps(value))

    def test_other_absolute_host_paths_are_not_exposed_in_accepted_summary(self):
        self.fixture.submit(summary="Inspect /etc/synthetic-private/config")
        value=self.read()["records"][0]
        self.assertIsNone(value["description"])

    def test_real_owner_retry_preserves_attempt_root_and_single_logical_ea(self):
        from engineering_platform.parity_lifecycle_dispatcher import retry_operator_gate
        sub=self.fixture.submit(modern=True)
        parent=self.fixture.claim(sub)
        self.fixture.phase(parent,"INITIALIZE")
        self.fixture.terminal(sub,parent,"FAILED")
        retry=retry_operator_gate(self.fixture.data,project_id="alpha",run_id=parent)
        queued=self.read()
        self.assertEqual(queued["logical_action_count"],1)
        retry_row=next(r for r in queued["records"] if r["submission_id"]==retry.submission_id)
        self.assertIsNone(retry_row["run_id"])
        self.assertEqual(retry_row["lineage"]["retry_parent_submission_id"],sub)
        run=self.fixture.claim(retry.submission_id)
        self.fixture.phase(run,"INITIALIZE")
        detail=self.read(run=run)["records"][0]
        self.assertEqual(detail["lineage"]["root_submission_id"],sub)
        self.assertEqual(detail["lineage"]["retry_parent_submission_id"],sub)
        self.assertEqual(detail["lineage"]["attempt_submission_id"],retry.submission_id)
        self.assertEqual(detail["lineage"]["retry_parent_run_id"],parent)
        self.assertEqual(detail["provenance"]["status"],"INTERNAL_RETRY_FROM_ACCEPTED_HTTP")
        self.assertEqual(detail["provenance"]["acceptance_submission_id"],sub)

    def test_internal_retry_cannot_use_a_parent_from_another_repository(self):
        from engineering_platform.parity_lifecycle_dispatcher import retry_operator_gate
        sub=self.fixture.submit(modern=True)
        parent=self.fixture.claim(sub)
        self.fixture.phase(parent,"INITIALIZE")
        self.fixture.terminal(sub,parent,"FAILED")
        retry_operator_gate(self.fixture.data,project_id="alpha",run_id=parent)
        with storage.sqlite_connection(self.fixture.database) as c:
            # Artificial corrupt legacy/boundary fixture, not a public writer.
            c.execute("UPDATE ep_submissions SET repository_id='beta' WHERE submission_id=?",(sub,))
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_RETRY_CONFLICT"):
            self.read()

    def test_contradictory_qualification_and_invalid_timestamp_never_publish(self):
        sub=self.fixture.submit()
        run=self.fixture.claim(sub)
        self.fixture.phase(run,"INITIALIZE")
        self.fixture.terminal(sub,run,"COMPLETE")
        with storage.sqlite_connection(self.fixture.database) as c:
            c.execute("INSERT INTO execution_run_qualification_snapshots VALUES(?,?,?,?,?,?,?)",(run,1,"snapshot","controls","checkpoint",json.dumps({"run_id":run,"terminal_execution_state":"FAILED","run_qualification":"NOT_QUALIFIED"}),"2026-10-15T00:00:00+00:00"))
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_QUALIFICATION_CONFLICT"):
            self.read(run=run)
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_TIMESTAMP_CORRUPT"):
            progress._timestamp("/private/host-path-is-not-a-timestamp")

    def test_unknown_legacy_is_not_modern_mission_execution(self):
        from engineering_platform import submission_service
        with storage.sqlite_connection(self.fixture.database) as c:
            submission_service.submit(c,submission_service.SubmissionRequest("alpha","alpha","forge","HUMAN","1","Unlinked explicit legacy fixture","HTTP",mission_id="mission",engineering_action_id="legacy"))
        value=self.read()
        self.assertEqual(value["records"],[])
        self.assertEqual(value["unlinked_legacy_records"],1)
        self.assertEqual(value["logical_action_count"],0)

    def test_autonomous_check_wait_and_verified_operator_resume_are_distinct(self):
        run=self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run,"INITIALIZE")
        self.fixture.phase(run,"WAIT_FOR_TERMINAL_EVIDENCE",pull_request=1)
        value=self.read(run=run)["records"][0]
        self.assertEqual(value["progress"]["wait_reason"],"AUTOMATED_CHECK_EVIDENCE")
        self.assertEqual(value["progress"]["responsible_role"],"EXECUTION_HOST")
        self.fixture.phase(run,"WAIT_FOR_OPERATOR_MERGE",pull_request=1,next_action="resume_verified_merge")
        self.assertNotEqual(self.read(run=run)["records"][0]["progress"]["responsible_role"],"OPERATOR")

    def test_real_audited_queue_disposition_is_not_generic_unstarted_work(self):
        from engineering_platform import submission_service
        for state in ("DEFERRED","QUARANTINED","DECLINED"):
            with self.subTest(state=state):
                sub=self.fixture.submit(action="ea-"+state,correlation=state,modern=True)
                with storage.sqlite_connection(self.fixture.database) as c:
                    submission_service.operator_queue_disposition(c,project_id="alpha",submission_id=sub,
                        disposition=state,reason="Private owner reason never exported",expected_state="QUEUED",
                        expected_revision=0,operation_id="operator-"+state,actor_reference="owner:synthetic-fixture")
                record=next(r for r in self.read()["records"] if r["submission_id"]==sub)
                self.assertEqual(record["submission_disposition"],state)
                self.assertEqual(record["progress"]["wait_reason"],"SUBMISSION_"+state)
                self.assertEqual(record["progress"]["responsible_role"],"OPERATOR")
                self.assertNotIn("Private owner reason",json.dumps(record))

    def test_reader_deadline_and_record_payload_limits_remain_red(self):
        self.fixture.submit()
        with patch.object(progress,"MAX_RECORDS",0):
            with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_RECORD_LIMIT"):self.read()
        with patch.object(progress,"MAX_BYTES",32):
            with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_PAYLOAD_LIMIT"):self.read()
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_SOURCE_LIMIT"):
            progress._json('x'*262145)
        sent=[]
        def delayed(body,instance,check):
            with patch.object(progress.time,"monotonic",return_value=time.monotonic()+4):check()
            sent.append(body)
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_DEADLINE"):
            progress.read(self.fixture.data,token=self.token,headers=self.fixture.headers,query={"mission_id":["mission"]},project_id="alpha",run_id=None,publish=delayed)
        self.assertEqual(sent,[])

    def test_project_disabled_authority_change_and_scope_narrowing_deny(self):
        for key in ("EP-Consumer-ID","EP-Audience","Workspace-Instance-ID"):
            with self.subTest(key=key),self.assertRaisesRegex(progress.ProgressError,"PROGRESS_SCOPE_MISMATCH"):
                progress.read(self.fixture.data,token=self.token,headers={**self.fixture.headers,key:"different"},query={"mission_id":["mission"]},project_id="alpha",run_id=None,publish=lambda *a:self.fail("No payload"))
        with storage.sqlite_connection(self.fixture.database) as c:
            c.execute("UPDATE ep_project_registrations SET status='DISABLED' WHERE project_id='alpha'")
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_PROJECT_DENIED"):self.read()
        with storage.sqlite_connection(self.fixture.database) as c:
            c.execute("UPDATE ep_project_registrations SET status='ACTIVE',attachment_contract=json_set(attachment_contract,'$.authority_repository_id','different') WHERE project_id='alpha'")
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_AUTHORITY_CHANGED"):self.read()

    def test_owning_run_instance_receipt_is_immutable(self):
        run=self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run,"INITIALIZE")
        with storage.sqlite_connection(self.fixture.database) as c:
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE ep_receipt_run_provenance SET installation_id='foreign-instance' WHERE run_id=?",(run,))
        self.assertEqual(self.read(run=run)["records"][0]["run_id"],run)

    def test_owner_rejects_invalid_and_corrupt_grants(self):
        with storage.sqlite_connection(self.fixture.database) as c:
            args=dict(grant_id="bad",instance_id=self.fixture.instance,consumer_id="workspace",workspace_instance_id="workspace-instance",selections=[self.fixture.selection],fields=sorted(authority.FIELDS),operation_id="bad-issue",ttl_seconds=900)
            for field,value in (("ttl_seconds",0),("fields",["identity"]),("selections",[]),("consumer_id","/private/path")):
                with self.subTest(field=field),self.assertRaises(authority.ProgressError):
                    authority.issue(c,**{**args,field:value})
            with self.assertRaisesRegex(authority.ProgressError,"PROGRESS_ALREADY_ISSUED"):
                authority.issue(c,**{**args,"grant_id":"grant","operation_id":"issue-grant"})
            with self.assertRaisesRegex(authority.ProgressError,"PROGRESS_GRANT_UNKNOWN"):
                authority.status(c,grant_id="missing")
            authority.revoke(c,grant_id="grant",operation_id="revoke")
            self.assertEqual(authority.revoke(c,grant_id="grant",operation_id="revoke")["status"],"REVOKED")
            with self.assertRaisesRegex(authority.ProgressError,"PROGRESS_REVOCATION_CONFLICT"):
                authority.revoke(c,grant_id="grant",operation_id="different")
            c.execute("UPDATE ep_execution_progress_grants SET expires_at='corrupt' WHERE grant_id='grant'")
            with self.assertRaisesRegex(authority.ProgressError,"PROGRESS_GRANT_CORRUPT"):
                authority.status(c,grant_id="grant")

    def test_private_owner_config_refuses_wide_permissions_links_and_nonobjects(self):
        path=Path(self.tmp.name)/"private.json";path.write_text('{}');path.chmod(0o644)
        with self.assertRaises(authority.ProgressError):cli._config(path)
        path.chmod(0o600);path.write_text('[]')
        with self.assertRaises(authority.ProgressError):cli._config(path)
        path.write_text('x'*65537)
        with self.assertRaises(authority.ProgressError):cli._config(path)
        link=Path(self.tmp.name)/"link.json";link.symlink_to(path)
        with self.assertRaises(OSError):cli._config(link)

    def test_query_contention_respects_fixed_deadline(self):
        with storage.sqlite_connection(self.fixture.database) as c:
            c.execute("BEGIN EXCLUSIVE")
            began=time.monotonic()
            with self.assertRaises(Exception):self.read()
            self.assertLess(time.monotonic()-began,3)

    def test_current_revoke_during_publication_sends_no_payload(self):
        self.fixture.submit()
        sent = []
        def publish(body, instance, check):
            with storage.sqlite_connection(self.fixture.database) as c:
                authority.revoke(c, grant_id="grant", operation_id="revoke")
            check()
            sent.append(body)
        with self.assertRaisesRegex(progress.ProgressError, "PROGRESS_INACTIVE"):
            progress.read(self.fixture.data, token=self.token, headers=self.fixture.headers, query={"mission_id":["mission"]}, project_id="alpha", run_id=None, publish=publish)
        self.assertEqual(sent, [])

    def test_actual_short_expiry_during_publication(self):
        self.token = self.fixture.issue("short", ttl=1)["credential"]
        def publish(body, instance, check):
            time.sleep(1.05)
            check()
        with self.assertRaisesRegex(progress.ProgressError, "PROGRESS_INACTIVE"):
            progress.read(self.fixture.data, token=self.token, headers=self.fixture.headers, query={"mission_id":["mission"]}, project_id="alpha", run_id=None, publish=publish)

    def test_expiry_during_final_selection_check_is_still_denied(self):
        self.token = self.fixture.issue("late-expiry", ttl=1)["credential"]
        original = authority.require_selections
        def delayed(connection, selection):
            original(connection, selection)
            time.sleep(1.05)
        sent = []
        def publish(body, instance, check):
            with patch.object(authority,"require_selections",delayed):
                check()
            sent.append(body)
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_INACTIVE"):
            progress.read(self.fixture.data,token=self.token,headers=self.fixture.headers,query={"mission_id":["mission"]},project_id="alpha",run_id=None,publish=publish)
        self.assertEqual(sent,[])

    def test_exact_run_does_not_read_another_action_corrupt_constraints(self):
        run=self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run,"INITIALIZE")
        other=self.fixture.submit(action="other",correlation="other")
        with storage.sqlite_connection(self.fixture.database) as c:
            c.execute("UPDATE ep_submissions SET constraints='corrupt unrelated fixture' WHERE submission_id=?",(other,))
        self.assertEqual(self.read(run=run)["records"][0]["run_id"],run)

    def test_current_source_update_and_selection_are_fenced(self):
        first = self.read()
        self.fixture.submit()
        with self.assertRaisesRegex(progress.ProgressError, "PROGRESS_SNAPSHOT_CONFLICT"):
            self.read(expected_snapshot=[first["snapshot_id"]])
        sent = []
        def publish(body, instance, check):
            self.fixture.submit(action="another",correlation="another")
            check()
            sent.append(body)
        with self.assertRaisesRegex(progress.ProgressError, "PROGRESS_SNAPSHOT_CONFLICT"):
            progress.read(self.fixture.data, token=self.token, headers=self.fixture.headers, query={"mission_id":["mission"]}, project_id="alpha", run_id=None, publish=publish)
        self.assertEqual(sent, [])

    def test_other_purpose_and_wrong_instance_cannot_read(self):
        from engineering_platform import project_discovery, submission_service
        with storage.sqlite_connection(self.fixture.database) as c:
            discovery = project_discovery.issue(c, grant_id="discovery",instance_id=self.fixture.instance,consumer_id="workspace",workspace_instance_id="workspace-instance",project_ids=["alpha"],fields=sorted(project_discovery.FIELDS),operation_id="other-issue",ttl_seconds=900)["credential"]
            submission_service.register_consumer(c,consumer_id="workspace",project_id="alpha")
            submission = submission_service.issue_consumer_credential(c,consumer_id="workspace",project_id="alpha")["credential"]
        for token in (None,"invalid",discovery,submission):
            with self.subTest(token_type="redacted"), self.assertRaises(progress.ProgressError):
                progress.read(self.fixture.data,token=token,headers=self.fixture.headers,query={"mission_id":["mission"]},project_id="alpha",run_id=None,publish=lambda *a:self.fail("No payload"))
        headers = dict(self.fixture.headers, **{"EP-Instance-ID":"another"})
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_SCOPE_MISMATCH"):
            progress.read(self.fixture.data,token=self.token,headers=headers,query={"mission_id":["mission"]},project_id="alpha",run_id=None,publish=lambda *a:self.fail("No payload"))

    def test_source_loss_corruption_and_symlink_do_not_create_empty_store(self):
        database = self.fixture.database
        saved = database.with_suffix(".saved")
        database.rename(saved)
        with self.assertRaises(FileNotFoundError):
            self.read()
        self.assertFalse(database.exists())
        database.symlink_to(saved)
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_SOURCE_CONFLICT"):
            self.read()
        database.unlink()
        database.write_bytes(b"actual deliberately corrupt fixture")
        with self.assertRaises(Exception):
            self.read()
        self.assertEqual(database.read_bytes(),b"actual deliberately corrupt fixture")

    def test_malformed_queries_and_oversized_events_are_explicit_errors(self):
        for query in ({"unknown":["x"]},{"mission_id":["mission","other"]}):
            with self.assertRaises(progress.ProgressError):
                progress.read(self.fixture.data,token=self.token,headers=self.fixture.headers,query=query,project_id="alpha",run_id=None,publish=lambda *a:self.fail("No payload"))
        run = self.fixture.claim(self.fixture.submit())
        self.fixture.phase(run,"INITIALIZE")
        with storage.sqlite_connection(self.fixture.database) as c:
            for i in range(101):
                c.execute("INSERT INTO execution_lifecycle_events(run_id,phase,checkpoint,recorded_at) VALUES(?,?,?,?)",(run,"EXECUTE_AGENT",json.dumps({"run_id":run,"phase":"EXECUTE_AGENT","fixture_ordinal":i}),"2026-10-15T00:00:00+00:00"))
        with self.assertRaisesRegex(progress.ProgressError,"PROGRESS_HISTORY_LIMIT"):
            self.read(run=run)

    def test_owner_cli_and_read_parity_keep_credential_private_and_no_store_creation(self):
        config = Path(self.tmp.name)/"config.json"
        setup = dict(grant_id="cli",consumer_id="workspace",workspace_instance_id="workspace-instance",selections=[self.fixture.selection],fields=sorted(authority.FIELDS),operation_id="cli-issue",ttl_seconds=900)
        config.write_text(json.dumps(setup));config.chmod(0o600)
        def call(command,*extra):
            out=io.StringIO()
            with redirect_stdout(out):code=cli.main([command,"--data-root",str(self.fixture.data),"--expected-instance-id",self.fixture.instance,*extra])
            return code,json.loads(out.getvalue())
        code,issued=call("issue","--config",str(config));self.assertEqual(code,0)
        code,status=call("status","--grant-id","cli");self.assertEqual(code,0);self.assertNotIn("credential",status)
        config.write_text(json.dumps({"headers":self.fixture.headers,"query":{"mission_id":["mission"]},"project_id":"alpha"}))
        with patch("sys.stdin",io.StringIO(issued["credential"])):
            code,result=call("read","--config",str(config),"--credential-stdin")
        self.assertEqual(code,0);self.assertEqual(result["records"],[])
        self.assertEqual(call("revoke","--grant-id","cli","--operation-id","cli-revoke")[0],0)
        self.fixture.database.rename(self.fixture.database.with_suffix(".saved"))
        self.assertEqual(call("status","--grant-id","cli")[0],2)
        self.assertFalse(self.fixture.database.exists())


class ExecutionProgressHTTPTests(ExecutionProgressTests):
    def setUp(self):
        super().setUp()
        self.listener=server._ConsoleHTTPServer(("127.0.0.1",0),server._HealthHandler)
        self.listener.daemon_threads=False
        self.listener_stopped=False
        self.listener.data_root=self.fixture.data
        self.listener.execution_progress_source_identity=progress.source_identity(self.fixture.database)
        self.thread=threading.Thread(target=self.listener.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_listener)

    def stop_listener(self):
        self.listener.shutdown();self.listener.server_close();self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.listener_stopped=True

    def request(self, path="/v1/execution-progress/projects/alpha?mission_id=mission",headers=None):
        connection=http.client.HTTPConnection("127.0.0.1",self.listener.server_port,timeout=5)
        try:
            connection.request("GET",path,headers={**self.fixture.headers,"Authorization":"Bearer "+self.token} if headers is None else headers)
            response=connection.getresponse();body=json.loads(response.read())
            self.assertEqual(response.getheader("Cache-Control"),"no-store")
            Draft202012Validator(json.loads(files("engineering_platform").joinpath("schemas/workspace-execution-progress-v1.schema.json").read_text()),format_checker=FormatChecker()).validate(body)
            return response.status,body
        finally:connection.close()

    def test_actual_http_binding_scope_replay_and_no_business_writes(self):
        run=self.fixture.claim(self.fixture.submit())
        for phase in ("INITIALIZE","EXECUTE_AGENT","QUALITY_CONTROL_AGENT","WAIT_FOR_OPERATOR_MERGE"):
            self.fixture.phase(run,phase,pull_request=1)
        before=self.fixture.database.read_bytes()
        code,page=self.request();self.assertEqual(code,200)
        code,detail=self.request(f"/v1/execution-progress/projects/alpha/runs/{run}?mission_id=mission&expected_snapshot={page['snapshot_id']}")
        self.assertEqual(code,200);self.assertEqual(detail["records"][0]["progress"]["wait_reason"],"OPERATOR_MERGE_REQUIRED")
        self.assertEqual(self.request("/v1/execution-progress/projects/beta?mission_id=mission")[0],403)
        self.assertEqual(self.request()[1]["snapshot_id"],page["snapshot_id"])
        self.assertEqual(self.fixture.database.read_bytes(),before)

    def test_actual_http_missing_and_corrupt_sources_are_not_empty_success(self):
        self.fixture.database.rename(self.fixture.database.with_suffix(".saved"))
        self.assertEqual(self.request()[0],503);self.assertFalse(self.fixture.database.exists())
        self.fixture.database.write_bytes(b"corrupt")
        self.assertEqual(self.request()[0],409)

    def test_read_grant_cannot_submit_and_duplicate_headers_are_rejected(self):
        before=self.fixture.database.read_bytes()
        c=http.client.HTTPConnection("127.0.0.1",self.listener.server_port,timeout=5)
        try:
            c.request("POST","/v1/projects/alpha/submissions",body='{}',headers={"Content-Type":"application/json","Authorization":"Bearer "+self.token})
            r=c.getresponse();r.read();self.assertEqual(r.status,401)
        finally:c.close()
        c=http.client.HTTPConnection("127.0.0.1",self.listener.server_port,timeout=5)
        try:
            c.putrequest("GET","/v1/execution-progress/projects/alpha?mission_id=mission")
            for key,value in {**self.fixture.headers,"Authorization":"Bearer "+self.token}.items():c.putheader(key,value)
            c.putheader("EP-Audience","workspace");c.endheaders()
            r=c.getresponse();r.read();self.assertEqual(r.status,400)
        finally:c.close()
        self.assertEqual(self.fixture.database.read_bytes(),before)

    def test_revocation_during_actual_response_encoding_is_caught_before_headers(self):
        original=server.json.dumps
        def encode(value,*args,**kwargs):
            if isinstance(value,dict) and value.get("purpose")=="EXECUTION_PROGRESS_READ":
                with storage.sqlite_connection(self.fixture.database) as c:
                    authority.revoke(c,grant_id="grant",operation_id="late")
            return original(value,*args,**kwargs)
        with patch.object(server.json,"dumps",encode):
            self.assertEqual(self.request()[0],401)

    def test_failed_listener_stop_preserves_store_until_actual_stop(self):
        with patch.object(self.listener,"shutdown",side_effect=RuntimeError("deliberate stop failure")):
            with self.assertRaises(RuntimeError):self.stop_listener()
        self.cleanup_store()
        self.assertTrue(self.fixture.database.is_file())
        self.assertTrue(self.thread.is_alive())

    def test_real_active_handler_is_joined_before_store_cleanup(self):
        entered,release=threading.Event(),threading.Event()
        original=progress._capture
        errors=[]
        def held(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise RuntimeError("fixture release not observed")
            return original(*args,**kwargs)
        def request():
            try:self.request()
            except BaseException as error:errors.append(error)
        with patch.object(progress,"_capture",held):
            client=threading.Thread(target=request);client.start()
            try:
                self.assertTrue(entered.wait(2))
                stopper=threading.Thread(target=self.stop_listener);stopper.start()
                time.sleep(0.1)
                self.assertFalse(self.listener_stopped)
                self.cleanup_store()
                self.assertTrue(self.fixture.database.is_file())
            finally:
                release.set();client.join(5)
                if 'stopper' in locals():stopper.join(5)
        self.assertFalse(client.is_alive());self.assertFalse(stopper.is_alive())
        self.assertTrue(self.listener_stopped);self.assertEqual(errors,[])
