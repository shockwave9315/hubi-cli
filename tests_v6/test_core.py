from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hubi6.adapters.git import normalized_origin, health, reference, pin_workspace
from hubi6.adapters.systemd import parse_units
from hubi6.config import Config, private_dir
from hubi6.core.model import (HubiError, Record, RepoHealth, RepoRef, Session, State, Unit,
                              derive_state, display, key_id, names, safe_path, tmux_format, validate_instance)
from hubi6.storage import Store, atomic_json, private_json
from hubi6.core.model import actions, phase_transition
from hubi6.tui import Input


class CoreTests(unittest.TestCase):
    def record(self):
        ref = RepoRef("/tmp/repo", 1, 2, "/tmp/repo/.git", 1, 3, 4)
        return Record("a"*20, os.getuid(), "codex", "primary", key_id("codex", ref.path, "primary"), "b"*16, ref)

    def test_state_matrix(self):
        live = Session("s", "$0", "%0", False, 1, 0, "")
        dead = replace(live, dead=True)
        active = Unit("u", "active")
        expected = {(False, False): State.STOPPED, (False, True): State.ORPHANED,
                    (True, False): State.STALE, (True, True): State.RUNNING}
        for session in (None, live):
            for unit in (None, active):
                with self.subTest(session=bool(session), unit=bool(unit)):
                    self.assertEqual(derive_state(session, unit, known=True), expected[bool(session), bool(unit)])
                    self.assertEqual(derive_state(session, unit, known=False), State.UNKNOWN)
                    self.assertEqual(derive_state(session, unit, known=True, valid=False), State.UNKNOWN)
        self.assertEqual(derive_state(dead, None, known=True), State.EXITED)
        self.assertEqual(derive_state(dead, active, known=True), State.ORPHANED)
        self.assertEqual(derive_state(replace(live, attached=3), active, known=True), State.ATTACHED)
        self.assertEqual(derive_state(live, Unit("u"), known=True), State.UNKNOWN)
        self.assertEqual(derive_state(None, None, known=True, starting=True), State.STARTING)

    def test_namespace_and_generation(self):
        r = self.record()
        self.assertTrue(r.scope.startswith("hubi6-"+r.runtime))
        self.assertNotEqual(r.scope, replace(r, runtime="c"*20).scope)
        self.assertNotEqual(r.scope, replace(r, generation="c"*16).scope)
        self.assertNotEqual(r.key, key_id("codex", "/tmp/other/repo", "primary"))
        self.assertNotEqual(r.key, key_id("terminal", r.repo.path, "primary"))

    def test_transition_rules_and_actions(self):
        self.assertEqual(phase_transition("prepared", "launch"), "launching")
        self.assertEqual(phase_transition("launching", "ready"), "running")
        self.assertEqual(phase_transition("running", "stop"), "stopping")
        for phase, event in (("running", "launch"), ("prepared", "ready"), ("whatever", "stop")):
            with self.assertRaises(HubiError):
                phase_transition(phase, event)
        self.assertNotIn("stop", actions(State.UNKNOWN))
        self.assertNotIn("attach", actions(State.ORPHANED))
        self.assertIn("inspect", actions(State.UNKNOWN))

    def test_invalid_identity(self):
        for bad in ("hubi-codex-x.scope", "*", "a"*19, "a"*20+"\n"):
            with self.subTest(bad=bad), self.assertRaises(HubiError):
                names(bad, "codex", "b"*20, "c"*16)

    def test_instances_ascii_in_all_locales_F10(self):
        for locale in ("C", "C.UTF-8", "en_US.UTF-8", "pl_PL.UTF-8"):
            with patch.dict(os.environ, {"LC_ALL": locale}):
                for good in ("primary", "C_2", "a"*32):
                    self.assertEqual(validate_instance(good), good)
                for bad in ("é1", "a"*33, "_x", "x\n", "*", ""):
                    with self.subTest(locale=locale, bad=bad), self.assertRaises(HubiError):
                        validate_instance(bad)

    def test_tmux_escaping_F1(self):
        for text in ("C#Sharp", "a#(touch OWNED)b", "#{pid}", "#H#S", "sp ace", "żółć", "$(touch OWNED)", "`id`", "*?[x]"):
            escaped = tmux_format(text)
            self.assertEqual(escaped.replace("##", "#"), text)
            self.assertEqual(escaped.count("#"), 2*text.count("#"))

    def test_control_characters_F15(self):
        for bad in ("a\nb", "a\x1bb", "a\x00b", "a\x7fb"):
            with self.assertRaises(HubiError):
                safe_path(bad)
            self.assertNotIn("\x1b", display(bad))
            self.assertNotIn("\n", display(bad))
        self.assertEqual(display("żółć $() #"), "żółć $() #")
        self.assertNotIn("\u202e", display("a\u202eb"))

    def test_metadata_roundtrip(self):
        r = self.record()
        self.assertEqual(Record.decode(r.encoded(), r.runtime, os.getuid()), r)

    def test_metadata_rejects_foreign_or_malformed_F11(self):
        r = self.record()
        for change in ({"uid": os.getuid()+1}, {"runtime": "c"*20}, {"schema": 5}, {"key": "*"},
                       {"scope": "hubi-codex-x.scope"}, {"invocation": "bogus"}, {"pane": "%0;kill-server"},
                       {"phase": "whatever"}, {"created": float("nan")}, {"instance": "é1"}):
            with self.subTest(change=change), self.assertRaises(HubiError):
                Record.parse(dict(r.data(), **change), r.runtime, os.getuid())
        for text in ("", "!", "a"*70000, "W10=", "bnVsbA=="):
            with self.subTest(text=text[:20]), self.assertRaises(HubiError):
                Record.decode(text, r.runtime, os.getuid())

    def test_git_origin_matching(self):
        for value in ("https://github.com/OWNER/Repo.git", "git@github.com:Owner/Repo.git", "ssh://git@github.com/Owner/Repo", "https://u:password@github.com/owner/repo/"):
            self.assertEqual(normalized_origin(value), "github.com/owner/repo")
        for value in ("https://evilgithub.com/a/b", "/tmp/a", "https://github.com/a/b/extra", "http://github.com.evil/a/b", "git@elsewhere:a/b"):
            self.assertEqual(normalized_origin(value), "")

    def test_systemd_unknown_parsing(self):
        base = "Id=x.scope\nLoadState=loaded\nActiveState=active\nInvocationID="+"a"*32+"\nControlGroup=/x.scope\nDescription=test\n"
        self.assertEqual(parse_units(base)["x.scope"].active, "active")
        for text in (base+"ActiveState=inactive\n", base.replace("active", "nonsense"),
                     base.replace("a"*32, ""), base.replace("ControlGroup=/x.scope\n", ""), "Id=x.scope\n"):
            self.assertEqual(parse_units(text)["x.scope"].active, "unknown")

    def test_runtime_path_fallback_F9(self):
        with patch.dict(os.environ, {}, clear=True):
            a = Config.load()
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}):
            b = Config.load()
        self.assertEqual(a.runtime, b.runtime)
        self.assertEqual(a.runtime_id, b.runtime_id)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp"}), self.assertRaises(HubiError):
            Config.load()

    def test_private_directories_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            private_dir(p)
            p.chmod(0o755)
            with self.assertRaises(HubiError):
                private_dir(p)
            p.chmod(0o700)
            atomic_json(p/"meta", {"x": 1})
            self.assertEqual(private_json(p/"meta"), {"x": 1})
            self.assertEqual((p/"meta").stat().st_mode & 0o777, 0o600)
            (p/"link").symlink_to(p/"meta")
            with self.assertRaises(OSError):
                private_json(p/"link")

    def test_repo_identity_replacement_and_invalid_F5_F6(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"repo"
            path.mkdir()
            subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
            ref = pin_workspace(reference(path))
            self.assertEqual(health(ref), RepoHealth.OK)
            # Ordinary Git administrative modifications must not mark REPLACED.
            (path/".git"/"index.lock").touch()
            self.assertEqual(health(ref), RepoHealth.OK)
            (path/".git"/"HEAD").write_text("garbage")
            self.assertEqual(health(ref), RepoHealth.INVALID)
            path.rename(Path(temp)/"moved")
            self.assertEqual(health(ref), RepoHealth.MISSING)
            path.mkdir()
            subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
            self.assertEqual(health(ref), RepoHealth.REPLACED)

    def test_git_optional_locks_F8(self):
        from hubi6.adapters.git import Git
        self.assertEqual(Git(Config.load(), None).env()["GIT_OPTIONAL_LOCKS"], "0")


class InputTests(unittest.TestCase):
    def feed(self, data):
        rd, wr = os.pipe()
        os.write(wr, data)
        os.close(wr)
        self.addCleanup(os.close, rd)
        return Input(rd)

    def test_line_and_eof(self):
        reader = self.feed(b"a\n")
        self.assertEqual(reader.line(), "a")
        self.assertIsNone(reader.line())

    def test_arrows_and_escapes(self):
        for bad in (b"\x1b[A\n", b"\x1b[5~\n", b"a\x1bx\n", b"\xff\n"):
            self.assertEqual(self.feed(bad).line(), "")

    def test_multiline_bracketed_paste(self):
        reader = self.feed(b"\x1b[200~d\nyes\nx\n\x1b[201~\na\n")
        self.assertEqual(reader.line(), "")
        self.assertEqual(reader.line(), "a")

    def test_incomplete_paste_times_out_without_enter(self):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(os.close, wr)
        os.write(wr, b"\x1b[200~d")
        self.assertEqual(Input(rd, paste_timeout=.02).line(), "")

    def test_backspace(self):
        self.assertEqual(self.feed("é\x7fa\n".encode()).line(), "a")
