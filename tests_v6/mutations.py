"""Small regression guards on disposable copies, never the working source."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tests_v6.support import LAB


def main():
    assert LAB, 'Mutation tests require the marked disposable UID'
    source = Path(__file__).resolve().parent.parent
    cases = [
        ('line-quarantine', 'hubi6/tui.py',
         '        self.discard_burst()\n        self.echo_buffer.clear()',
         '        self.echo_buffer.clear()',
         'tests_v6.test_polish.PolishPtyTests.test_line_quarantine_blocks_split_burst_without_confirmation',
         'AssertionError'),
        ('optional-git-locks', 'hubi6/adapters/git.py',
         '        env["GIT_OPTIONAL_LOCKS"] = "0"',
         '        env.pop("GIT_OPTIONAL_LOCKS", None)',
         'tests_v6.test_fixset.FixsetRuntimeTests.test_git_read_status_write_deterministic_F8',
         'HubiError: git failed (exit 49)'),
        ('session-umask', 'hubi6/worker.py',
         '    os.umask(mask)\n    os.execvpe',
         '    os.execvpe',
         'tests_v6.test_umask.UmaskRuntimeTests.test_session_gets_the_launcher_umask_0002',
         'AssertionError: 384 != 436'),
        ('umask-fail-closed', 'hubi6/worker.py',
         '    mask = session_umask(payload)',
         '    mask = payload.get("umask", 0o022)',
         'tests_v6.test_umask.UmaskRuntimeTests.test_invalid_umask_refuses_the_start',
         'AssertionError: HubiError not raised'),
        # The guard's hard rules (brief from the owner, 25.10).
        ('guard-pause-rule', 'hubi6/guard.py',
         '            if state.get("STATE") in PAUSED and (name != mine',
         '            if False and (name != mine',
         'tests_v6.test_guard.GuardTests.test_pause_refuses_a_new_cp_or_codex_but_allows_the_same_restart',
         'AssertionError: HubiError not raised'),
        ('guard-weekly-lock', 'hubi6/guard.py',
         '        if state.get("STATE") == WEEKLY:\n            raise',
         '        if False:\n            raise',
         'tests_v6.test_guard.GuardTests.test_weekly_lock_says_how_to_resume_by_hand',
         'AssertionError: HubiError not raised'),
        ('guard-keeps-settings', 'hubi6/guard.py',
         '        env = dict(setup.env)\n',
         '        env = dict(DEFAULT_ENV)\n',
         'tests_v6.test_guard.GuardTests.test_new_cp_in_running_keeps_every_setting',
         'AssertionError'),
        ('guard-new-inode', 'hubi6/guard.py',
         '            shutil.copy2(source, temp)               # like cp -p: mode and times of the version\n'
         '            os.replace(temp, self.dir/"watchdog.sh")',
         '            shutil.copyfile(source, self.dir/"watchdog.sh")',
         'tests_v6.test_guard.GuardTests.test_version_switch_restarts_with_the_same_setup',
         'AssertionError'),
        ('guard-remember-after-start', 'hubi6/guard.py',
         '        for _ in range(8):\n            self.sleep(.25)',
         '        self.remember(replace(setup, env=tuple(sorted(env.items()))))\n        for _ in range(8):\n            self.sleep(.25)',
         'tests_v6.test_guard.GuardTests.test_a_failed_start_keeps_the_last_good_setup',
         'AssertionError'),
        ('guard-goes-back', 'hubi6/guard.py',
         '                    self.go_back(setup, e, back)\n',
         '',
         'tests_v6.test_guard.GuardTests.test_a_version_that_rejects_the_setup_brings_the_old_version_back',
         'AssertionError'),
        ('guard-threshold-check', 'hubi6/guard.py',
         '    if re.fullmatch(r"[0-9]{1,5}", text) and low <= int(text) <= high:',
         '    if text.isdigit():',
         'tests_v6.test_guard.GuardTests.test_bad_thresholds_are_refused_before_the_guard_stops',
         'AssertionError'),
        ('guard-thresholds-keep-settings', 'hubi6/guard.py',
         '        return replace(self, env=tuple(sorted({**dict(self.env), **values}.items())))',
         '        return replace(self, env=tuple(sorted({**dict(DEFAULT_ENV), **values}.items())))',
         'tests_v6.test_guard.GuardTests.test_new_thresholds_restart_the_same_guard_and_keep_everything_else',
         'AssertionError'),
    ]
    for label, filename, before, after, test, expected in cases:
        with tempfile.TemporaryDirectory(prefix='hubi6-mutation-', dir=Path.home()) as tmp:
            root = Path(tmp)
            for name in ('hubi6', 'tests_v6', 'config', 'systemd'):
                shutil.copytree(source/name, root/name, ignore=shutil.ignore_patterns('__pycache__'))
            path = root/filename
            original = path.read_text()
            assert original.count(before) == 1, 'Mutation no longer matches source'
            path.write_text(original.replace(before, after))
            result = subprocess.run([sys.executable, '-m', 'unittest', test, '-v'], cwd=root,
                                    env=dict(os.environ, PYTHONPATH=str(root)),
                                    capture_output=True, text=True, timeout=30)
            output = result.stdout+result.stderr
            assert result.returncode != 0 and expected in output, label+':\n'+output
            print(label+': mutant detected by '+test, flush=True)


if __name__ == '__main__':
    main()
