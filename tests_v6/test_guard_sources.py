"""The guard's own files in this repository (guard/) and guard/install.sh.

Unit tests install into temporary directories only, never into the live guard
directory. The guard's own test suite (guard/tests/run.sh) runs in the lab UID.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from hubi6 import guard as G
from tests_v6.support import LAB

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT/"guard"
FILES = ["README.md", "limits.py", "reset_credit.py", "watchdog.sh", "watchdog.v4.sh", "watchdog.v48.sh", "watchdog.v49.sh"]


def install(dest):
    return subprocess.run(["bash", str(SRC/"install.sh"), str(dest)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "WD_DIR": ""})


class GuardSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name)/"codex-watchdog"

    def test_the_repository_has_a_working_guard(self):
        for name in FILES+["install.sh", "tests/run.sh"]:
            self.assertTrue((SRC/name).is_file(), name)
        # Hubi names the running version by the kept file identical to watchdog.sh.
        self.assertEqual((SRC/"watchdog.sh").read_bytes(), (SRC/"watchdog.v49.sh").read_bytes())
        for name in ["install.sh", "watchdog.sh", "watchdog.v4.sh", "watchdog.v48.sh", "watchdog.v49.sh", "tests/run.sh"]:
            self.assertTrue(os.access(SRC/name, os.X_OK), name)
            subprocess.run(["bash", "-n", str(SRC/name)], check=True)
        for name in ["limits.py", "reset_credit.py"]:
            compile((SRC/name).read_text(), name, "exec")

    def test_install_copies_every_file_once(self):
        p = install(self.dest)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skopiowano 7 z 7", p.stdout)
        self.assertEqual(sorted(f.name for f in self.dest.iterdir()), FILES)   # no temp file left behind
        for name in FILES:
            self.assertEqual((self.dest/name).read_bytes(), (SRC/name).read_bytes(), name)
        self.assertTrue(os.access(self.dest/"watchdog.sh", os.X_OK))
        p = install(self.dest)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skopiowano 0 z 7", p.stdout)
        self.assertEqual(G.version_label(self.dest/"watchdog.v49.sh"), "v4.9")

    def test_install_never_overwrites_another_file(self):
        self.dest.mkdir()
        (self.dest/"limits.py").write_text("# the owner's own edit\n")
        (self.dest/"state.codex-praca").write_text("CP=CP20\nSTATE=PAUSED_5H\n")
        p = install(self.dest)
        self.assertEqual(p.returncode, 1)
        self.assertIn("inny plik już jest", p.stderr)
        self.assertIn("nic nie skopiowano", p.stderr)
        self.assertEqual(sorted(f.name for f in self.dest.iterdir()), ["limits.py", "state.codex-praca"])
        self.assertEqual((self.dest/"limits.py").read_text(), "# the owner's own edit\n")

    def test_a_guard_switched_to_another_kept_version_is_not_a_conflict(self):
        install(self.dest)
        (self.dest/"watchdog.sh").write_bytes((SRC/"watchdog.v48.sh").read_bytes())   # hubi guard version v4.8
        p = install(self.dest)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.dest/"watchdog.sh").read_bytes(), (SRC/"watchdog.v48.sh").read_bytes())

    def test_an_own_readme_stays(self):
        self.dest.mkdir()
        (self.dest/"README.md").write_text("notatki właściciela\n")
        p = install(self.dest)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skopiowano 6 z 7", p.stdout)
        self.assertEqual((self.dest/"README.md").read_text(), "notatki właściciela\n")


@unittest.skipUnless(LAB, "the guard's own tests use tmux; they run only in the lab UID")
class GuardOwnTests(unittest.TestCase):
    def test_guard_test_suite_passes(self):
        p = subprocess.run(["bash", str(SRC/"tests/run.sh")], capture_output=True, text=True, timeout=1200)
        tail = "\n".join(p.stdout.splitlines()[-15:])
        self.assertEqual(p.returncode, 0, [line for line in p.stdout.splitlines() if line.startswith("FAIL")] or tail)
        self.assertIn("FAIL=0", p.stdout)


if __name__ == "__main__":
    unittest.main()
