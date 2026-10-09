"""Actual inline request acceptance and canonical authority ordering."""
from dataclasses import replace
from pathlib import Path
import unittest

from engineering_platform.capability_review import run_reviews, ReviewerSelection, ReviewStartUncertain
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.storage import sqlite_connection
from tests.engineering.inline_review_backend import InlineReviewBackend
from tests.engineering import test_managed_adoption as adoption
from tests.engineering.test_git_effect_authority_regressions import target_bytes


class InlineStartAuthority(unittest.TestCase):
    def pipeline(self, boundary=None):
        c = adoption.AdoptionLifecycleTests(); c.setUp(); self.addCleanup(c.doCleanups)
        self.last_case = c
        base, github = c.lifecycle_adapters()
        actual = base.review
        revoked, bodies = [], []
        class Adapter(InlineReviewBackend):
            def __getattr__(self, name): return getattr(base, name)
            @property
            def review(self):
                if boundary == 'late_resolve' and not revoked:
                    c.unbind(); revoked.append(target_bytes(c.root))
                def response(root, selection, objective, evidence=None):
                    bodies.append(selection.reviewer)
                    result = actual(root, selection, objective, evidence)
                    if boundary == 'between_reviews' and selection.reviewer == 'quality':
                        c.unbind(); revoked.append(target_bytes(c.root))
                    return result
                return response
            def prepare_review(self, *args):
                request = super().prepare_review(*args)
                if boundary == 'prepared_before_start' and not revoked:
                    c.unbind(); revoked.append(target_bytes(c.root))
                if boundary == 'checkpoint_before_start' and not revoked:
                    c.store.save(replace(c.store.load('adopt-run'),owner_authorized=False))
                    revoked.append(target_bytes(c.root))
                if boundary == 'lease_before_start' and not revoked:
                    from engineering_platform.execution_lease import release
                    release(c.root,runner.active_lease,central_database=c.database)
                    revoked.append(target_bytes(c.root))
                if boundary == 'lost_ack':
                    import socket, struct
                    from threading import Thread
                    from engineering_platform.capability_review import SocketReviewRequest
                    from engineering_platform.qualification_runtime import receive
                    front, proxy = socket.socketpair(); front.settimeout(300)
                    upstream = request.connection
                    def forward_and_drop_ack():
                        try:
                            size = receive(proxy,4); length = struct.unpack('!I',size)[0]
                            upstream.sendall(size+receive(proxy,length))
                            receive(upstream,64)  # Actual backend accepted; ACK is lost.
                        finally:
                            upstream.close(); proxy.close()
                    Thread(target=forward_and_drop_ack,daemon=True).start()
                    return SocketReviewRequest(front,request.payload)
                return request
        adapter = Adapter()
        if boundary == 'audit_failure':
            with sqlite_connection(c.database) as db:
                db.execute("CREATE TRIGGER deny_actual_review_audit BEFORE INSERT ON provider_invocations "
                           "WHEN NEW.phase='MANDATORY_ASSURANCE_DISPATCH' BEGIN SELECT RAISE(ABORT,'real audit fault'); END")
        runner = EngineeringRunner(c.root,c.store,c.repository,github,adapter,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        state = runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        acceptances = getattr(adapter,'review_acceptances',[])
        with sqlite_connection(c.database) as db:
            rows = [tuple(row) for row in db.execute("SELECT phase,role FROM provider_invocations WHERE run_id='adopt-run' ORDER BY ordinal")]
        if boundary is None:
            self.assertEqual(state.phase,'WAIT_FOR_OPERATOR_MERGE')
            self.assertEqual([a['reviewer'] for a in acceptances],['quality','security'])
            self.assertEqual(bodies,['quality','security'])
            self.assertEqual(github.creates,1)
            self.assertEqual([e['status'] for e in state.assurance_launch_events],['INTENT','STARTED','RESULT']*2)
        elif boundary in {'audit_failure','lost_ack'}:
            self.assertEqual(state.next_action,'assurance_start_uncertain')
            self.assertEqual([a['reviewer'] for a in acceptances],['quality'])
            self.assertEqual(rows,[])
            self.assertEqual([e['status'] for e in state.assurance_launch_events],['INTENT','UNKNOWN'])
            c.stop_host(runner)
            if boundary == 'audit_failure':
                with sqlite_connection(c.database) as db: db.execute('DROP TRIGGER deny_actual_review_audit')
            # Same public recovery cannot manufacture a fresh invocation while
            # the prior actual acceptance lacks a canonical start audit.
            again = EngineeringRunner(c.root,c.store,c.repository,github,adapter,lambda _:None)
            self.addCleanup(c.stop_host,again)
            restored = again.run(c.prompt,run_id='adopt-run',resume=True)
            self.assertEqual(len(adapter.review_acceptances),1)
            self.assertEqual(restored.next_action,'assurance_start_uncertain')
        else:
            expected = ['quality'] if boundary == 'between_reviews' else []
            self.assertEqual([a['reviewer'] for a in acceptances],expected, 'revoked inline request must have zero subsequent actual backend acceptances')
            self.assertEqual(bodies,expected)
            self.assertEqual([r['reviewer'] for r in state.assurance_reviews],expected)
            self.assertEqual(rows,[(p,r) for r in expected for p in ('MANDATORY_ASSURANCE_DISPATCH','MANDATORY_ASSURANCE')])
            self.assertEqual(target_bytes(c.root),revoked[0])
            self.assertEqual((state.phase,state.next_action),('BLOCKED','managed_candidate_adoption_invalid'))
            self.assertEqual(github.creates,0)
        self.assertEqual(state.repair_iterations,0)
        self.assertEqual(base.prompts,[])
        for accepted in acceptances:
            self.assertTrue(accepted['invocation_id'])
            self.assertEqual(len(accepted['request_digest']),64)
        return state

    def test_valid_two_acceptances_and_normal_publication(self): self.pipeline()
    def test_late_callable_resolution_is_before_final_guard(self): self.pipeline('late_resolve')
    def test_prepared_request_is_denied_before_actual_send(self): self.pipeline('prepared_before_start')
    def test_first_accepted_result_survives_withdrawal_second_does_not_start(self): self.pipeline('between_reviews')
    def test_real_audit_failure_preserves_unknown_acceptance_no_blind_retry(self): self.pipeline('audit_failure')
    def test_lost_actual_backend_ack_is_unknown_and_not_replayed(self): self.pipeline('lost_ack')
    def test_actual_checkpoint_loss_prevents_handoff(self): self.pipeline('checkpoint_before_start')
    def test_actual_lease_loss_prevents_handoff(self): self.pipeline('lease_before_start')

    def test_synchronous_only_scoped_adapter_has_no_fallback_or_start_marker(self):
        from engineering_platform.managed_adoption import AdoptionAuthorityError
        calls, starts = [], []
        class Client:
            native_process_effects = True  # A self-claimed marker is not native transport.
            def review(self,*args): calls.append(True)
        from contextlib import contextmanager
        @contextmanager
        def standalone_authority(): yield lambda: None
        with self.assertRaises(AdoptionAuthorityError):
            run_reviews(Path('.'),(ReviewerSelection('quality','assurance',1),),'objective',Client(),
                        authority=standalone_authority,started=lambda:starts.append(True))
        self.assertEqual((calls,starts),([],[]))

    def test_former_callback_window_is_after_true_acceptance_and_blocks_next_request(self):
        import os
        from engineering_platform.execution_lease import acquire, LeaseHeartbeat
        from engineering_platform.managed_adoption import effect_authority, AdoptionAuthorityError
        c = adoption.AdoptionLifecycleTests();c.setUp();self.addCleanup(c.doCleanups)
        base, github = c.lifecycle_adapters()
        runner=EngineeringRunner(c.root,c.store,c.repository,github,base,lambda _:None)
        self.addCleanup(c.stop_host,runner)
        state=runner.run(c.prompt,run_id='adopt-run',owner_authorized=True,managed_candidate=c.selection)
        runner.active_lease=acquire(c.root,state.run_id,identity=runner.host_identity,
            instance_id=runner.host_instance_id,process_id=os.getpid(),central_database=c.database)
        runner.lease_heartbeat=LeaseHeartbeat(c.root,runner.active_lease,central_database=c.database)
        runner.lease_heartbeat.start()
        before=len(base.review_acceptances)
        order=[]
        def accepted_callback():
            self.assertEqual(len(base.review_acceptances),before+1,
                             'start callback must follow actual full-request backend acceptance')
            order.append('accepted_before_callback')
            c.unbind();order.append('canonical_withdrawal')
        authority=lambda:effect_authority(state=state,root=c.root,central_database=c.database,lease=runner.active_lease)
        result=run_reviews(c.root,(ReviewerSelection('quality','proof',1,transport_invocation_id='callback-window'),),
                           'proof',base,authority=authority,started=accepted_callback)
        self.assertFalse(result[0].failed)
        self.assertEqual(order,['accepted_before_callback','canonical_withdrawal'])
        snapshot=target_bytes(c.root)
        with self.assertRaises(AdoptionAuthorityError):
            run_reviews(c.root,(ReviewerSelection('security','proof',1,transport_invocation_id='denied-next'),),
                        'proof',base,authority=authority,started=lambda:self.fail('denied next start'))
        self.assertEqual(len(base.review_acceptances),before+1)
        self.assertEqual(target_bytes(c.root),snapshot)


    def test_launch_history_cannot_be_erased_rebound_or_fabricated(self):
        from engineering_platform.agent_state import StateError
        state=self.pipeline()
        c=self.last_case
        original=state.assurance_launch_events
        for changed in ((), original[:-1],
                        original+({**original[-1], "status": "RESULT"},),
                        ({**original[0], "status": "STARTED"},)+original[1:],
                        (original[0], {**original[1], "candidate_sha": "f"*40})+original[2:]):
            with self.subTest(events=len(changed)):
                with self.assertRaises(StateError):
                    c.store.save(replace(state,assurance_launch_events=changed))
                self.assertEqual(c.store.load(state.run_id).assurance_launch_events,original)
