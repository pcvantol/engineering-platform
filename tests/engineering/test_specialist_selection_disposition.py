"""Owning SA-SEL/SA-LOOP regression and boundary qualification."""
from pathlib import Path
import unittest

from engineering_platform.capability_review import (
    ReviewerResult, ReviewerSelection, records_for_storage, select_reviewers,
)


class SpecialistSelectionDispositionTests(unittest.TestCase):
    def test_misleading_coordinator_yaml_and_markdown_without_consumer_make_no_selection(self):
        selected = select_reviewers(
            "A coordinator updates a .yaml file.", Path("work.md"), "IMPLEMENTATION", {},
        )
        self.assertEqual(selected, ())

    def test_returned_advice_is_proposed_never_invented_acceptance(self):
        selection = ReviewerSelection("validation", "bounded question", 1.0)
        result = ReviewerResult("validation", "Advice", ("Add a negative case.",))
        record = records_for_storage((selection,), (result,))[0]
        self.assertEqual(record["accepted_recommendations"], 0)



# These fixtures use real Git, installed-compatible CENTRAL and product methods;
# only the external model/GitHub and their capacity/readiness transports double.
from dataclasses import replace
import json
import tempfile
from unittest.mock import patch
from engineering_platform import capability_review as cr
from engineering_platform.agent_state import StateStore, StateError, TransactionState
from engineering_platform.execution_host import EngineeringRunner, assemble_prompt
from engineering_platform.execution_models import AgentResult
from engineering_platform.execution_reporting import _format_specialist_dispositions
from engineering_platform.storage import load_validation_context, sqlite_connection
from engineering_platform.provider_usage import provider_usage_summary
from tests.engineering import test_managed_adoption as adoption_fixture
from tests.engineering.test_execution_host import FakeAgent, mandatory_review_result


class SpecialistPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = adoption_fixture.AdoptionLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.store = self.fixture.root, self.fixture.store
        self.fixture.git('switch', 'main')
        self.requests = [
            {'reviewer': 'documentation', 'question': 'Which README acceptance detail is absent?', 'paths': ['README.md'], 'consumer': 'EXECUTE_AGENT', 'risk': 'NORMAL'},
            {'reviewer': 'validation', 'question': 'Which documentation test edge case needs checking?', 'paths': ['tests/test_docs.py'], 'consumer': 'EXECUTE_AGENT', 'risk': 'HIGH'},
        ]
        self.objective = '# Objective\nUpdate the README only inside approved scope.\n# Safety\nNever bypass independent assurance.\nSpecialist review requests: ' + json.dumps(self.requests)
        self.fixture.prompt.write_text(self.objective)
        self.state = TransactionState('specialist-run', 'qualification/managed', str(self.fixture.prompt), 'INITIALIZE', owner_authorized=True)
        self.capacity = patch('engineering_platform.codex_capacity.read_remaining_percent', return_value=100)
        self.capacity.start();self.addCleanup(self.capacity.stop)

    def plan(self, **kwargs):
        return select_reviewers(self.objective, self.fixture.prompt, 'IMPLEMENTATION', {},
            root=self.root, run_id=self.state.run_id, repository=self.state.repository, candidate_sha=self.fixture.base,
            qualified_roles=kwargs.pop('qualified_roles', ('documentation', 'validation')),
            remaining_percent=kwargs.pop('remaining_percent', 100), reserve_percent=0, **kwargs)

    def transport(self, dispositions=None, optional_fail=False):
        fixture = self.fixture
        class Model(FakeAgent):
            qualified_specialist_roles = ('documentation', 'validation')
            def __init__(self):
                super().__init__(AgentResult('COMPLETE'))
                self.roles = [];self.selected = [];self.implementations = 0
            def review(inner, root, selected, objective, evidence=None):
                inner.roles.append(selected.reviewer)
                if selected.reviewer in {'quality', 'security'}:
                    if 'Optional non-assurance specialist findings' in objective:
                        raise AssertionError('Optional conclusions reached independent assurance')
                    return mandatory_review_result(selected.reviewer, objective)
                inner.selected.append(selected)
                path = selected.specialist_paths[0]
                return ReviewerResult(selected.reviewer, 'Bounded advice',
                    findings=({'id': 'missing-detail', 'summary': 'Add the missing acceptance sentence.', 'path': path,
                               'evidence_ref': 'git-blob:' + dict(selected.specialist_source_blobs)[path], 'proposed_disposition': 'ACCEPTED'},),
                    contract_version=cr.SPECIALIST_CONTRACT_VERSION, failed=optional_fail,
                    usage={'input_tokens': 100 if selected.reviewer == 'documentation' else 200},
                    specialist_binding=dict(selected.specialist_binding))
            def invoke(inner, root, prompt):
                inner.implementations += 1
                if inner.implementations > 1:
                    raise AssertionError('Mechanical validation/publication or implementation replay invoked model')
                state = fixture.store.load('specialist-run')
                findings = cr.specialist_readback(state.specialist_records)['findings']
                assert all(line in prompt for line in fixture.prompt.read_text().splitlines() if line.startswith('Never '))
                assert all(f['id'] in prompt and f['summary'] in prompt for f in findings)
                branch=state.branch or 'codex/specialist'
                fixture.git('switch', '-c', branch)
                (root/'README.md').write_text('# Qualified delivery\n\nAcceptance sentence.\n')
                fixture.git('add', 'README.md');fixture.git('commit', '-qm', 'bounded implementation')
                choices=[]
                for finding in findings:
                    disposition = dispositions.get(finding['reviewer']) if dispositions else ('IMPLEMENTED' if finding['path']=='README.md' else 'DEFERRED')
                    choices.append({'finding_id': finding['id'], 'disposition': disposition,
                                    'reason': 'Approved scope applied and tested.' if disposition=='IMPLEMENTED' else 'Existing test surface retained for this bounded delivery.',
                                    'changed_paths': ['README.md'] if disposition=='IMPLEMENTED' else []})
                return AgentResult('COMPLETE', branch, commit_sha=fixture.git('rev-parse', 'HEAD'), specialist_dispositions=tuple(choices))
        return Model()

    def runner(self, model=None):
        _, github = self.fixture.lifecycle_adapters()
        runner=EngineeringRunner(self.root, self.store, self.fixture.repository, github, model or self.transport(), lambda _:None)
        self.addCleanup(self.fixture.stop_host, runner)
        return runner

    def test_real_selection_primary_application_controls_assurance_and_publication_join(self):
        model=self.transport();runner=self.runner(model)
        result=runner.run(self.fixture.prompt, run_id='specialist-run', owner_authorized=True)
        self.assertEqual(result.phase, 'WAIT_FOR_OPERATOR_MERGE')
        data=cr.specialist_readback(self.store.load('specialist-run').specialist_records)
        self.assertEqual((data['reserved_invocation_count'],data['completed_invocation_count'],model.implementations),(2,2,1))
        self.assertEqual(model.roles, ['validation','documentation','quality','security'])
        by_role={f['reviewer']:f for f in data['findings']}
        self.assertEqual(by_role['documentation']['disposition'],'VERIFIED')
        self.assertEqual(by_role['validation']['disposition'],'DEFERRED')
        self.assertEqual(by_role['documentation']['result_candidate_sha'],self.fixture.git('rev-parse','HEAD'))
        context=load_validation_context(self.root,'specialist-run',central_database=self.fixture.database)
        self.assertEqual(by_role['documentation']['validation_profile_digest'],context['profile_digest'])
        self.assertEqual(set(by_role['documentation']['control_refs']),{c['command_id'] for c in context['controls'].values()})
        self.assertEqual([r['status'] for r in result.assurance_reviews],['PASS','PASS'])
        self.assertIn('VERIFIED','\n'.join(_format_specialist_dispositions(result)))
        with sqlite_connection(self.fixture.database) as connection:
            rows=connection.execute("SELECT phase,input_tokens FROM provider_invocations WHERE run_id=? AND phase='CAPABILITY_REVIEW' ORDER BY ordinal",('specialist-run',)).fetchall()
        self.assertEqual([r[1] for r in rows],[200,100])
        self.assertEqual(provider_usage_summary(self.root,'specialist-run',central_database=self.fixture.database)['provider_invocation_count'],5)

    def test_actual_duplicate_results_are_consumed_and_applied_only_once(self):
        model=self.transport();review=model.review
        def duplicate(root,selected,objective,evidence=None):
            result=review(root,selected,objective,evidence)
            return replace(result,findings=result.findings*2) if selected.reviewer not in {'quality','security'} else result
        model.review=duplicate
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        data=cr.specialist_readback(result.specialist_records)
        self.assertEqual((len(data['findings']),data['duplicate_observations']),(2,2))
        self.assertEqual(len([r for r in result.specialist_records if r['kind']=='APPLICATION']),1)
        self.assertEqual(model.implementations,1)
        self.assertEqual(provider_usage_summary(self.root,'specialist-run',central_database=self.fixture.database)['provider_invocation_count'],5)

    def test_real_accept_and_reject_are_decisions_not_implemented_or_verified(self):
        result=self.runner(self.transport({'documentation':'ACCEPTED','validation':'REJECTED'})).run(self.fixture.prompt, run_id='specialist-run', owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        self.assertEqual({f['disposition'] for f in cr.specialist_readback(result.specialist_records)['findings']},{'ACCEPTED','REJECTED'})
        self.assertFalse(any(r['kind']=='VERIFICATION' for r in result.specialist_records))

    def test_misleading_marker_and_no_consumer_make_no_optional_model_call(self):
        self.fixture.prompt.write_text('A coordinator changes .yaml within this .md prompt.')
        model=self.transport();result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(model.roles,['quality','security'])
        self.assertEqual(cr.specialist_readback(result.specialist_records)['reserved_invocation_count'],0)
        self.assertTrue(all(e['payload']['status']=='SKIPPED' for e in result.specialist_records))

    def test_real_failed_optional_transport_has_no_findings_and_no_retry(self):
        model=self.transport(optional_fail=True);result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        self.assertEqual(model.roles,['validation','documentation','quality','security'])
        self.assertEqual(cr.specialist_readback(result.specialist_records)['findings'],[])

    def interrupted_consumer(self):
        model=self.transport();runner=self.runner(model);save=self.store.save
        def checkpoint(state,**kwargs):
            value=save(state,**kwargs)
            if any(r['kind']=='CONSUMER_RESULT' for r in state.specialist_records):
                raise SystemExit('after durable consumer receipt')
            return value
        with patch.object(self.store,'save',side_effect=checkpoint),self.assertRaises(SystemExit):
            runner.run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.fixture.stop_host(runner)
        self.assertEqual(model.implementations,1)
        return model,self.store.load('specialist-run')

    def test_completed_primary_without_catalog_before_consumer_marker_blocks_no_replay(self):
        from engineering_platform import execution_host
        model=self.transport();runner=self.runner(model)
        persist=execution_host.persist_recovery_agent_result
        def interrupted(*args,**kwargs):
            persist(*args,**kwargs)
            raise SystemExit('after artifact catalog before marker')
        with patch.object(execution_host,'persist_recovery_agent_result',side_effect=interrupted),self.assertRaises(SystemExit):
            runner.run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        state=self.store.load('specialist-run')
        self.assertFalse(any(r['kind']=='CONSUMER_RESULT' for r in state.specialist_records))
        self.fixture.stop_host(runner)
        with sqlite_connection(self.fixture.database) as connection:
            connection.execute("DELETE FROM execution_artifact_records WHERE artifact_type='PROVIDER_RECOVERY_AGENT_RESULT'")
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.next_action,'specialist_consumer_result_unavailable')
        self.assertEqual(model.implementations,1)
        self.assertEqual(result.specialist_records,state.specialist_records)

    def test_consumer_resume_rejects_corrupt_actual_artifact_without_provider_replay(self):
        model,state=self.interrupted_consumer()
        receipt=next(r['payload'] for r in state.specialist_records if r['kind']=='CONSUMER_RESULT')
        artifact=self.fixture.database.parent/'artifacts/provider-recovery-results'/f"{receipt['consumer_invocation_id']}.json"
        self.assertTrue(artifact.is_file());artifact.write_text('{}\n')
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.next_action,'specialist_consumer_result_unavailable')
        self.assertEqual(model.implementations,1)
        self.assertFalse(any(r['kind']=='APPLICATION' for r in result.specialist_records))

    def test_consumer_resume_rejects_actual_candidate_drift_without_relabelling(self):
        model,state=self.interrupted_consumer()
        (self.root/'README.md').write_text('foreign later candidate\n')
        self.fixture.git('add','README.md');self.fixture.git('commit','-qm','candidate drift')
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.next_action,'specialist_consumer_result_stale')
        self.assertEqual(model.implementations,1)
        self.assertEqual(result.specialist_records,state.specialist_records)

    def test_consumer_resume_rejects_changed_question_with_same_consumed_allowance(self):
        model,state=self.interrupted_consumer()
        self.fixture.prompt.write_text(self.objective.replace('Which README acceptance detail is absent?','Which unrelated change is wanted?'))
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.next_action,'specialist_snapshot_changed')
        self.assertEqual(model.implementations,1)
        self.assertEqual(result.specialist_records,state.specialist_records)

    def test_consumer_resume_rejects_branch_drift_even_with_identical_commit(self):
        model,state=self.interrupted_consumer()
        self.fixture.git('switch','-c','codex/foreign-same-head')
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.next_action,'specialist_consumer_result_stale')
        self.assertEqual(model.implementations,1)
        self.assertEqual(result.specialist_records,state.specialist_records)

    def test_durable_consumer_resume_applies_once_without_primary_replay(self):
        model,state=self.interrupted_consumer()
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        self.assertEqual(model.implementations,1)
        self.assertEqual(len([r for r in result.specialist_records if r['kind']=='APPLICATION']),1)
        self.assertEqual(result.specialist_records[:len(state.specialist_records)],state.specialist_records)

    def assurance_resume(self,optional_fail):
        model=self.transport(optional_fail=optional_fail);review=model.review
        def interrupt(root,selected,objective,evidence=None):
            if selected.reviewer=='quality':raise SystemExit('after real controls, before assurance result')
            return review(root,selected,objective,evidence)
        model.review=interrupt;runner=self.runner(model)
        with self.assertRaises(SystemExit):
            runner.run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        state=self.store.load('specialist-run');self.fixture.stop_host(runner)
        self.assertTrue(any(r['kind']=='CONSUMER_RESULT' for r in state.specialist_records))
        controls=load_validation_context(self.root,'specialist-run',central_database=self.fixture.database)['controls']
        model.review=review
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',resume=True,owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        self.assertEqual(model.implementations,1)
        self.assertEqual(result.specialist_records,state.specialist_records)
        self.assertEqual(load_validation_context(self.root,'specialist-run',central_database=self.fixture.database)['controls'],controls)
        if optional_fail:self.assertEqual(cr.specialist_readback(result.specialist_records)['findings'],[])

    def test_assurance_resume_preserves_applied_consumer_and_controls(self):
        self.assurance_resume(False)

    def test_assurance_resume_with_no_optional_findings_preserves_primary_once(self):
        self.assurance_resume(True)

    def test_selector_relevance_capacity_capability_overflow_and_consumed_allowance(self):
        for kwargs,reason in (({'remaining_percent':None},'capacity_unknown'),({'remaining_percent':50},'mandatory_controls_assurance_and_repair_reserved'),
                              ({'qualified_roles':()},'capability_unqualified'),({'consumed_invocations':2},'finite_optional_allowance_exhausted')):
            with self.subTest(reason=reason):
                plan=self.plan(**kwargs);self.assertFalse(plan.selections)
                self.assertEqual([e['payload']['reason'] for e in plan.decisions if e['reviewer'] in {'documentation','validation'}],[reason,reason])
        old=self.objective;self.objective+='\n'+'mandatory detail '*2000
        self.assertTrue(all(e['payload']['reason']=='complete_context_overflow' for e in self.plan().decisions if e['reviewer'] in {'documentation','validation'}))
        self.objective=old
        self.assertEqual([s.reviewer for s in self.plan().selections],['validation','documentation'])

    def test_foreign_stale_binding_and_unsafe_or_scope_expanding_finding_denied(self):
        selection=replace(self.plan().selections[0],specialist_binding={**self.plan().selections[0].specialist_binding,'invocation_id':'a'*32})
        base=ReviewerResult(selection.reviewer,'Advice',findings=({'id':'finding','summary':'Add an edge case.','path':selection.specialist_paths[0],
                          'evidence_ref':'git-blob:' + dict(selection.specialist_source_blobs)[selection.specialist_paths[0]],'proposed_disposition':'ACCEPTED'},),
                          contract_version=cr.SPECIALIST_CONTRACT_VERSION,specialist_binding=dict(selection.specialist_binding))
        for key in selection.specialist_binding:
            with self.subTest(binding=key),self.assertRaises(ValueError):
                cr.specialist_findings(selection,replace(base,specialist_binding={**base.specialist_binding,key:'foreign'}))
        for key,value in (('path','outside.py'),('summary','private reasoning transcript'),('evidence_ref','https://evil.invalid/run'),('proposed_disposition','VERIFIED')):
            with self.subTest(field=key),self.assertRaises(ValueError):
                cr.specialist_findings(selection,replace(base,findings=({**base.findings[0],key:value},)))
        duplicated=replace(base,findings=base.findings*2)
        self.assertEqual(len(cr.specialist_findings(selection,duplicated)),1)
        with self.assertRaises(ValueError):
            cr.specialist_findings(selection,replace(base,findings=(base.findings[0],{**base.findings[0],'summary':'Conflicting identity.'})))

    def process_boundary(self,boundary):
        import subprocess,sys,time
        from datetime import datetime,timezone
        state=replace(self.state,repair_iterations=1 if boundary=='repair-assurance' else 2)
        self.store.save(state)
        spec=self.fixture.area/'specialist-process-input.json'
        spec.write_text(json.dumps({'root':str(self.root),'data':str(self.fixture.data),'database':str(self.fixture.database),
                                    'prompt':str(self.fixture.prompt),'remote':str(self.fixture.remote)}))
        command=(sys.executable,'-m','tests.engineering.specialist_delivery_process',str(spec))
        start=subprocess.run((*command,'start',boundary),capture_output=True,text=True,timeout=60)
        self.assertEqual(start.returncode,73,start.stdout+start.stderr)
        interrupted=self.store.load('specialist-run')
        self.assertEqual(interrupted.repair_iterations,2)
        interrupted_controls=load_validation_context(self.root,'specialist-run',central_database=self.fixture.database) if boundary in {'assurance','assurance-empty','publication','repair-assurance'} else None
        with sqlite_connection(self.fixture.database) as connection:
            expiry=connection.execute("SELECT expires_at FROM execution_run_leases WHERE run_id=? AND lease_state='ACTIVE'",('specialist-run',)).fetchone()[0]
        delay=(datetime.fromisoformat(expiry)-datetime.now(timezone.utc)).total_seconds()
        if delay>0:time.sleep(delay+.1)
        resume=subprocess.run((*command,'resume',boundary),capture_output=True,text=True,timeout=60)
        self.assertEqual(resume.returncode,0,resume.stdout+resume.stderr)
        after=self.store.load('specialist-run');data=cr.specialist_readback(after.specialist_records)
        if boundary=='noop-consumer':
            self.assertEqual((after.phase,after.pull_request,after.repair_iterations),('COMPLETE',None,2))
            self.assertEqual((self.fixture.git('branch','--show-current'),self.fixture.git('rev-parse','HEAD')),('main',self.fixture.base))
            self.assertEqual(data['findings'],[])
            calls=[json.loads(line) for line in (self.fixture.data/'specialist-model-calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum(call['role']=='implementation' for call in calls),1)
            self.assertFalse(any(call['mode']=='resume' for call in calls))
            self.assertEqual(after.specialist_records[:len(interrupted.specialist_records)],interrupted.specialist_records)
            return
        self.assertEqual((after.phase,after.pull_request,after.repair_iterations),('WAIT_FOR_OPERATOR_MERGE',71,2))
        self.assertEqual(after.specialist_records[:len(interrupted.specialist_records)],interrupted.specialist_records)
        calls=[json.loads(line) for line in (self.fixture.data/'specialist-model-calls.jsonl').read_text().splitlines()]
        self.assertEqual(sum(call['role']=='implementation' for call in calls),1)
        if boundary=='repair-assurance':
            self.assertEqual(sum(call['role']=='repair' for call in calls),1)
            receipt=next(r['payload'] for r in after.specialist_records if r['kind']=='CONSUMER_RESULT')
            self.assertNotEqual(receipt['result_candidate_sha'],self.fixture.git('rev-parse','HEAD'))
        if boundary=='dispatch':
            self.assertEqual((data['reserved_invocation_count'],data['uncertain_invocation_count']),(1,1))
            self.assertEqual(data['completed_invocation_count'],0)
            self.assertTrue(data['dispatch_skips'])
        elif boundary=='assurance-empty':
            # Returned failed advice is completed usage, never adoption.
            self.assertEqual((data['reserved_invocation_count'],data['completed_invocation_count']),(2,2))
            self.assertEqual(data['findings'],[])
            self.assertFalse(any(r['kind']=='APPLICATION' for r in after.specialist_records))
        else:
            self.assertEqual((data['reserved_invocation_count'],data['completed_invocation_count']),(2,2))
            self.assertEqual([f['disposition'] for f in data['findings']],['DEFERRED','VERIFIED'])
            self.assertEqual(len([r for r in after.specialist_records if r['kind']=='APPLICATION']),1)
        expected_resume = [] if boundary=='publication' else ['implementation','quality','security'] if boundary=='dispatch' else ['quality','security']
        self.assertEqual([call['role'] for call in calls if call['mode']=='resume'],expected_resume)
        if boundary in {'assurance','assurance-empty','publication','repair-assurance'}:
            after_controls=load_validation_context(self.root,'specialist-run',central_database=self.fixture.database)['controls']
            self.assertEqual(after_controls,interrupted_controls['controls'])
        remote=json.loads((self.fixture.data/'specialist-remote.json').read_text())
        self.assertEqual(remote['head_sha'],self.fixture.git('rev-parse','HEAD'))
        self.assertTrue(all(c['result']=='PASS' for c in load_validation_context(self.root,'specialist-run',central_database=self.fixture.database)['controls'].values()))

    def test_real_new_process_after_noop_consumer_never_invents_branch_or_replays(self):
        self.process_boundary('noop-consumer')

    def test_real_new_process_after_same_pr_repair_assurance_uses_actual_new_candidate(self):
        self.process_boundary('repair-assurance')

    def test_real_new_process_after_recovered_primary_keeps_replacement_identity(self):
        self.process_boundary('recovered')

    def test_real_managed_noop_with_empty_advice_retains_actual_clean_main(self):
        import subprocess,sys
        model=self.transport();review=model.review
        def empty(root,selection,objective,evidence=None):
            result=review(root,selection,objective,evidence)
            return replace(result,findings=()) if selection.reviewer not in {'quality','security'} else result
        model.review=empty
        def noop(root,prompt):
            model.implementations+=1
            completed=subprocess.run((sys.executable,'-m','unittest','discover','-s','tests'),cwd=root,capture_output=True,text=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            return AgentResult('COMPLETE',terminal_condition='repository_reconciled',commit_sha=self.fixture.git('rev-parse','HEAD'),
                validation_evidence=({'command':'python3 -m unittest discover -s tests','result':'PASS: actual isolated repository tests'},))
        model.invoke=noop;before=self.fixture.git('rev-parse','HEAD')
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.phase,'COMPLETE')
        self.assertEqual((model.implementations,len(model.selected)),(1,2))
        self.assertEqual((self.fixture.git('branch','--show-current'),self.fixture.git('rev-parse','HEAD')),('main',before))
        self.assertEqual(cr.specialist_readback(result.specialist_records)['findings'],[])

    def test_real_new_process_after_primary_artifact_catalog_never_replays_consumer(self):
        self.process_boundary('artifact')

    def test_commit_replace_ref_cannot_substitute_selected_source_blobs(self):
        original=self.plan()
        (self.root/'README.md').write_text('# Replacement content\n')
        self.fixture.git('add','README.md');self.fixture.git('commit','-qm','replacement commit')
        replacement=self.fixture.git('rev-parse','HEAD')
        self.fixture.git('replace',self.fixture.base,replacement)
        substituted=self.plan()
        self.assertEqual([s.specialist_source_blobs for s in substituted.selections],
                         [s.specialist_source_blobs for s in original.selections])
        self.assertNotEqual(self.fixture.git('show',self.fixture.base+':README.md'),
                            self.fixture.git('--no-replace-objects','show',self.fixture.base+':README.md'))

    def test_known_unlabelled_credentials_are_denied_and_redacted_in_actual_readback(self):
        from engineering_platform.agent_state import redact_diagnostic
        selection=self.plan().selections[0]
        selection=replace(selection,specialist_binding={**selection.specialist_binding,'invocation_id':'a'*32})
        path=selection.specialist_paths[0]
        for credential in ('ghp_'+'A'*36,'github_pat_'+'B'*30,'sk-proj-'+'C'*32,'AKIA'+'D'*16):
            self.assertNotIn(credential,redact_diagnostic('Observed '+credential))
            result=ReviewerResult(selection.reviewer,'Bounded advice',findings=({'id':'case','summary':credential,
                'path':path,'evidence_ref':'git-blob:'+dict(selection.specialist_source_blobs)[path],
                'proposed_disposition':'DEFERRED'},),contract_version=cr.SPECIALIST_CONTRACT_VERSION,
                specialist_binding=dict(selection.specialist_binding))
            with self.assertRaises(ValueError):cr.specialist_findings(selection,result)
            result=replace(result,contribution=credential,findings=())
            with self.assertRaises(ValueError):cr.specialist_findings(selection,result)

    def test_pre_cancelled_specialist_adapter_launches_no_transport(self):
        from engineering_platform.execution_executor import CodexCliClient
        from engineering_platform.providers import CodexCliProvider
        class NoCall(CodexCliProvider):
            def invoke(self,*args,**kwargs):
                raise AssertionError('Cancelled specialist launched provider')
        client=CodexCliClient(NoCall());client.set_cancellation_check(lambda:True)
        selection=self.plan().selections[0]
        result=client.review(self.root,selection,self.objective)
        self.assertTrue(result.failed)
        self.assertTrue(client._cancellation_observed)

    def test_real_bounded_provider_cancellation_reaps_owned_child_and_preserves_sibling(self):
        import subprocess,sys,os,time
        from engineering_platform.providers import CodexCliProvider,ProviderInvocationCancelled
        marker=self.fixture.area/'cancel-child.pid'
        launcher=self.fixture.area/'cancel-provider'
        launcher.write_text('#!'+sys.executable+'\nimport os,time\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text(str(os.getpid()))\ntime.sleep(30)\n')
        launcher.chmod(0o700)
        sibling=subprocess.Popen((sys.executable,'-c','import time;time.sleep(30)'),start_new_session=True)
        try:
            prefix=self.fixture.area/'cancel-managed';(prefix/'bin').mkdir(parents=True)
            (prefix/'bin/codex').symlink_to(launcher)
            with patch.dict(os.environ,{'EP_MANAGED_CODEX_CLI_PREFIX':str(prefix)}):
                provider=CodexCliProvider()
            started=time.monotonic()
            with self.assertRaises(ProviderInvocationCancelled):
                provider.invoke(self.root,('codex','exec'),timeout=10,max_output_bytes=1024,
                                cancellation_check=lambda:marker.exists())
            self.assertLess(time.monotonic()-started,5)
            with self.assertRaises(ProcessLookupError):os.kill(int(marker.read_text()),0)
            self.assertIsNone(sibling.poll())
        finally:
            sibling.terminate();sibling.wait(timeout=10)

    def test_real_new_process_after_optional_dispatch_never_retries_or_resets_budget(self):
        self.process_boundary('dispatch')

    def test_real_new_process_after_primary_receipt_consumes_once_without_model_replay(self):
        self.process_boundary('consumer')

    def test_real_new_process_at_assurance_preserves_primary_candidate_and_budget(self):
        self.process_boundary('assurance')

    def test_real_new_process_at_assurance_without_findings_never_replays_primary(self):
        self.process_boundary('assurance-empty')

    def test_real_new_process_after_publication_acceptance_never_creates_twice(self):
        self.process_boundary('publication')

    def test_all_registered_roles_need_real_exact_snapshot_files(self):
        paths=('apps/apple/View.swift','apps/windows/View.cs','custom_components/example/entity.py','firmware/device.yaml',
               'renderer/screen.py','receiver/transport.js','website/index.html','src/api.py','.github/check.yaml','tests/test_docs.py','docs/guide.md','README.md')
        for path in paths:
            target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():target.write_text('bounded fixture\n')
        self.fixture.git('add','.');self.fixture.git('commit','-qm','capability snapshots')
        self.fixture.base=self.fixture.git('rev-parse','HEAD')
        for role,path in zip(cr.REVIEWER_ORDER,paths,strict=True):
            self.objective='Specialist review requests: '+json.dumps([{'reviewer':role,'question':'Which concrete contract condition needs follow-up?',
                'paths':[path],'consumer':'EXECUTE_AGENT','risk':'NORMAL'}])
            plan=self.plan(qualified_roles=cr.REVIEWER_ORDER)
            self.assertEqual([s.reviewer for s in plan.selections],[role])
            self.assertIsNone(plan.selections[0].confidence)
            self.assertEqual(dict(plan.selections[0].specialist_source_blobs)[path],self.fixture.git('rev-parse',f'HEAD:{path}'))

    def test_invalid_ambiguous_irrelevant_and_no_consumer_requests_are_concrete_skips(self):
        for update,reason in (({'consumer':'none'},'no_consumer'),({'paths':['absent.md']},'irrelevant_task_paths'),
                              ({'paths':['docs/absent.md']},'missing_snapshot_path'),({'question':'token=secret-material'},'invalid_question_contract'),
                              ({'paths':['../outside']},'invalid_question_contract'),({'risk':{}},'invalid_question_contract')):
            with self.subTest(reason=reason):
                request={**self.requests[0],**update}
                self.objective='Specialist review requests: '+json.dumps([request])
                plan=self.plan();self.assertFalse(plan.selections)
                self.assertEqual(next(d['payload']['reason'] for d in plan.decisions if d['reviewer']=='documentation'),reason)
        for text in ('Specialist review requests: not-json','Specialist review requests: {}','Specialist review requests: []\nSpecialist review requests: []'):
            self.objective=text;self.assertFalse(self.plan().selections)
        request=self.requests[0]
        self.objective='Specialist review requests: '+json.dumps([request,request])
        self.assertFalse(self.plan().selections)
        self.assertEqual(next(d['payload']['reason'] for d in self.plan().decisions if d['reviewer']=='documentation'),'overlapping_or_ambiguous_question')

    def test_primary_disposition_validation_replay_and_state_concurrency_are_real(self):
        runner=self.runner();result=runner.run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        records=result.specialist_records
        readback=cr.specialist_readback(records)
        supplied=[]
        for finding in readback['findings']:
            prior=next(r['payload'] for r in records if r['kind']=='DISPOSITION' and r['payload']['finding_id']==finding['id'])
            supplied.append({k:prior[k] for k in ('finding_id','disposition','reason','changed_paths')})
            if finding['path']=='README.md':supplied[-1].update(disposition='IMPLEMENTED',changed_paths=['README.md'])
        head=self.fixture.git('rev-parse','HEAD')
        self.assertEqual(cr.specialist_dispositions(records,tuple(supplied),candidate_sha=head,changed_paths=('README.md',)),())
        with self.assertRaises(ValueError):cr.specialist_dispositions(records,tuple(supplied),candidate_sha='f'*40,changed_paths=('README.md',))
        with self.assertRaises(ValueError):cr.specialist_dispositions(records,tuple(supplied)*2,candidate_sha=head,changed_paths=('README.md',))
        with self.assertRaises(StateError):self.store.save(replace(result,specialist_records=records[:-1]))
        invalid=json.loads(json.dumps(result.to_dict()));invalid['specialist_records'][0]['repository']='foreign/repo'
        with self.assertRaises(StateError):TransactionState.from_dict(invalid)
        invalid=json.loads(json.dumps(result.to_dict()));invalid['specialist_records'][-1]['payload']['result_candidate_sha']='b'*40
        with self.assertRaises(StateError):TransactionState.from_dict(invalid)
        self.assertEqual(self.store.load('specialist-run').specialist_records,records)

    def test_current_controls_are_required_for_verification_and_old_sha_cannot_be_relabelled(self):
        result=self.runner().run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        context=load_validation_context(self.root,'specialist-run',central_database=self.fixture.database)
        records=tuple(r for r in result.specialist_records if r['kind']!='VERIFICATION')
        head=self.fixture.git('rev-parse','HEAD')
        self.assertEqual(len(cr.specialist_verified(records,context,candidate_sha=head)),1)
        self.assertEqual(cr.specialist_verified(records,context,candidate_sha='f'*40),())
        for change in ({'controls':{}},{'profile_digest':'sha256:'+'a'*64},{'currentness':99},{'profile_currentness_conflict':True}):
            self.assertEqual(cr.specialist_verified(records,{**context,**change},candidate_sha=head),())
        self.assertEqual(cr.specialist_verified(result.specialist_records,context,candidate_sha=head),())

    def test_capacity_loss_at_dispatch_never_proposes_findings(self):
        self.capacity.stop()
        model=self.transport()
        with patch('engineering_platform.codex_capacity.read_remaining_percent',side_effect=(100,0)):
            result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(model.roles,['quality','security'])
        self.assertEqual(cr.specialist_readback(result.specialist_records)['reserved_invocation_count'],0)
        self.assertEqual(len(cr.specialist_readback(result.specialist_records)['dispatch_skips']),2)

    def test_context_overflow_then_capacity_loss_persists_each_skip_once_and_runs_primary(self):
        from engineering_platform.reviewer_evidence import ReviewerEvidence
        evidence=self.fixture.repository.inspect(self.root)
        factual=ReviewerEvidence.from_repository(self.state.run_id,self.state.execution_mode,evidence)
        selected=self.plan().selections[0]
        selected=replace(selected,specialist_binding={**selected.specialist_binding,'invocation_id':'a'*32})
        wrapped=len(cr.reviewer_prompt(selected,self.objective,factual).encode())
        self.objective+='\n'+'x'*max(0,18040-wrapped)
        self.fixture.prompt.write_text(self.objective)
        self.capacity.stop();model=self.transport()
        with patch('engineering_platform.codex_capacity.read_remaining_percent',side_effect=(100,100,0)):
            result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        self.assertEqual(model.roles,['quality','security'])
        skips=cr.specialist_readback(result.specialist_records)['dispatch_skips']
        self.assertEqual(len(skips),2)
        self.assertEqual(len({r['request_id'] for r in result.specialist_records if r['kind']=='SKIP'}),2)

    def test_skipped_reservation_cannot_later_dispatch_and_dispatched_slot_cannot_be_skipped(self):
        selected=self.plan().selections[0]
        binding=selected.specialist_binding
        skip={**binding,'kind':'SKIP','payload':{'reason':'capacity_reserved'}}
        dispatch={**binding,'invocation_id':'a'*32,'kind':'DISPATCH','payload':{'status':'DISPATCHED'}}
        for events in ((skip,dispatch),(dispatch,skip)):
            with self.subTest(events=events),self.assertRaises(StateError):
                self.store.save(replace(self.state,specialist_records=self.plan().decisions+events))

    def test_concurrent_checkpoint_append_preserves_one_actual_winner_and_rejects_other(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        plan=self.plan();state=replace(self.state,specialist_records=plan.decisions)
        self.store.save(state);barrier=Barrier(2)
        def append(selection):
            barrier.wait()
            next_state=replace(state,specialist_records=state.specialist_records+({**selection.specialist_binding,'kind':'SKIP','payload':{'reason':'capacity_reserved'}},))
            try:
                self.store.save(next_state)
                return 'SAVED'
            except StateError:
                return 'CONFLICT'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(append,plan.selections))
        self.assertEqual(sorted(outcomes),['CONFLICT','SAVED'])
        after=self.store.load(state.run_id)
        self.assertEqual(after.specialist_records[:-1],state.specialist_records)
        self.assertEqual(after.specialist_records[-1]['kind'],'SKIP')

    def test_primary_out_of_scope_application_is_blocked_before_assurance_and_publication(self):
        model=self.transport();original=model.invoke
        def invoke(root,prompt):
            result=original(root,prompt)
            choices=tuple({**c,'changed_paths':['outside.md']} if c['disposition']=='IMPLEMENTED' else c for c in result.specialist_dispositions)
            return replace(result,specialist_dispositions=choices)
        model.invoke=invoke
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.next_action,'specialist_disposition_invalid')
        self.assertEqual(model.roles,['validation','documentation'])
        self.assertIsNone(result.pull_request)
        self.assertFalse(any(r['kind']=='APPLICATION' for r in result.specialist_records))

    def test_real_deadline_expired_result_is_not_proposed_or_adopted(self):
        import time
        from engineering_platform.execution_timeout_policy import SPECIALIST_REVIEW
        self.requests=self.requests[:1]
        self.objective='Specialist review requests: '+json.dumps(self.requests)
        self.fixture.prompt.write_text(self.objective)
        model=self.transport();original=model.review
        def review(root,selected,objective,evidence=None):
            result=original(root,selected,objective,evidence)
            if selected.reviewer=='documentation':
                time.sleep(SPECIALIST_REVIEW.seconds+.1)
            return result
        model.review=review
        result=self.runner(model).run(self.fixture.prompt,run_id='specialist-run',owner_authorized=True)
        self.assertEqual(result.phase,'WAIT_FOR_OPERATOR_MERGE')
        self.assertEqual(cr.specialist_readback(result.specialist_records)['findings'],[])
        self.assertEqual(next(r['payload']['status'] for r in result.specialist_records if r['kind']=='RESULT'),'FAILED')
        self.assertEqual(model.roles,['documentation','quality','security'])

    def test_context_wrapper_overflow_is_no_call_and_complete_context_is_not_cut(self):
        from engineering_platform.execution_executor import CodexCliClient
        from engineering_platform.providers import CodexCliProvider
        from engineering_platform.capability_review import reviewer_prompt
        plan=self.plan();selection=replace(plan.selections[0],specialist_binding={**plan.selections[0].specialist_binding,'invocation_id':'a'*32})
        full=self.objective+'\n'+'required rule '*2000
        text=json.loads(reviewer_prompt(selection,full))['objective']
        self.assertEqual(text,full)
        client=CodexCliClient(CodexCliProvider())
        with patch.object(client.provider,'invoke',side_effect=AssertionError('Overflow dispatched model')):
            self.assertTrue(client.review(self.root,selection,full).failed)

    def test_typed_dispositions_do_not_rewrite_historical_completion_confidence_proxies(self):
        from engineering_platform.engineering_memory import capture_engineering_memory,load_engineering_memory
        legacy={'reviewers':[{'reviewer':'documentation','usage_count':3,'successful_outcomes':2,'accepted_recommendations':4,
                             'recommendation_count':5,'average_duration':0,'future_confidence':.67}]}
        path=self.root/'.engineering/memory/engineering-memory.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(legacy))
        before=legacy['reviewers'][0]
        capture_engineering_memory(self.root,self.state,({'reviewer':'documentation','measurement_semantics':'specialist-disposition-v1','accepted_recommendations':1,'verified_recommendations':1},))
        self.assertEqual(load_engineering_memory(self.root)['reviewers'][0],before)

    def test_actual_codex_adapter_requires_typed_binding_and_rejects_private_or_foreign_output(self):
        import subprocess
        from engineering_platform.execution_executor import CodexCliClient
        from engineering_platform.providers import CodexCliProvider
        selection=self.plan().selections[0]
        selection=replace(selection,specialist_binding={**selection.specialist_binding,'invocation_id':'a'*32})
        path=selection.specialist_paths[0]
        payload={'contract_version':cr.SPECIALIST_CONTRACT_VERSION,'contribution':'Bounded typed result','recommendations':[],
                 'specialist_binding':dict(selection.specialist_binding),'findings':[{'id':'case','summary':'Add an edge case.',
                    'path':path,'evidence_ref':'git-blob:'+dict(selection.specialist_source_blobs)[path],'proposed_disposition':'DEFERRED'}]}
        class ModelTransport(CodexCliProvider):
            def invoke(inner,root,command,*args,**kwargs):
                if command==('codex','--version'):
                    return subprocess.CompletedProcess(command,0,'codex-cli 0.160.1\n','')
                if command==('codex','mcp','list','--json'):
                    return subprocess.CompletedProcess(command,0,'[{"name":"unsafe_inherited"}]','')
                schema=json.loads(Path(command[command.index('--output-schema')+1]).read_text())
                assert schema['properties']['specialist_binding']['const']==selection.specialist_binding
                assert schema['properties']['findings']['items']['additionalProperties'] is False
                assert root!=self.root and not (root/'.git').exists()
                assert all(flag in command for flag in ('--ignore-user-config','--ignore-rules','--strict-config','--ephemeral'))
                assert kwargs['max_output_bytes']==262144
                assert 'permissions.ep-effects-' in ' '.join(command)
                assert 'mcp_servers.unsafe_inherited.enabled=false' in ' '.join(command)
                text=json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(inner.output)}})
                return subprocess.CompletedProcess(command,0,text,'')
        provider=ModelTransport();provider.output=payload;client=CodexCliClient(provider)
        result=client.review(self.root,selection,self.objective)
        self.assertFalse(result.failed);self.assertEqual(len(cr.specialist_findings(selection,result)),1)
        for changed in ({**payload,'private_reasoning':'never retain'},{**payload,'specialist_binding':{**payload['specialist_binding'],'run_id':'foreign'}},
                        {**payload,'recommendations':['untyped advice']},{**payload,'findings':{}},{**payload,'contract_version':'1.0'}):
            provider.output=changed
            self.assertTrue(client.review(self.root,selection,self.objective).failed)

    def test_parallel_mandatory_or_excess_optional_wave_is_not_admitted(self):
        for selections in ((ReviewerSelection('quality','mandatory',1),ReviewerSelection('security','mandatory',1)),
                           tuple(ReviewerSelection(role,'question',1) for role in ('validation','documentation','api'))):
            with self.assertRaises(ValueError):cr.run_reviews(self.root,selections,self.objective,self.transport())
        self.assertEqual(cr.run_reviews(self.root,(),self.objective,None),())

    def test_timed_out_cancelled_foreign_and_mutating_optional_transports_do_not_create_proposals(self):
        for failure in ('timeout','cancelled','foreign','mutating'):
            with self.subTest(failure=failure):
                # A fresh same-fixture checkpoint for each isolated run, never a
                # reset of the previously consumed run identity.
                run_id='specialist-'+failure
                state=replace(self.state,run_id=run_id,phase='CAPABILITY_REVIEW')
                model=self.transport()
                original=model.review
                def review(root,selection,objective,evidence=None):
                    if failure=='timeout':raise TimeoutError('external transport timeout')
                    result=original(root,selection,objective,evidence)
                    if failure=='cancelled':return replace(result,failed=True)
                    if failure=='foreign':return replace(result,specialist_binding={**result.specialist_binding,'run_id':'foreign'})
                    (root/'README.md').write_text('unauthorized reviewer mutation\n');return result
                model.review=review
                runner=self.runner(model)
                result=runner._run_optional_specialists(state,self.objective,self.fixture.repository.inspect(self.root))
                self.assertEqual(cr.specialist_readback(result.specialist_records)['findings'],[])
                if failure=='mutating':self.assertEqual(result.next_action,'specialist_snapshot_changed')
                # Restore only this fixture's declared synthetic bytes after denial.
                self.fixture.git('restore','README.md')
