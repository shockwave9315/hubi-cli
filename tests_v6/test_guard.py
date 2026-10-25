"""The Codex limit guard in Hubi: reading, steering and its hard safety rules.

Unit tests play systemd with a fake runner on a copy of the guard directory.
The lab test runs real systemd-run with a stand-in guard script under the lab UID.
"""
from dataclasses import replace
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from hubi6 import guard as G
from hubi6.config import Config
from hubi6.core.model import HubiError
from hubi6.i18n import pl
from tests_v6.support import LAB

UNIT = "praca-codex-watchdog"
LIVE_ENV = (("WD_IDLE_MIN", "720"), ("WD_LOW", "7"), ("WD_MAX_HOURS", "48"), ("WD_WEEK_LOW", "1"))


class FakeSystemd:
    """systemctl, systemd-run, journalctl and limits.py for one transient unit."""

    def __init__(self, directory, unit=UNIT):
        self.dir, self.unit = directory, unit
        self.calls = []
        self.active = False
        self.argv, self.env, self.props = [], {}, {}
        self.cwd = None
        self.since = 1792823690.0
        self.refuse = ""                  # journal text of a guard that exits right after start
        self.refuse_when = None           # (directory, argv) -> journal text, "" = it starts
        self.journal_text = ""
        self.limits_out = "status=ok five_left=64 five_reset=1792900000 week_left=53 week_reset=1793300000 allowed=true"
        self.limits_runs = 0
        self.starts = 0

    def run(self, argv, *, env=None, input=None, timeout=10, check=True, cwd=None, interactive=False):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out, rc = "", 0
        if argv[:3] == ["systemctl", "--user", "show"]:
            out = self.show()
        elif argv[:3] == ["systemctl", "--user", "stop"]:
            assert argv[3] == self.unit+".service"
            self.active = False
        elif argv[:3] == ["systemctl", "--user", "reset-failed"]:
            pass
        elif argv[0] == "systemd-run":
            assert "--unit="+self.unit in argv and "--collect" in argv and "--same-dir" in argv and "--user" in argv
            split = argv.index("--")
            self.env = dict(a[len("--setenv="):].split("=", 1) for a in argv[:split] if a.startswith("--setenv="))
            self.props = dict(argv[i+1].split("=", 1) for i in range(split) if argv[i] == "-p")
            self.argv, self.cwd = argv[split+1:], cwd
            self.starts += 1
            why = self.refuse or (self.refuse_when(self.dir, self.argv) if self.refuse_when else "")
            if why:
                self.journal_text, self.active = why, False
            else:
                self.active, self.since = True, time.time()
        elif argv[0] == "journalctl":
            out = self.journal_text
        elif argv[:2] == ["python3", "-I"] and argv[2].endswith("/limits.py"):
            self.limits_runs += 1
            out = self.limits_out
        else:
            raise AssertionError(f"unexpected command {argv}")
        if check and rc:
            raise HubiError(f"{argv[0]} failed (exit {rc})")
        return subprocess.CompletedProcess(argv, rc, out, "")

    def show(self):
        lines = [f"Id={self.unit}.service"]
        if not self.active:
            return "\n".join(lines+["LoadState=not-found", "ActiveState=inactive", "MainPID=0", "ActiveEnterTimestamp=",
                                    "ExecStart=", "Environment=", "Restart=no", "RestartUSec=100ms",
                                    "RestartPreventExitStatus=", "WorkingDirectory="])
        words = " ".join(["/usr/bin/bash"]+self.argv[1:])
        return "\n".join(lines+[
            "LoadState=loaded", "ActiveState=active", "MainPID=4242", f"ActiveEnterTimestamp=@{self.since:.0f}",
            f"ExecStart={{ path=/usr/bin/bash ; argv[]={words} ; ignore_errors=no ; start_time=[@{self.since:.0f}] ; "
            f"stop_time=[n/a] ; pid=4242 ; code=(null) ; status=0/0 }}",
            "Environment="+" ".join(f"{k}={v}" for k, v in sorted(self.env.items())),
            "Restart="+self.props.get("Restart", "no"), "RestartUSec="+self.props.get("RestartSec", "100ms"),
            "RestartPreventExitStatus="+self.props.get("RestartPreventExitStatus", ""), f"WorkingDirectory={self.dir}"])

    def launch(self, cp="CP20", codex="codex/praca", contact="codex/praca/temp-rev", env=None):
        """The guard as the README starts it, outside Hubi."""
        self.argv = ["bash", "./watchdog.sh", cp, codex, contact]
        self.env = dict(env or (("PATH", os.environ["PATH"]),)+LIVE_ENV)
        self.props = {"Restart": "on-failure", "RestartSec": "30s", "RestartPreventExitStatus": "1 2 3"}
        self.active, self.since = True, time.time()+5

    def commands(self, word):
        return [c for c in self.calls if word in " ".join(c)]


