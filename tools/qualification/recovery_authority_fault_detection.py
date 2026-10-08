#!/usr/bin/env python3
"""Require real fault detection for the r33 authority and local Genesis regressions."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

AUTHORITY = 'tests.engineering.test_recovery_authority_regressions.RecoveryAuthorityRegressions.test_unbound_available_repair_has_zero_calls_and_target_writes'
GENESIS = 'tests.engineering.test_recovery_authority_regressions.GenesisConsumerRegression.test_empty_specialist_consumer_supports_local_genesis_without_origin'


def run(root, test):
    environment = dict(os.environ, PYTHONPATH=str(root / 'src') + os.pathsep + str(root),
                       PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run((sys.executable, '-m', 'unittest', test, '-v'), cwd=root,
                          env=environment, text=True, capture_output=True, timeout=60)  # nosec B603


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    source = args.source_root.resolve()
    receipts = []
    for name, test, marker in (
        ('revoked_replacement', AUTHORITY, 'revoked authority must prevent every replacement call'),
        ('local_genesis_origin', GENESIS, "No such remote 'origin'"),
    ):
        with tempfile.TemporaryDirectory(prefix='ep-recovery-fault-control-') as temporary:
            root = Path(temporary)
            shutil.copytree(source / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(source / 'tests', root / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
            positive = run(root, test)
            if positive.returncode != 0:
                raise RuntimeError(f'{name}: current acceptance regression failed\n{positive.stdout}\n{positive.stderr}')
            host = root / 'src/engineering_platform/execution_host.py'
            text = host.read_text()
            if name == 'revoked_replacement':
                # Restore precisely the old ordering in an isolated negative
                # control, including the old unguarded actual invocation.
                start = text.index('    def _invoke_agent_with_timing(')
                end = text.index('    def _run_local_repository_validation(', start)
                controller = text[start:end].replace('self._verify_adoption_continuation(state)', 'None')
                text = text[:start] + controller + text[end:]
                early = '''                try:\n                    self._verify_adoption_continuation(state)\n                except RunnerError as error:\n                    return self._save_terminal(state, "BLOCKED", "managed_candidate_adoption_invalid", str(error))\n                result = self._durable_repair_result_for_validation_resume(state)'''
                if text.count(early) != 1:
                    raise RuntimeError('authority negative-control anchor changed')
                text = text.replace(early, '                result = self._durable_repair_result_for_validation_resume(state)', 1)
                guarded = 'with effect_authority(state=state, root=self.root,'
                if text.count(guarded) != 1:
                    raise RuntimeError('authority effect-control anchor changed')
                text = text.replace(guarded, 'with effect_authority(state=replace(state, managed_candidate_adoption=None), root=self.root,', 1)
            else:
                anchor = '        baseline = evidence\n        if not state.specialist_records:'
                if text.count(anchor) != 1:
                    raise RuntimeError('Genesis negative-control anchor changed')
                text = text.replace(anchor, '        baseline = evidence\n        evidence = self.repository.inspect(self.root)\n        if not state.specialist_records:', 1)
            host.write_text(text)
            negative = run(root, test)
            output = negative.stdout + negative.stderr
            if negative.returncode != 1 or marker not in output or 'FAILED (' not in output:
                raise RuntimeError(f'{name}: negative control did not detect the concrete fault\n{output}')
            if 'ImportError' in output or '_FailedTest' in output:
                raise RuntimeError(f'{name}: import failure cannot qualify fault detection')
            receipts.append({'regression': name, 'current_acceptance': 'PASS',
                             'restored_fault': 'DETECTED', 'test': test})
    print(json.dumps({'qualification': 'PASS', 'controls': receipts}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
