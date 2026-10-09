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
PUBLICATION = 'tests.engineering.test_publication_authority_regressions.PublicationAuthorityRegressions.test_unbind_after_create_prevents_edit_and_ready'
HEARTBEAT = 'tests.engineering.test_publication_authority_regressions.PublicationAuthorityRegressions.test_real_publication_wait_past_busy_timeout_keeps_heartbeat'
FRESH_GIT = 'tests.engineering.test_git_effect_authority_regressions.GitEffectAuthorityRegressions.test_fresh_revocation_has_zero_git_mutations_and_identical_metadata'
POSTMERGE_GIT = 'tests.engineering.test_git_effect_authority_regressions.GitEffectAuthorityRegressions.test_postmerge_unbind_preserves_all_target_metadata_and_attempt'
LATER_RESUME = 'tests.engineering.test_later_adoption_and_profile_authority.LaterAdoptionAndProfileAuthority.test_public_finalization_resume_after_unbind_has_zero_native_effects'
PARTIAL_PROFILE = 'tests.engineering.test_later_adoption_and_profile_authority.LaterAdoptionAndProfileAuthority.test_partial_adoption_revocation_never_fetches_promisor_objects'
CONTROL = 'tests.engineering.test_control_assurance_authority.ControlAssuranceAuthority.test_original_control_regression_has_zero_effects'
ASSURANCE = 'tests.engineering.test_control_assurance_authority.ControlAssuranceAuthority.test_last_control_to_quality_withdrawal'
INLINE = 'tests.engineering.test_inline_review_start_authority.InlineStartAuthority.test_prepared_request_is_denied_before_actual_send'
GENESIS = 'tests.engineering.test_recovery_authority_regressions.GenesisConsumerRegression.test_empty_specialist_consumer_supports_local_genesis_without_origin'

R10_NATIVE = 'tests.engineering.test_round10_public_host_boundaries.Round10PublicHostBoundaries.test_native_public_host_metadata_not_modelstarts_full_delivery'
R10_ACK = 'tests.engineering.test_round10_public_host_boundaries.Round10PublicHostBoundaries.test_fragmented_ack_total_deadline_releases_public_host_lock'
R10_CHECKPOINT = 'tests.engineering.test_round10_public_host_boundaries.Round10PublicHostBoundaries.test_last_native_control_committed_withdrawal_blocks_reviews'