class Conf:
    def __init__(self, base, directory):
        self.home, self.state_base, self.guard_dir, self.guard_unit = base, base/"state", directory, UNIT

    def prepare(self):
        self.state_base.mkdir(mode=0o700, parents=True, exist_ok=True)


class FakeGit:
    def resolve(self, name):
        if name not in ("praca", "hubi-cli"):
            raise HubiError("Repository missing, invalid, replaced or outside repo root")
        return name


class App:
    def __init__(self, conf, runner):
        self.c, self.r, self.git = conf, runner, FakeGit()


def guard_dir(base):
    d = base/"codex-watchdog"
    d.mkdir()
    (d/"watchdog.v48.sh").write_text("#!/usr/bin/env bash\n# v4.8\n")
    (d/"watchdog.v49.sh").write_text("#!/usr/bin/env bash\n# v4.9, a little longer\n")
    (d/"watchdog.v4.sh").write_text("#!/usr/bin/env bash\n# v4\n")
    shutil.copy2(d/"watchdog.v49.sh", d/"watchdog.sh")
    (d/"watchdog.sh").chmod(0o775)
    (d/"limits.py").write_text("raise SystemExit('the fake runner answers for limits.py')\n")
    (d/"reset_credit.py").write_text("raise SystemExit('Hubi must never run this')\n")
    (d/"README.md").write_text("Strażnik\n")
    (d/"log").write_text("".join(f"2026-10-24T13:3{n}:58Z CP20 codex/praca RUNNING: wpis {n}\n" for n in range(8)))
    state(d, "codex-praca", CP="CP20", STATE="RUNNING", PAUSE_WINDOW="1792856773")
    return d


def state(d, key, **values):
    (d/("state."+key)).write_text("".join(f"{k}={v}\n" for k, v in values.items()))


