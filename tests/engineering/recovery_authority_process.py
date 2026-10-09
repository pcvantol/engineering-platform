"""Real process crash/resume at an adopted repair availability boundary."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from engineering_platform import execution_host as host
from engineering_platform.agent_state import StateStore, TransactionState
from engineering_platform.execution_host import EngineeringRunner
from engineering_platform.execution_lease import acquire, LeaseHeartbeat
from engineering_platform.execution_repository import SubprocessRepositoryClient
from engineering_platform.managed_adoption import verify_selection
from tests.engineering.test_managed_adoption import AdoptionLifecycleTests, LocalGitHubTransport
from tests.engineering.test_recovery_authority_regressions import RecoveryAuthorityRegressions


def main():
    spec_path, mode = Path(sys.argv[1]), sys.argv[2]
    spec = json.loads(spec_path.read_text())
    case = AdoptionLifecycleTests()
    for key in ('root', 'remote', 'data', 'area', 'prompt', 'database'):
        setattr(case, key, Path(spec[key]))
    case.sha, case.base, case.selection = spec['sha'], spec['base'], spec['selection']
    case.repository = SubprocessRepositoryClient(LocalGitHubTransport(case.remote))
    case.store = StateStore(case.root / '.engineering/engineering-runs', central_database=case.database,
                           emit_local_projection=False)
    test = RecoveryAuthorityRegressions()
    test.case, test.calls = case, []
    test.agent, test.github = case.lifecycle_adapters()
    with patch('engineering_platform.execution_host.provider_readiness_failures', return_value=()):
        if mode == 'start':
            state = TransactionState('adopt-run', 'qualification/managed', str(case.prompt),
                'LOCAL_REPOSITORY_VALIDATION', owner_authorized=True,
                last_verified_sha=case.sha, implementation_head_sha=case.sha)
            state = verify_selection(selection=case.selection, state=state, root=case.root,
                repository=case.repository, central_database=case.database, owner_authorized=True)
            case.store.save(state)
            runner = EngineeringRunner(case.root, case.store, case.repository, test.github, test.agent, lambda _: None)
            state, error = runner._confirm_deterministic_admission(state)
            assert error is None
            runner.active_lease = acquire(case.root, state.run_id, identity=runner.host_identity,
                instance_id=runner.host_instance_id, process_id=os.getpid(), central_database=case.database)
            runner.lease_heartbeat = LeaseHeartbeat(case.root, runner.active_lease, central_database=case.database)
            runner.lease_heartbeat.start()
            capture = host.capture_worktree_provenance
            def crash_after_available(*args, **kwargs):
                captured = capture(*args, **kwargs)
                if kwargs.get('stage') == 'interrupted' and kwargs.get('phase') == 'REPAIR_AGENT':
                    assert captured
                    os._exit(73)
                return captured
            with patch.dict(os.environ, {'ENGINEERING_PLATFORM_TEST_INTERRUPT_PROVIDER_ONCE': 'adopt-run:REPAIR_AGENT'}), \
                 patch.object(host, 'capture_worktree_provenance', side_effect=crash_after_available):
                runner._repair(state, 'local validation failed. Correct the documentation heading.')
            raise AssertionError('actual availability crash did not occur')
        test.replacement()
        result = test.resume()
        test.doCleanups()
        document = {'phase': result.phase, 'next_action': result.next_action, 'calls': test.calls,
                    'creates': test.github.creates, 'repair_iterations': result.repair_iterations}
        spec_path.with_suffix('.result.json').write_text(json.dumps(document))


if __name__ == '__main__':
    main()
