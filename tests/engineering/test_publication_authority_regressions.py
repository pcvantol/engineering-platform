"""Real authority/lease regressions at native and inline publication effects."""
from datetime import datetime, timezone
import json
import os
import sqlite3
import sys
import time
import unittest

from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_repository import GhCliClient
from engineering_platform.providers import LocalProcessProvider, GitHubProvider
from engineering_platform.provider_recovery import load_recovery_state
from tests.engineering import test_recovery_authority_regressions as recovery_fixture


class PublicationAuthorityRegressions(unittest.TestCase):
    def setUp(self):
        self.recovery = recovery_fixture.RecoveryAuthorityRegressions()
        self.recovery.setUp()
        self.addCleanup(self.recovery.doCleanups)

    def continuation(self, revoke):
        case = self.recovery
        before, recovery = case.recovered_result()
        events, snapshots, audits = [], [], []
        readback = case.github.publication_candidates
        fixture = case.case
        class ExternalGitHubTransport:
            def github(inner, *args):
                events.append(args[:2])
                if args[:2] == ('pr', 'view'):
                    if revoke == 'view':
                        fixture.unbind()
                        snapshots.append(case.snapshot())
                    audits.append(fixture.store.load(before.run_id).repair_audit)
                    return json.dumps({'body': 'One\\nTwo'})
                return ''
        client = GhCliClient(ExternalGitHubTransport(), repository='qualification/managed')
        def external_readback(*args):
            result = readback(*args)
            if result and revoke == 'create' and not snapshots:
                fixture.unbind()
                snapshots.append(case.snapshot())
                audits.append(fixture.store.load(before.run_id).repair_audit)
            return result
        case.github.publication_candidates = external_readback
        def normalize(number):
            result = client.normalize_markdown_body(number)
            if revoke == 'edit':
                fixture.unbind()
                snapshots.append(case.snapshot())
                audits.append(fixture.store.load(before.run_id).repair_audit)
            return result
        case.github.normalize_markdown_body = normalize
        case.github.ready = client.ready
        after = case.resume()
        mutations = [event for event in events if event[1] in {'edit', 'ready'}]
        expected = [] if revoke in {'create', 'view'} else [('pr', 'edit')] if revoke == 'edit' else [('pr', 'edit'), ('pr', 'ready')]
        self.assertEqual(mutations, expected, 'revoked publication must prevent every subsequent mutation')
        self.assertEqual(after.phase, 'BLOCKED' if revoke else 'WAIT_FOR_OPERATOR_MERGE', after)
        if revoke:
            self.assertEqual(after.next_action, 'managed_candidate_adoption_invalid')
            self.assertEqual(case.snapshot(), snapshots[0])
        self.assertEqual(case.calls, [])
        self.assertEqual(case.github.creates, 1)
        self.assertEqual(after.repair_iterations, before.repair_iterations)
        self.assertEqual(len(after.repair_audit), len(before.repair_audit))
        self.assertEqual(after.repair_audit[0]['repair_id'], before.repair_audit[0]['repair_id'])
        if revoke:
            self.assertEqual(after.repair_audit, audits[0])
        self.assertEqual(load_recovery_state(fixture.root, before.run_id, central_database=fixture.database), recovery)
        self.assertEqual(after.publication_intent['pull_request'], 71)

    def test_unbind_after_create_prevents_edit_and_ready(self):
        self.continuation('create')

    def test_unbind_during_body_read_prevents_edit_and_ready(self):
        self.continuation('view')

    def test_unbind_after_edit_prevents_ready(self):
        self.continuation('edit')

    def test_bound_recovered_publication_edits_and_readies_once(self):
        self.continuation(None)

    def delayed_publication(self, delay, lose_lease=False):
        case = self.recovery
        fixture = case.case
        runner = EngineeringRunner(fixture.root, fixture.store, fixture.repository,
                                   case.github, case.agent, lambda _: None)
        self.addCleanup(fixture.stop_host, runner)
        original = case.github.create_draft_publication
        observed = []
        def delayed(*args):
            if lose_lease:
                # Stop only this owning heartbeat; expiry remains the actual
                # unmodified product 90s lease, with no fake clock or SQL edit.
                runner.lease_heartbeat.stop()
            LocalProcessProvider().execute(fixture.root, (sys.executable, '-c', f'import time;time.sleep({delay})'))
            error = runner.lease_heartbeat.error
            with sqlite3.connect(fixture.database) as connection:
                row = connection.execute('SELECT lease_state, expires_at FROM execution_run_leases WHERE lease_id=?',
                                         (runner.active_lease.lease_id,)).fetchone()
            live = row[0] == 'ACTIVE' and datetime.fromisoformat(row[1]) >= datetime.now(timezone.utc)
            observed.append((None if error is None else str(error), live))
            original(*args)
        case.github.create_draft_publication = delayed
        after = runner.run(fixture.prompt, run_id='adopt-run', owner_authorized=True,
                           managed_candidate=fixture.selection)
        self.assertEqual(observed, [(None, not lose_lease)], 'publication wait must not block the real lease heartbeat')
        self.assertEqual(case.github.creates, 0 if lose_lease else 1)
        self.assertEqual(after.phase, 'BLOCKED' if lose_lease else 'WAIT_FOR_OPERATOR_MERGE', after)
        self.assertEqual(after.publication_intent['status'], 'CREATE_UNCERTAIN' if lose_lease else 'RECONCILED')
        self.assertEqual(after.repair_iterations, 0)
        self.assertEqual(after.publication_intent['candidate_sha'], fixture.sha)
        if lose_lease:
            self.assertEqual(after.next_action, 'managed_candidate_adoption_invalid')
            # A denied effect preserves the one-way existing publication
            # intent; it must not invent a new attempt or retry the create.
            self.assertIsNone(after.publication_intent['pull_request'])

    def test_real_publication_wait_past_busy_timeout_keeps_heartbeat(self):
        self.delayed_publication(31)

    def test_real_publication_wait_past_original_expiry_keeps_heartbeat(self):
        self.delayed_publication(96)

    def test_actual_natural_lease_loss_prevents_delayed_create(self):
        self.delayed_publication(96, lose_lease=True)

    def test_native_github_transport_wait_releases_canonical_lock(self):
        case = self.recovery
        before, recovery = case.recovered_result()
        fixture = case.case
        events = []
        class NativeQualificationTransport(GitHubProvider):
            def github(inner, *args):
                # Real OS process, same production primitive as native gh.
                # Only the remote GitHub response is deterministic.
                output = json.dumps({'body': 'One\\nTwo'}) if args[:2] == ('pr', 'view') else ''
                completed = LocalProcessProvider().execute(fixture.root,
                    (sys.executable, '-c', 'import sys,time;time.sleep(1);print(sys.argv[1])', output))
                events.append(args[:2])
                if args[:2] == ('pr', 'view'):
                    fixture.unbind()  # Genuine writer succeeds after child start.
                return completed.stdout.strip()
        client = GhCliClient(NativeQualificationTransport(), repository='qualification/managed')
        case.github.normalize_markdown_body = client.normalize_markdown_body
        case.github.ready = client.ready
        after = case.resume()
        self.assertEqual(events, [('pr', 'view')])
        self.assertEqual((after.phase, after.next_action), ('BLOCKED', 'managed_candidate_adoption_invalid'))
        self.assertEqual(case.calls, [])
        self.assertEqual(case.github.creates, 1)
        self.assertEqual(load_recovery_state(fixture.root, before.run_id, central_database=fixture.database), recovery)