def run(root, test):
    environment = dict(os.environ, PYTHONPATH=str(root / 'src') + os.pathsep + str(root),
                       PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run((sys.executable, '-m', 'unittest', test, '-v'), cwd=root,
                          env=environment, text=True, capture_output=True, timeout=90)  # nosec B603


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    source = args.source_root.resolve()
    receipts = []
    for name, test, marker in (
        ('revoked_replacement', AUTHORITY, 'revoked authority must prevent every replacement call'),
        ('local_genesis_origin', GENESIS, "No such remote 'origin'"),
        ('revoked_publication_continuation', PUBLICATION, 'revoked publication must prevent every subsequent mutation'),
        ('publication_heartbeat_lock', HEARTBEAT, 'publication wait must not block the real lease heartbeat'),
        ('revoked_fresh_git', FRESH_GIT, 'revoked fresh admission must prevent every mutating Git start'),
        ('revoked_postmerge_git', POSTMERGE_GIT, 'revoked post-merge readback must prevent every mutating Git start'),
        ('revoked_later_public_resume', LATER_RESUME, 'revoked later resume must deny every mutating host Git start'),
        ('revoked_partial_profile', PARTIAL_PROFILE, 'revoked profile observation must keep all target Git metadata unchanged'),
        ('revoked_control_start', CONTROL, 'withdrawal must prevent every subsequent control target write'),
        ('revoked_assurance_start', ASSURANCE, 'withdrawal must prevent every subsequent reviewer start'),
        ('revoked_inline_actual_handoff', INLINE, 'revoked inline request must have zero subsequent actual backend acceptances'),
        ('native_metadata_modelstart', R10_NATIVE, 'native public host must execute both actual specialist model requests'),
        ('fragmented_ack_total_deadline', R10_ACK, 'total ACK deadline must release authority lock for canonical withdrawal'),
        ('postcontrol_grant_checkpoint', R10_CHECKPOINT, 'last control checkpoint change must prevent every subsequent reviewer'),
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
            if name == 'native_metadata_modelstart':
                provider = root / 'src/engineering_platform/providers.py'
                content = provider.read_text()
                anchor = 'if command == "exec" and _model_started.get() is not None:'
                if content.count(anchor) != 1: raise RuntimeError('native modelstart fault anchor changed')
                provider.write_text(content.replace(anchor, 'if _model_started.get() is not None:', 1))
            elif name == 'fragmented_ack_total_deadline':
                review = root / 'src/engineering_platform/capability_review.py'
                content = review.read_text()
                anchor = '\n            remaining = deadline - time.monotonic()'
                if content.count(anchor) != 1: raise RuntimeError('ACK deadline fault anchor changed')
                review.write_text(content.replace(anchor, '\n            remaining = 5', 1))
            elif name == 'postcontrol_grant_checkpoint':
                anchor = '                self._save_effect_checkpoint(previous_validation, validation)'
                if text.count(anchor) != 1: raise RuntimeError('postcontrol fault anchor changed')
                text = text.replace(anchor, '                self.store.save(validation)', 1)
            elif name == 'revoked_inline_actual_handoff':
                review = root / 'src/engineering_platform/capability_review.py'
                content = review.read_text()
                anchor = '                        with process_effect_start() as release:'
                if content.count(anchor) != 1: raise RuntimeError('inline handoff fault anchor changed')
                content = 'from contextlib import nullcontext\n' + content if 'from __future__' not in content else content.replace('from __future__ import annotations', 'from __future__ import annotations\nfrom contextlib import nullcontext', 1)
                review.write_text(content.replace(anchor,
                    '                        with nullcontext(lambda: None) as release:', 1))
            elif name == 'revoked_control_start':
                start = text.index('    def _execute_required_validation_controls(')
                end = text.index('    def _run_required_validation_command(', start)
                block = text[start:end]
                anchor = 'state=validation, root=self.root,'
                if block.count(anchor) != 1: raise RuntimeError('control fault anchor changed')
                block = block.replace(anchor, 'state=replace(validation, managed_candidate_adoption=None), root=self.root,', 1)
                text = text[:start] + block + text[end:]
            elif name == 'revoked_assurance_start':
                anchor = 'authority=lambda: effect_authority(state=quality, root=self.root,'
                if text.count(anchor) != 1: raise RuntimeError('assurance fault anchor changed')
                text = text.replace(anchor, 'authority=lambda: effect_authority(state=replace(quality, managed_candidate_adoption=None), root=self.root,', 1)
            elif name == 'revoked_replacement':
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
            elif name == 'local_genesis_origin':
                anchor = '        baseline = evidence\n        if not state.specialist_records:'
                if text.count(anchor) != 1:
                    raise RuntimeError('Genesis negative-control anchor changed')
                text = text.replace(anchor, '        baseline = evidence\n        evidence = self.repository.inspect(self.root)\n        if not state.specialist_records:', 1)
            elif name == 'revoked_publication_continuation':
                start = text.index('            from .managed_adoption import effect_authority',
                                   text.index('    def _continue_after_quality_control('))
                end = text.index('        return self._poll(state, result)', start)
                text = (text[:start] + '            self.github.normalize_markdown_body(state.pull_request)\n'
                        + '            self.github.ready(state.pull_request)\n' + text[end:])
            elif name == 'revoked_fresh_git':
                repository = root / 'src/engineering_platform/execution_repository.py'
                implementation = repository.read_text()
                start = implementation.index('    def protected_main_revision(self, root: Path) -> str:', implementation.index('class SubprocessRepositoryClient'))
                end = implementation.index('\n    def ', start + 5)
                implementation = implementation[:start] + '''    def protected_main_revision(self, root: Path) -> str:
        self.refresh_main_reference(root)
        return self._run(root, "git", "rev-parse", "origin/main")
''' + implementation[end:]
                repository.write_text(implementation)
            elif name == 'revoked_postmerge_git':
                anchor = '''            with process_effect_scope(lambda: effect_authority(
                    state=attempted, root=self.root,
                    central_database=self.store.central_database, lease=self.active_lease,
                    git_effect=True)):
                self.repository.refresh_main_reference(git_root)'''
                if text.count(anchor) != 1:
                    raise RuntimeError('postmerge Git negative-control anchor changed')
                text = text.replace(anchor, '            self.repository.refresh_main_reference(git_root)', 1)
            elif name == 'revoked_later_public_resume':
                start = text.index('        elif context.execution_mode == "MANAGED":',
                                   text.index('        if adoption_selection is not None:', text.index('    def run(')))
                end = text.index('                # The initial observation predates lease acquisition.', start)
                block = text[start:end]
                scope = block.index('                with process_effect_scope(lambda: effect_authority(')
                mutation = block.index('                    if revision_binding is not None', scope)
                unguarded = ''.join(line[4:] if line.startswith('    ') else line
                                    for line in block[mutation:].splitlines(keepends=True))
                text = text[:start] + block[:scope] + unguarded + text[end:]
                text = text.replace('if state.managed_candidate_adoption is not None:\n            from .managed_adoption import verify_continuation',
                    'if state.managed_candidate_adoption is not None and state.transaction_kind == "IMPLEMENTATION":\n            from .managed_adoption import verify_continuation', 1)
                authority = root / 'src/engineering_platform/managed_adoption.py'
                content = authority.read_text()
                anchor = '    if state.managed_candidate_adoption is None:\n        yield lambda: None'
                if content.count(anchor) != 1:
                    raise RuntimeError('later-kind authority negative-control anchor changed')
                authority.write_text(content.replace(anchor,
                    '    if (state.managed_candidate_adoption is None\n            or (state.transaction_kind != "IMPLEMENTATION" and not git_effect)):\n        yield lambda: None', 1))
            elif name == 'revoked_partial_profile':
                profile = root / 'src/engineering_platform/validation_profile.py'
                content = profile.read_text()
                if 'import subprocess\n' not in content:
                    content = content.replace('import sys\n', 'import sys\nimport subprocess\n', 1)
                start = content.index('def changed_paths(')
                end = content.index('\ndef main(', start)
                content = content[:start] + '''def changed_paths(root: Path, base: str) -> tuple[str, ...]:
    completed = subprocess.run(("git", "diff", "--name-only", f"{base}...HEAD"), cwd=root, text=True, capture_output=True, check=False)
    if completed.returncode:
        return ()
    return tuple(completed.stdout.splitlines())
''' + content[end:]
                profile.write_text(content)
            else:
                provider = root / 'src/engineering_platform/providers.py'
                transport = provider.read_text()
                anchor = '            stdout, stderr = process.communicate(timeout=timeout)'
                if transport.count(anchor) != 1:
                    raise RuntimeError('publication heartbeat control anchor changed')
                transport = transport.replace(anchor,
                    '            with process_effect_start():\n                stdout, stderr = process.communicate(timeout=timeout)', 1)
                provider.write_text(transport)
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