class Fixture(unittest.TestCase):
    """A copy of the guard directory, a fake systemd and a PATH with codex."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.dir = guard_dir(self.base)
        bin_dir = self.base/"bin"
        bin_dir.mkdir()
        (bin_dir/"codex").write_text("#!/bin/sh\n")
        (bin_dir/"codex").chmod(0o755)
        self.path = f"{bin_dir}:/usr/bin:/bin"
        env = patch.dict(os.environ, {"PATH": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.sd = FakeSystemd(self.dir)
        self.clock = [2000000000.0]
        self.g = G.Guard(App(Conf(self.base, self.dir), self.sd), clock=lambda: self.clock[0], sleep=lambda s: None)
        self.before = sorted(p.name for p in self.dir.iterdir())

    def tearDown(self):
        # Hard rules: no consent file appears, nothing is deleted, no reset helper, never a message to Codex.
        after = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(after, self.before)
        self.assertFalse(self.sd.commands("reset_credit"))
        self.assertFalse(self.sd.commands("send"))
        self.assertFalse(self.sd.commands("tmux"))


class GuardTests(Fixture):
    # ---- reading

    def test_status_reads_the_running_unit_like_the_live_one(self):
        self.sd.launch()
        s = self.g.status()
        self.assertTrue(s.running)
        self.assertEqual((s.setup.cp, s.setup.codex, s.setup.contact), ("CP20", "codex/praca", "codex/praca/temp-rev"))
        self.assertEqual(dict(s.setup.env)["WD_MAX_HOURS"], "48")
        self.assertEqual(dict(s.setup.restart), {"Restart": "on-failure", "RestartSec": "30s", "RestartPreventExitStatus": "1 2 3"})
        self.assertEqual(s.versions, ["v4.9"])
        self.assertEqual(s.state["STATE"], "RUNNING")
        self.assertIsNone(s.resume_at)                 # no pause, no resume time
        self.assertEqual(len(s.log), 5)
        line = G.summary(s)
        for word in ("działa", "CP20", "RUNNING", "v4.9", "codex/praca → codex/praca/temp-rev"):
            self.assertIn(word, line)
        text = "\n".join(G.report(s))
        self.assertIn("WD_MAX_HOURS=48", text)
        self.assertNotIn("PATH=", text)
        self.assertIn("restart on-failure po 30s", text)

    def test_pause_shows_the_resume_time_in_polish_time(self):
        self.sd.launch()
        state(self.dir, "codex-praca", CP="CP20", STATE="PAUSED_5H", PAUSE_WINDOW="1792856773")
        s = self.g.status()
        self.assertEqual(s.resume_at, 1792856773+120)
        # 1792856893 = 24.10.2026 15:48:13 UTC = 17:48 in Warsaw (summer time)
        self.assertIn("wznowienie 24.10 17:48", G.summary(s))
        self.assertIn("pauza do odnowienia limitu 5 h", "\n".join(G.report(s)))
        # A changed WD_RESUME_DELAY is honoured.
        self.sd.env["WD_RESUME_DELAY"] = "600"
        self.assertEqual(self.g.status().resume_at, 1792856773+600)

    def test_polish_time_follows_winter_time(self):
        moment = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(G.when(moment), "26.10 13:00")
        self.assertEqual(G.log_line("2026-10-26T12:00:00Z CP20 codex/praca RUNNING: x", "codex/praca"),
                         "26.10 13:00:00 CP20 RUNNING: x")
        self.assertEqual(G.log_line("2026-10-26T12:00:00Z CP20 codex/inne RUNNING: x", "codex/praca"),
                         "26.10 13:00:00 CP20 codex/inne RUNNING: x")

    def test_log_text_cannot_inject_terminal_controls(self):
        self.assertNotIn("\033", G.log_line("2026-10-26T12:00:00Z CP20 codex/praca RUNNING: \033[2Jx"))
        self.assertNotIn("\033", G.log_line("\033]0;title\007"))

    def test_weekly_lock_says_how_to_resume_by_hand(self):
        state(self.dir, "codex-praca", CP="CP20", STATE="WEEKLY_STOP")
        self.sd.launch()
        self.g.status(remember=True)
        self.sd.active = False                         # the guard ended itself at the weekly limit
        s = self.g.status()
        self.assertFalse(s.running)
        self.assertEqual(s.source, "saved")
        text = "\n".join(G.report(s))
        self.assertIn("blokada tygodniowa", text)
        self.assertIn("„CP20 wznów”, usuń plik state.codex-praca i włącz strażnika ponownie", text)
        self.assertIn("ostatnio CP20", G.summary(s))
        with self.assertRaises(HubiError) as e:
            self.g.start()
        self.assertIn("delete that file and start the guard again", str(e.exception))
        self.assertIn("usuń ten plik", pl(e.exception))
        self.assertEqual(self.sd.starts, 0)

    def test_unknown_systemd_never_guesses(self):
        self.sd.show = lambda: ""
        s = self.g.status()
        self.assertEqual(s.unit, "unknown")
        self.assertIn("stan nieznany", G.summary(s))
        for action in (self.g.stop, self.g.start, lambda: self.g.change_cp("CP21")):
            with self.assertRaises(HubiError):
                action()
        self.assertFalse(self.sd.commands("systemd-run"))
        self.assertFalse(self.sd.commands("stop"))

    def test_unconfigured_guard_is_invisible(self):
        (self.dir/"watchdog.sh").unlink()
        self.before.remove("watchdog.sh")
        s = self.g.status()
        self.assertFalse(s.configured)
        self.assertEqual(G.summary(s), "")
        self.assertEqual(s.data(), {"configured": False})
        with self.assertRaises(HubiError):
            self.g.start()

    def test_unit_started_another_way_is_shown_but_not_changed(self):
        self.sd.launch()
        self.sd.argv = ["bash", "./inny.sh", "CP20", "codex/praca", "claude/praca"]
        s = self.g.status()
        self.assertTrue(s.running)
        self.assertIn("inaczej", pl(s.problem))
        with self.assertRaises(HubiError):
            self.g.change_cp("CP21")
        self.assertFalse(self.sd.commands("stop"))
        self.sd.launch(env=(("PATH", self.path), ("WD_BAR_RE", "(x y)")))
        self.assertTrue(self.g.status().problem)

    # ---- names

    def test_names_are_hubi_names_never_panes(self):
        self.assertEqual(G.canonical("codex/praca/1"), "codex/praca")
        self.assertEqual(G.canonical("codex/praca/primary"), "codex/praca")
        self.assertEqual(G.canonical("codex/praca/temp-rev"), "codex/praca/temp-rev")
        for bad in ("%5", "%14", "praca", "codex/", "codex/praca/x/y", "terminal/praca", "codex/pra ca", ""):
            with self.assertRaises(HubiError, msg=bad):
                G.canonical(bad)
        with self.assertRaises(HubiError):
            G.canonical("claude/praca", kind="codex")
        with self.assertRaises(HubiError):
            G.Setup("CP20", "codex/praca", "codex/praca")        # the contact is never the guarded Codex
        with self.assertRaises(HubiError):
            G.Setup("CP20", "codex/praca", "codex/hubi-cli")     # another Codex only under its own name
        with self.assertRaises(HubiError):
            G.Setup("20", "codex/praca", "claude/praca")
        G.Setup("CP15d", "codex/praca", "codex/praca/temp-rev")

    def test_who_rejects_pane_numbers_and_unknown_repositories(self):
        self.sd.launch()
        for codex, contact in (("%5", "claude/praca"), ("codex/praca", "%8"), ("codex/nieznane", "claude/praca"),
                               ("codex/praca", "claude/nieznane")):
            with self.assertRaises(HubiError, msg=(codex, contact)):
                self.g.change_who(codex, contact)
        self.assertFalse(self.sd.commands("stop"))
        self.assertTrue(self.sd.active)

    # ---- steering

    def test_new_cp_in_running_keeps_every_setting(self):
        self.sd.launch()
        old, new = self.g.change_cp("CP21")
        self.assertEqual((old.cp, new.cp), ("CP20", "CP21"))
        self.assertEqual(self.sd.argv, ["bash", "./watchdog.sh", "CP21", "codex/praca", "codex/praca/temp-rev"])
        self.assertEqual(self.sd.env, dict((("PATH", self.path),)+LIVE_ENV))
        self.assertEqual(self.sd.props, {"Restart": "on-failure", "RestartSec": "30s", "RestartPreventExitStatus": "1 2 3"})
        self.assertEqual(self.sd.cwd, str(self.dir))
        stop = self.sd.calls.index(self.sd.commands("systemctl --user stop")[0])
        run = self.sd.calls.index(self.sd.commands("systemd-run")[0])
        self.assertLess(stop, run)
        self.assertEqual(self.g.saved().cp, "CP21")   # a later start without arguments uses it

    def test_pause_refuses_a_new_cp_or_codex_but_allows_the_same_restart(self):
        self.sd.launch()
        for paused in ("PAUSE_REQUESTED", "PAUSED_5H", "RESUME_REQUESTED"):
            state(self.dir, "codex-praca", CP="CP20", STATE=paused, PAUSE_WINDOW="1792856773")
            with self.assertRaises(HubiError, msg=paused) as e:
                self.g.change_cp("CP21")
            self.assertIn("lose the pause", str(e.exception))
            self.assertIn("zgubiłby stan pauzy", pl(e.exception))
            with self.assertRaises(HubiError):
                self.g.change_who("codex/hubi-cli", "claude/praca")
        self.assertFalse(self.sd.commands("systemctl --user stop"))
        self.assertTrue(self.sd.active)
        # The same CP and Codex with another contact keeps the pause: allowed.
        self.g.change_who("codex/praca", "claude/praca")
        self.assertEqual(self.sd.argv[2:], ["CP20", "codex/praca", "claude/praca"])
        # Stop and start again with the same CP: allowed in a pause too.
        self.assertTrue(self.g.stop())
        self.g.start()
        self.assertEqual(self.sd.argv[2:], ["CP20", "codex/praca", "claude/praca"])

    def test_explicit_start_cannot_drop_a_saved_pause(self):
        self.sd.launch()
        self.g.stop()
        state(self.dir, "codex-praca", CP="CP20", STATE="PAUSED_5H", PAUSE_WINDOW="1792856773")
        with self.assertRaises(HubiError):
            self.g.start(G.Setup("CP21", "codex/praca", "claude/praca"))
        self.assertEqual(self.sd.starts, 0)
        self.g.start(G.Setup("CP20", "codex/praca", "claude/praca"))
        self.assertEqual(dict(self.g.status().setup.env)["WD_MAX_HOURS"], "48")   # settings of the last guard

    def test_new_codex_must_not_carry_a_weekly_lock(self):
        self.sd.launch()
        state(self.dir, "codex-hubi-cli", CP="CP3", STATE="WEEKLY_STOP")
        self.before.append("state.codex-hubi-cli")
        self.before.sort()
        with self.assertRaises(HubiError):
            self.g.change_who("codex/hubi-cli", "claude/hubi-cli")
        self.assertTrue(self.sd.active)
        self.assertEqual(self.sd.argv[2], "CP20")

    def test_start_refuses_while_running_and_without_any_setup(self):
        self.sd.launch()
        with self.assertRaises(HubiError):
            self.g.start()
        self.sd.active = False
        with self.assertRaises(HubiError) as e:
            self.g.start()
        self.assertIn("podaj CP", pl(e.exception))
        self.assertEqual(self.sd.starts, 0)

    def test_first_start_uses_the_readme_settings_and_the_callers_path(self):
        setup = self.g.start(G.Setup("CP21", G.canonical("codex/praca/1"), "claude/praca"))
        self.assertEqual(setup.codex, "codex/praca")
        self.assertEqual(self.sd.env, {"PATH": self.path, "WD_IDLE_MIN": "720", "WD_LOW": "7", "WD_WEEK_LOW": "1"})
        self.assertEqual(self.sd.props["RestartPreventExitStatus"], "1 2 3")

    def test_a_path_without_codex_is_refused(self):
        self.sd.launch(env=(("PATH", "/usr/bin:/bin"),)+LIVE_ENV)
        with self.assertRaises(HubiError) as e:
            self.g.change_cp("CP21")
        self.assertIn("codex", pl(e.exception))
        self.assertIn("Previous setup: hubi guard start CP20 codex/praca codex/praca/temp-rev", str(e.exception))
        self.assertIn("Poprzednie ustawienie", pl(e.exception))
        self.assertIn("W PATH strażnika nie ma codex", pl(e.exception))   # the nested reason is Polish too

    def test_a_guard_that_dies_at_once_is_reported_with_its_reason(self):
        self.sd.launch()
        self.sd.refuse = "strażnik dla codex/praca już działa"
        with self.assertRaises(HubiError) as e:
            self.g.change_cp("CP21")
        self.assertIn("już działa", pl(e.exception))
        self.assertIn("zaraz po starcie", pl(e.exception))
        self.assertIn("hubi guard start CP20", pl(e.exception))
        self.assertEqual(self.g.saved().cp, "CP20")        # a failed start never replaces the last good setup

    def test_a_failed_start_keeps_the_last_good_setup(self):
        self.sd.launch()
        self.g.stop()
        self.sd.refuse = "zły próg"
        with self.assertRaises(HubiError) as e:
            self.g.start(G.Setup("CP21", "codex/praca", "claude/praca"))
        self.assertIn("zaraz po starcie: zły próg", pl(e.exception))
        self.assertFalse(self.sd.active)
        self.assertEqual(self.g.saved().words(), "CP20 codex/praca codex/praca/temp-rev")

    def test_a_new_setup_that_dies_brings_the_old_one_back(self):
        self.sd.launch()
        self.sd.refuse_when = lambda d, argv: "zły próg" if argv[2] == "CP21" else ""
        with self.assertRaises(HubiError) as e:
            self.g.change_cp("CP21")
        self.assertIn("nie wystartowało: Strażnik zakończył się zaraz po starcie: zły próg; strażnik działa znowu z CP20",
                      pl(e.exception))
        self.assertTrue(self.sd.active)
        self.assertEqual(self.sd.argv[2:], ["CP20", "codex/praca", "codex/praca/temp-rev"])
        self.assertEqual(self.sd.env["WD_MAX_HOURS"], "48")
        self.assertEqual(self.g.saved().cp, "CP20")

    def test_a_version_that_rejects_the_setup_brings_the_old_version_back(self):
        # The live case: v4.8 takes only claude/… as the contact, the guard runs with codex/praca/temp-rev.
        self.sd.launch()
        self.sd.refuse_when = lambda d, argv: ("użycie: watchdog.sh <CP> <Codex> <Claude>"
                                               if "v4.8" in (d/"watchdog.sh").read_text() and argv[4].startswith("codex/") else "")
        v49 = (self.dir/"watchdog.v49.sh").read_text()
        with self.assertRaises(HubiError) as e:
            self.g.switch_version("v4.8")
        self.assertIn("Wersja v4.8 nie przyjęła tego ustawienia", pl(e.exception))
        self.assertIn("wrócono do v4.9, strażnik działa", pl(e.exception))
        self.assertTrue(self.sd.active)
        self.assertEqual((self.dir/"watchdog.sh").read_text(), v49)
        self.assertEqual(self.g.current_versions(), ["v4.9"])
        self.assertEqual(self.sd.argv[2:], ["CP20", "codex/praca", "codex/praca/temp-rev"])
        # With a Claude contact v4.8 starts.
        self.g.change_who("codex/praca", "claude/praca")
        self.assertTrue(self.g.switch_version("v4.8"))
        self.assertTrue(self.sd.active)

    def test_when_going_back_fails_too_both_reasons_and_the_old_setup_are_shown(self):
        self.sd.launch()
        self.sd.refuse = "zły próg"
        with self.assertRaises(HubiError) as e:
            self.g.switch_version("v4.8")
        text = pl(e.exception)
        self.assertIn("powrót też nie", text)
        self.assertIn("Poprzednie ustawienie: hubi guard start CP20 codex/praca codex/praca/temp-rev", text)
        self.assertEqual(self.g.current_versions(), ["v4.9"])   # the script that ran is back anyway

    def test_without_a_saved_setup_every_paused_codex_counts(self):
        state(self.dir, "codex-praca", CP="CP20", STATE="PAUSED_5H", PAUSE_WINDOW="1792856773")
        state(self.dir, "5", CP="CP17", STATE="PAUSED_5H", PAUSE_WINDOW="1792607871")   # v3.2, a pane number
        self.before = sorted(self.before+["state.5"])
        self.assertIsNone(self.g.saved())
        with self.assertRaises(HubiError) as e:
            self.g.start(G.Setup("CP21", "codex/hubi-cli", "claude/hubi-cli"))
        self.assertIn("state.codex-praca", str(e.exception))
        self.assertEqual(self.sd.starts, 0)
        self.g.start(G.Setup("CP20", "codex/praca", "claude/praca"))   # the paused Codex itself, its own CP
        self.assertTrue(self.sd.active)

    def test_stop_when_nothing_runs(self):
        self.assertFalse(self.g.stop())
        self.assertFalse(self.sd.commands("systemctl --user stop"))

    def test_version_switch_restarts_with_the_same_setup(self):
        self.sd.launch()
        inode = (self.dir/"watchdog.sh").stat().st_ino
        self.assertTrue(self.g.switch_version("v4.8"))
        self.assertEqual((self.dir/"watchdog.sh").read_text(), (self.dir/"watchdog.v48.sh").read_text())
        self.assertNotEqual((self.dir/"watchdog.sh").stat().st_ino, inode)   # never rewritten under a running bash
        self.assertEqual(self.g.current_versions(), ["v4.8"])
        self.assertEqual(self.sd.argv[2:], ["CP20", "codex/praca", "codex/praca/temp-rev"])
        self.assertEqual(self.sd.env["WD_MAX_HOURS"], "48")
        self.assertFalse(self.g.switch_version("48"))          # already that version: nothing restarts
        self.assertEqual(self.sd.starts, 1)
        self.assertTrue(self.g.switch_version("4"))
        self.assertEqual(self.g.current_versions(), ["v4"])

    def test_a_failed_copy_keeps_the_guard_running(self):
        self.sd.launch()
        v49 = (self.dir/"watchdog.sh").read_text()
        with patch("hubi6.guard.shutil.copy2", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(HubiError) as e:
                self.g.switch_version("v4.8")
        self.assertIn("Nie udało się podmienić watchdog.sh (OSError); strażnik działa jak wcześniej", pl(e.exception))
        self.assertTrue(self.sd.active)
        self.assertEqual(self.sd.starts, 1)
        self.assertEqual((self.dir/"watchdog.sh").read_text(), v49)
        self.sd.active = False
        with patch("hubi6.guard.shutil.copy2", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(HubiError) as e:
                self.g.switch_version("v4.8")
        self.assertEqual(pl(e.exception), "Nie udało się podmienić watchdog.sh (OSError)")
        self.assertEqual(self.sd.starts, 1)                  # it was not running: nothing starts

    def test_version_switch_while_stopped_only_copies(self):
        self.assertTrue(self.g.switch_version("v4.8"))
        self.assertEqual(self.sd.starts, 0)

    def test_version_switch_never_destroys_an_unsaved_edit(self):
        (self.dir/"watchdog.sh").write_text("#!/usr/bin/env bash\n# local edit\n")
        with self.assertRaises(HubiError):
            self.g.switch_version("v4.8")
        self.assertIn("local edit", (self.dir/"watchdog.sh").read_text())
        for bad in ("v9.9", "x", "4.8.1"):
            with self.assertRaises(HubiError, msg=bad):
                self.g.switch_version(bad)

    def test_version_labels(self):
        self.assertEqual([label for label, _ in self.g.versions()], ["v4", "v4.8", "v4.9"])
        self.assertEqual(G.version_label("watchdog.v31.sh"), "v3.1")
        self.assertEqual(G.version_label("watchdog.pre-v4.sh"), "")
        self.assertEqual(G.version_file("v4.10"), "watchdog.v410.sh")

    def test_changed_script_after_start_is_flagged(self):
        self.sd.launch()
        self.sd.since = time.time()-3600
        os.utime(self.dir/"watchdog.sh")
        (self.dir/"watchdog.sh").chmod(0o775)          # ctime now: replaced after the start
        s = self.g.status()
        self.assertTrue(s.changed)
        self.assertIn("podmieniony po starcie", "\n".join(G.report(s)))

    def test_limits_at_most_every_two_minutes(self):
        fields, at = self.g.limits()
        self.assertEqual(fields["five_left"], "64")
        self.g.limits()
        self.assertEqual(self.sd.limits_runs, 1)
        self.clock[0] += 121
        self.g.limits()
        self.assertEqual(self.sd.limits_runs, 2)
        s = self.g.status()
        text = G.limits_text(s.limits, s.limits_at, self.clock[0]+300)
        self.assertIn("5 h 64%", text)
        self.assertIn("tydzień 53%", text)
        self.assertIn("sprzed 5 min", text)
        self.sd.limits_out, self.clock[0] = "status=error reason=spawn", self.clock[0]+121
        fields, _ = self.g.limits()
        self.assertEqual(fields, {"status": "error", "reason": "spawn"})

    def test_saved_setup_comes_from_hubi_or_the_running_unit(self):
        self.sd.launch()
        self.g.status()
        self.assertIsNone(self.g.saved())                  # hubi list never writes
        self.g.status(remember=True)
        self.assertEqual(self.g.saved().contact, "codex/praca/temp-rev")
        mode = (self.base/"state"/"guard.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        (self.base/"state"/"guard.json").write_text("{broken")
        self.assertIsNone(self.g.saved())                  # Hubi's own memo never blocks the guard

    def test_json_shape(self):
        self.sd.launch()
        data = self.g.status().data()
        self.assertTrue(data["running"])
        self.assertNotIn("PATH", data["setup"]["env"])
        self.assertEqual(data["setup"]["cp"], "CP20")
        self.assertEqual(data["versions"], ["v4.9"])


class CliTests(Fixture):
    """hubi guard …: the same guard through argparse and the Polish output."""

    def cli(self, *words):
        import contextlib
        import io
        from hubi6 import cli
        args = cli.parser().parse_args(["guard", *words])
        out = io.StringIO()
        with patch("hubi6.guard.Guard", lambda app: self.g), contextlib.redirect_stdout(out):
            code = cli.guard(None, args)
        return code, out.getvalue()

    def test_status_cp_start_and_versions(self):
        self.sd.launch()
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Strażnik: działa", out)
        self.assertIn("CP CP20, cel codex/praca, kontakt codex/praca/temp-rev", out)
        code, out = self.cli("cp", "CP21")
        self.assertIn("Strażnik działa: CP21 codex/praca codex/praca/temp-rev (było CP20)", out)
        code, out = self.cli("version")
        self.assertIn("* v4.9  watchdog.v49.sh", out)
        self.assertIn("  v4.8  watchdog.v48.sh", out)
        code, out = self.cli("log", "-n", "2")
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("24.10 15:37:58 CP20 RUNNING: wpis 7", out)
        code, out = self.cli("stop")
        self.assertIn("Zatrzymano strażnika.", out)
        with self.assertRaises(HubiError):
            self.cli("start", "CP21", "codex/praca")
        code, out = self.cli("start", "CP21", "codex/praca/1", "claude/praca")
        self.assertIn("Strażnik działa: CP21 codex/praca claude/praca", out)
        code, out = self.cli("status", "--json")
        self.assertIn('"cp": "CP21"', out)

    def test_hubi_guard_runs_through_main(self):
        # execute() once had a local named guard that shadowed the command: UnboundLocalError live.
        import contextlib
        import io
        from hubi6 import cli
        import logging
        logger = logging.getLogger("hubi6")
        handlers = list(logger.handlers)
        self.addCleanup(lambda: setattr(logger, "handlers", handlers))   # main() adds a file handler
        self.sd.launch()
        out, err = io.StringIO(), io.StringIO()
        with patch("hubi6.guard.Guard", lambda app: self.g), patch("hubi6.services.App", lambda config: App(self.g.c, self.sd)), \
                patch("hubi6.cli.Config.load", lambda path=None: self.g.c), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.g.c.state_base.mkdir(mode=0o700, exist_ok=True)
            (self.g.c.state_base/"hubi6.log").touch()
            code = cli.main(["guard", "status"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("Strażnik: działa", out.getvalue())

    def test_parser_rejects_unknown_guard_commands(self):
        from hubi6 import cli
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            cli.parser().parse_args(["guard", "reset"])


class ConfigTests(unittest.TestCase):
    def config(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)/"config.toml"
        path.write_text(text)
        return Config.load(str(path))

    def test_guard_configuration(self):
        c = self.config("")
        self.assertEqual(c.guard_dir, c.home/".cache/praca/codex-watchdog")
        self.assertEqual(c.guard_unit, "praca-codex-watchdog")
        self.assertIsNone(self.config('[guard]\ndir = ""\n').guard_dir)
        self.assertEqual(self.config('[guard]\ndir = "/srv/g"\nunit = "inny"\n').guard_unit, "inny")
        for bad in ('[guard]\nunit = "x.service"\n', '[guard]\nunit = "a b"\n', '[guard]\ndir = "relative"\n',
                    '[guard]\nother = 1\n', 'guard = 3\n'):
            with self.assertRaises(HubiError, msg=bad):
                self.config(bad)


STAND_IN = r'''#!/usr/bin/env bash
# Stand-in for watchdog.sh: the same arguments, refusals and exit codes, no Codex.
CP=$1 CODEX=$2 CONTACT=$3
[[ $CP =~ ^CP[0-9]+[a-z]?$ && $CODEX == codex/* ]] || { echo "użycie: watchdog.sh <CP> <Codex> <Claude>" >&2; exit 2; }
[[ $CP == CP99 ]] && { echo "atrapa: zły próg" >&2; exit 2; }
key=${CODEX//\//-}
grep -q '^STATE=WEEKLY_STOP' "state.$key" 2>/dev/null && { echo "blokada tygodniowa w state.$key" >&2; exit 3; }
exec 9>"lock.$key"; flock -n 9 || { echo "strażnik dla $CODEX już działa" >&2; exit 1; }
echo "$(date -u +%FT%TZ) $CP $CODEX RUNNING: start" >> log
echo "$CP $CODEX $CONTACT low=${WD_LOW:-} max=${WD_MAX_HOURS:-} codex=$(command -v codex)" > seen
trap 'echo "$(date -u +%FT%TZ) $CP $CODEX RUNNING: koniec: sygnał" >> log; exit 0' TERM INT
while :; do sleep 1 & wait $!; done
'''


@unittest.skipUnless(LAB, "real systemd-run runs only in the lab UID")
class GuardLabTests(unittest.TestCase):
    def test_real_systemd_start_change_stop_and_refusals(self):
        from hubi6.services import App as RealApp
        from tests_v6.support import repo
        repo("praca")
        tag = uuid.uuid4().hex[:8]
        base = Path.home()/f"guard-lab-{tag}"
        d = base/"codex-watchdog"
        bin_dir = base/"bin"
        d.mkdir(parents=True)
        bin_dir.mkdir()
        (bin_dir/"codex").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir/"codex").chmod(0o755)
        (d/"watchdog.v49.sh").write_text(STAND_IN)
        # Like the real v4.8: the contact must be a Claude session.
        (d/"watchdog.v48.sh").write_text(STAND_IN.replace(
            '[[ $CP == CP99 ]]', '[[ $CONTACT == claude/* ]] || { echo "użycie: v4.8 chce claude/…" >&2; exit 2; }\n[[ $CP == CP99 ]]'))
        shutil.copy2(d/"watchdog.v49.sh", d/"watchdog.sh")
        (d/"limits.py").write_text("print('status=ok five_left=50 five_reset=1 week_left=40 week_reset=2 allowed=true')\n")
        unit = f"hubi6-lab-guard-{tag}"
        config = replace(Config.load(), guard_dir=d, guard_unit=unit)
        g = G.Guard(RealApp(config))
        saved = config.state_base/"guard.json"
        saved.unlink(missing_ok=True)
        path = f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"

        def seen():
            return (d/"seen").read_text().split()

        try:
            g.start(G.Setup("CP20", "codex/praca", "claude/praca", (("PATH", path), ("WD_LOW", "7"), ("WD_MAX_HOURS", "48")),
                            G.DEFAULT_RESTART))
            s = g.status(remember=True)
            self.assertTrue(s.running, s)
            self.assertEqual(s.setup.words(), "CP20 codex/praca claude/praca")
            self.assertEqual(dict(s.setup.env), {"PATH": path, "WD_LOW": "7", "WD_MAX_HOURS": "48"})
            self.assertEqual(dict(s.setup.restart)["RestartPreventExitStatus"], "1 2 3")
            self.assertEqual(s.versions, ["v4.9"])
            self.assertEqual(seen(), ["CP20", "codex/praca", "claude/praca", "low=7", "max=48", f"codex={bin_dir}/codex"])

            g.change_cp("CP21")
            self.assertEqual(seen()[:3], ["CP21", "codex/praca", "claude/praca"])
            self.assertEqual(seen()[3:5], ["low=7", "max=48"])
            self.assertIn("CP20 codex/praca RUNNING: koniec: sygnał", (d/"log").read_text())

            pid = g.unit_info()["MainPID"]
            state(d, "codex-praca", CP="CP21", STATE="PAUSED_5H", PAUSE_WINDOW="1792856773")
            with self.assertRaises(HubiError):
                g.change_cp("CP22")
            self.assertEqual(g.unit_info()["MainPID"], pid)        # untouched
            g.change_who("codex/praca", "claude/praca/inny")        # same CP and Codex: allowed in a pause
            self.assertEqual(seen()[:3], ["CP21", "codex/praca", "claude/praca/inny"])

            # The review's probe: v4.8 refuses a Codex contact. The guard must keep running on v4.9.
            g.change_who("codex/praca", "codex/praca/temp-rev")
            with self.assertRaises(HubiError) as e:
                g.switch_version("v4.8")
            self.assertIn("back to v4.9, the guard runs", str(e.exception))
            s = g.status()
            self.assertTrue(s.running)
            self.assertEqual(s.versions, ["v4.9"])
            self.assertEqual(seen()[:3], ["CP21", "codex/praca", "codex/praca/temp-rev"])
            # A new CP that dies at once brings the old one back too (in RUNNING: a pause refuses any new CP).
            state(d, "codex-praca", CP="CP21", STATE="RUNNING")
            with self.assertRaises(HubiError) as e:
                g.change_cp("CP99")
            self.assertIn("runs again with CP21", str(e.exception))
            self.assertTrue(g.status().running)
            self.assertEqual(seen()[0], "CP21")

            g.change_who("codex/praca", "claude/praca/inny")
            self.assertTrue(g.switch_version("v4.8"))
            self.assertEqual(g.status().versions, ["v4.8"])
            self.assertEqual(seen()[:3], ["CP21", "codex/praca", "claude/praca/inny"])

            self.assertTrue(g.stop())
            self.assertFalse(g.status().running)
            self.assertEqual(g.unit_info()["state"], "")            # --collect: no unit left behind
            g.start()                                               # the last setup
            self.assertEqual(seen()[:3], ["CP21", "codex/praca", "claude/praca/inny"])
            g.stop()

            state(d, "codex-praca", CP="CP21", STATE="WEEKLY_STOP")
            with self.assertRaises(HubiError):
                g.start()
            self.assertFalse(g.status().running)
            state(d, "codex-praca", CP="CP21", STATE="RUNNING")

            with self.assertRaises(HubiError) as e:
                g.start(G.Setup("CP99", "codex/praca", "claude/praca"))
            self.assertIn("right after start", str(e.exception))
            self.assertFalse(g.status().running)
            g.start()                                               # a failed unit never blocks the next start
            self.assertTrue(g.status().running)
        finally:
            subprocess.run(["systemctl", "--user", "stop", unit+".service"], check=False)
            subprocess.run(["systemctl", "--user", "reset-failed", unit+".service"], check=False,
                           stderr=subprocess.DEVNULL)
            saved.unlink(missing_ok=True)
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
