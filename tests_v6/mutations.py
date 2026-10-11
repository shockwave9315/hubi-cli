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
