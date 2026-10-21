"""Polish dashboard: pure rendering and key handling anywhere; lifecycle in the lab."""
import ast
from dataclasses import replace
import io
import os
from pathlib import Path
import pty
import re
import signal
import sys
import termios
import time
import unittest
from unittest.mock import patch

from hubi6 import menu
from hubi6.adapters.tmux import NEW_PANE_HINT, label
from hubi6.config import Config
from hubi6.core.model import HubiError, Record, RepoHealth, RepoRef, Session, State, Unit, Workload, key_id
from hubi6.i18n import age, instance_of, name_pl, pl, plural
from hubi6.inventory import Snapshot
from hubi6.services import App
from hubi6.tui import terminal
from tests_v6.support import LAB, bootstrap
from tests_v6.test_runtime import Client, wait_for

ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(data):
    return ANSI.sub("", data.decode("utf-8", "replace") if isinstance(data, bytes) else data)


def ref(name, inode=1):
    return RepoRef(f"/srv/repos/{name}", 1, inode, f"/srv/repos/{name}/.git", 1, 100+inode, "", "")


def workload(kind, instance, name, state, *, created, attached=0, status=None, exit_signal="", health=RepoHealth.OK):
    r = Record("a"*20, os.getuid(), kind, instance, key_id(kind, ref(name).path, instance), "b"*16, ref(name),
               created=created, pane="%1", invocation="c"*32, phase="running")
    s = Session(r.session, "$1", "%1", state == State.EXITED, 1, attached, r.encoded())
    s.exit_status, s.exit_signal = status, exit_signal
    return Workload(r, state, health, s, None, [])


def snapshot(workloads, names=("alpha", "beta", "praca"), issues=()):
    return Snapshot(list(workloads), [ref(n, i) for i, n in enumerate(names)], list(issues), "OK", None, 1, "0", True, True, 1.0, 2)


def sample():
    now = time.time()
    items = [workload("claude", "rev", "praca", State.RUNNING, created=now-1560),
             workload("codex", "cp5", "praca", State.RUNNING, created=now-1980),
             workload("terminal", "primary", "alpha", State.ATTACHED, created=now-9000, attached=1),
             workload("claude", "smoke", "alpha", State.EXITED, created=now-60, status=1)]
    titles = {items[0].record.session: ("claude", "✳ Claude Code"),
              items[1].record.session: ("node", "⠼ Zaimplementuj CP5: timer i sloty | praca"),
              items[2].record.session: ("vim", "dev@box: ~/repos/alpha")}
    return items, titles


class KeyTests(unittest.TestCase):
    def reader(self, **kw):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(lambda: os.close(wr) if wr in self.open else None)
        self.open = {wr}
        return menu.Keys(rd, **kw), wr

    def test_single_keys_and_names(self):
        cases = {b"1": "1", b"q": "q", b" ": " ", b"\n": "enter", b"\r": "enter", b"\x1b": "esc", b"\x7f": "back",
                 b"\x1b[A": "up", b"\x1bOB": "down", b"\x1b[5~": "pgup", b"\x04": "eof", "ą".encode(): "ą"}
        for raw, expected in cases.items():
            self.assertEqual(menu.parse_key(raw), expected, raw)

    def test_unused_keys_are_ignored_not_bursts(self):
        for raw in (b"\x1b[H", b"\x1b[1;5C", b"\x1a", b"\x15", b"\x1b[I"):
            self.assertEqual(menu.parse_key(raw), "", raw)

    def test_several_keys_in_one_read_are_a_burst(self):
        for raw in (b"1q", b"d1q", b"w\n", b"\x1b[200~d", b"ab\xff", b"\x1b[A\x1b[A"):
            self.assertIsNone(menu.parse_key(raw), raw)

    def test_burst_is_discarded_whole(self):
        reader, wr = self.reader()
        os.write(wr, b"d1q")
        self.assertEqual(reader.key(1), "burst")
        self.assertIsNone(reader.key(.05))          # nothing from the burst survives
        os.write(wr, b"w")
        self.assertEqual(reader.key(1), "w")

    def test_separate_quick_keys_are_both_honoured(self):
        # The line menu dropped everything within 80 ms of Enter. Clean single
        # keys carry no such penalty: type-ahead in separate reads works.
        reader, wr = self.reader()
        os.write(wr, b"w")
        self.assertEqual(reader.key(1), "w")
        os.write(wr, b"b")
        started = time.monotonic()
        self.assertEqual(reader.key(1), "b")
        self.assertLess(time.monotonic()-started, .05)

    def test_bracketed_paste_is_swallowed_across_reads(self):
        reader, wr = self.reader()
        os.write(wr, b"\x1b[200~d\n1\n")
        os.write(wr, b"q\n\x1b[201~")
        self.assertEqual(reader.key(1), "burst")
        self.assertIsNone(reader.key(.05))

    def test_unterminated_paste_times_out(self):
        reader, wr = self.reader(paste_timeout=.02)
        os.write(wr, b"\x1b[200~q")
        self.assertEqual(reader.key(1), "burst")

    def test_settle_drops_queued_confirmation(self):
        reader, wr = self.reader()
        os.write(wr, b"q")
        reader.settle()
        self.assertIsNone(reader.key(.05))

    def test_eof_and_timeout(self):
        reader, wr = self.reader()
        self.assertIsNone(reader.key(.01))
        os.close(wr)
        self.open.clear()
        self.assertEqual(reader.key(1), "eof")


class RenderTests(unittest.TestCase):
    def frames(self, width, height, ui=None):
        items, titles = sample()
        view = menu.build_view(snapshot(items), titles)
        with patch.object(menu, "branch_of", return_value="fix/a-rather-long-branch-name"):
            view = menu.build_view(snapshot(items), titles)
        g = menu.UNICODE
        catalogue = type("C", (), {"stale": True, "error": ""})()
        repos = [{"nameWithOwner": f"owner/repo-with-a-long-name-{n}", "isPrivate": n % 2, "isFork": n % 3 == 0,
                  "isArchived": False, "local": n % 2 == 0, "local_paths": []} for n in range(14)]
        return {
            "main": menu.frame_main(view, menu.Ui(autologin=True), width, height, g),
            "main-stop": menu.frame_main(view, menu.Ui(verb="stop"), width, height, g),
            "pick": menu.frame_pick(view, menu.Ui(screen="pick", kind="claude", resume=True), width, height, g),
            "repo": menu.frame_repo(view, menu.Ui(screen="repo", repo=ref("alpha"), git="## main\n M a\n?? b\n"*9), width, height, g),
            "detail": menu.frame_detail(view, menu.Ui(screen="detail", selected=items[1].record.id), width, height, g),
            "github": menu.frame_github(view, menu.Ui(screen="github"), width, height, g, catalogue, repos),
            "help": menu.frame_help(view, menu.Ui(screen="help"), width, height, g),
        }

    def test_no_frame_wraps_or_overflows_at_any_size(self):
        for width in (24, 39, 48, 64, 79, 120, 200):
            for height in (8, 12, 18, 25, 50):
                for name, lines in self.frames(width, height).items():
                    rendered = menu.render(lines, menu.Theme(False), width)
                    self.assertLessEqual(len(rendered), height, (name, width, height))
                    for line in rendered:
                        self.assertLessEqual(menu.width_of(line), width, (name, width, height, line))

    def test_phone_sized_main_screen_content(self):
        text = "\n".join(menu.render(self.frames(64, 25)["main"], menu.Theme(False), 64))
        for expected in ("claude/rev", "codex/cp5", "terminal/1", "Zaimplementuj CP5: timer i sloty",
                         "pracuje", "czeka", "błąd (kod 1)", "AGENCI I TERMINALE", "REPOZYTORIA", "x rozłącz"):
            self.assertIn(expected, text)
        for english in ("RUNNING", "ATTACHED", "EXITED", "Choice", "workloads", "back", "primary"):
            self.assertNotIn(english, text)
        self.assertNotIn("Claude Code", text)        # a generic agent title is noise, not information

    def test_default_instance_is_shown_as_a_polish_label_everywhere(self):
        frames = self.frames(64, 25)
        for name in ("main", "pick", "repo", "detail"):
            text = "\n".join(menu.render(frames[name], menu.Theme(False), 64))
            self.assertNotIn("primary", text, name)
            self.assertNotIn("instanc", text.lower(), name)
        for name in ("pick", "repo"):
            text = "\n".join(menu.render(frames[name], menu.Theme(False), 64))
            self.assertIn("n nazwa sesji", text, name)
            self.assertNotIn("etykiet", text.lower(), name)
            self.assertNotIn("nazwa sesji:", text, name)       # nothing typed: the name is automatic, so no line
        words = " ".join(text for _keys, text in menu.HELP).lower()
        self.assertNotIn("instanc", words)
        self.assertNotIn("etykiet", words)

    def test_conversation_switch_is_loud_and_says_what_will_happen(self):
        items, titles = sample()
        view = menu.build_view(snapshot(items), titles)
        draw = lambda ui, frame=menu.frame_pick: "\n".join(menu.render(frame(view, ui, 64, 25, menu.UNICODE), menu.Theme(False), 64))
        new = draw(menu.Ui(screen="pick", kind="claude"))
        old = draw(menu.Ui(screen="pick", kind="claude", resume=True))
        self.assertIn("NOWY ✳ CLAUDE", new)
        self.assertIn("[ NOWA ROZMOWA ]", new)
        self.assertIn("wznów rozmowę", new)
        self.assertIn("r przełącza", new)
        self.assertIn("WZNÓW ✳ CLAUDE", old)
        self.assertIn("[ WZNÓW ROZMOWĘ ]", old)
        self.assertIn("nowa rozmowa", old)
        self.assertIn("wróci rozmowa sesji", old)
        self.assertNotIn("wróci rozmowa sesji", new)
        # The active side is drawn in its own style, not only in brackets.
        frame = menu.frame_pick(view, menu.Ui(screen="pick", kind="codex", resume=True), 64, 25, menu.UNICODE)
        self.assertIn(("[ WZNÓW ROZMOWĘ ]", "on"), frame[1])
        # A terminal has no conversation; a typed name is always shown.
        shell = draw(menu.Ui(screen="pick", kind="terminal", instance="rev"))
        self.assertNotIn("ROZMOW", shell)
        self.assertIn("nazwa sesji: rev", shell)
        repo = draw(menu.Ui(screen="repo", repo=ref("alpha"), resume=True), menu.frame_repo)
        self.assertIn("[ WZNÓW ROZMOWĘ ]", repo)

    def test_repository_row_says_how_many_sessions_did_not_fit(self):
        now = time.time()
        items = [workload(kind, name, "praca", State.RUNNING, created=now+n)
                 for n, (kind, name) in enumerate((("codex", "primary"), ("claude", "rev"), ("terminal", "primary"), ("terminal", "t2")))]
        with patch.object(menu, "branch_of", return_value="cp5-timer-trip"):
            row = next(r for r in menu.build_view(snapshot(items), {}).repos if r.name == "praca")
        draw = lambda width: "".join(menu.render([menu.repo_row("", row, width, menu.UNICODE)], menu.Theme(False), width))
        self.assertTrue(draw(65).rstrip().endswith(">_ 1  ✳ rev  $ 1  +1"), draw(65))
        self.assertTrue(draw(40).rstrip().endswith("+4"), draw(40))
        self.assertNotIn("+", draw(100))
        self.assertIn("$ t2", draw(100))
        # At every width the shown sessions plus the counter account for all four.
        for width in range(30, 101):
            text = draw(width)
            shown = sum(text.count(mark) for mark in (">_ 1", "✳ rev", "$ 1", "$ t2"))
            hidden = int(text.rstrip().rsplit("+", 1)[1]) if text.rstrip().split()[-1].startswith("+") else 0
            self.assertEqual(shown+hidden, 4, (width, text))

    def test_footer_survives_a_short_terminal(self):
        text = "\n".join(menu.render(self.frames(64, 8)["main"], menu.Theme(False), 64))
        self.assertIn("1-9 wejdź", text)

    def test_numbering_follows_creation_order(self):
        items, titles = sample()
        before = [i.id for i in menu.build_view(snapshot(items), titles).items]
        newer = workload("terminal", "extra", "alpha", State.RUNNING, created=time.time())
        after = [i.id for i in menu.build_view(snapshot(items+[newer]), titles).items]
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after[-1], newer.record.id)

    def test_colour_can_be_disabled(self):
        lines = self.frames(64, 25)["main"]
        self.assertNotIn("\x1b", "".join(menu.render(lines, menu.Theme(False), 64)))
        self.assertIn("\x1b[", "".join(menu.render(lines, menu.Theme(True), 64)))

    def test_hostile_text_cannot_inject_terminal_controls(self):
        evil = workload("codex", "x", "praca", State.RUNNING, created=time.time())
        titles = {evil.record.session: ("node", "⠼ \x1b[2J\x1b]0;pwn\x07 ‮ reversed")}
        view = menu.build_view(snapshot([evil]), titles)
        text = "".join(menu.render(menu.frame_main(view, menu.Ui(), 64, 25, menu.UNICODE), menu.Theme(False), 64))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("‮", text)

    def test_describe(self):
        self.assertEqual(menu.describe("codex", "node", "⠼ Fix tests | praca", "praca"), "Fix tests")
        self.assertEqual(menu.describe("claude", "claude", "✳ Claude Code", "praca"), "")
        self.assertEqual(menu.describe("claude", "claude", "✳ Hubi v6 PR", "praca"), "Hubi v6 PR")
        self.assertEqual(menu.describe("terminal", "bash", "dev@box: ~", "praca"), "")
        self.assertEqual(menu.describe("terminal", "vim", "dev@box: ~", "praca"), "vim")
        # Codex before it has a topic: the title is only spinner frames.
        self.assertEqual(menu.describe("codex", "node", "⠼ ⠼ | praca", "praca"), "")
        self.assertEqual(menu.describe("claude", "claude", "◐ Test odpowiedzi", "praca"), "Test odpowiedzi")

    def test_working_is_read_from_real_agent_titles(self):
        # Recorded on 2026-10-14 from Claude Code 2.1.296 and Codex 0.162.1.
        for title in ("◐ Test odpowiedzi jednym słowem", "⠼ ⠼ | praca", "⠸ Potwierdź | praca", "⠏"):
            self.assertEqual(menu.working(title), title[0], title)
        for title in ("✳ Test odpowiedzi jednym słowem", "✳ Claude Code", "Potwierdź | praca", "praca", "claude · resume",
                      "", "⠸bez spacji", "- lista", "* gwiazdka", "\u2800 pusty brajl"):
            self.assertEqual(menu.working(title), "", title)
        now = time.time()
        claude = workload("claude", "primary", "praca", State.RUNNING, created=now)
        shell = workload("terminal", "primary", "praca", State.RUNNING, created=now+1)
        dead = workload("codex", "primary", "praca", State.EXITED, created=now+2, status=0)
        titles = {w.record.session: ("x", "◐ Praca") for w in (claude, shell, dead)}
        view = menu.build_view(snapshot([claude, shell, dead]), titles)
        self.assertEqual([(i.kind, i.busy) for i in view.items], [("claude", True), ("terminal", False), ("codex", False)])
        # The title only says busy; the glyph comes from the menu's own clock.
        self.assertEqual(menu.state_cell(view.items[0], menu.UNICODE), ("pracuje ⠋", "busy"))

    def test_spinner_glyph_follows_the_menu_clock(self):
        now = time.time()
        busy = workload("claude", "primary", "praca", State.RUNNING, created=now)
        idle = workload("codex", "primary", "praca", State.RUNNING, created=now+1)
        view = menu.build_view(snapshot([busy, idle]), {busy.record.session: ("claude", "◐ Praca")})
        for glyphs in (menu.UNICODE, menu.ASCII):
            seen = [menu.state_cell(view.items[0], glyphs, frame)[0] for frame in range(2*len(glyphs.spinner))]
            self.assertEqual(seen[:len(glyphs.spinner)], ["pracuje "+g for g in glyphs.spinner])
            self.assertEqual(seen[len(glyphs.spinner):], seen[:len(glyphs.spinner)])     # it wraps around
            self.assertEqual({menu.state_cell(view.items[1], glyphs, f) for f in range(5)}, {("czeka", "idle")})
        draw = lambda frame: menu.render(menu.frame_main(replace(view, frame=frame), menu.Ui(), 64, 25, menu.UNICODE),
                                         menu.Theme(False), 64)
        changed = [n for n, (a, b) in enumerate(zip(draw(0), draw(1))) if a != b]
        self.assertEqual(len(changed), 1)                                  # only the working agent's row moves
        self.assertIn("pracuje", draw(1)[changed[0]])
        detail = lambda frame: menu.render(menu.frame_detail(replace(view, frame=frame), menu.Ui(screen="detail", selected=view.items[0].id),
                                                             64, 25, menu.UNICODE), menu.Theme(False), 64)
        self.assertNotEqual(detail(0), detail(1))

    def test_message_wraps_instead_of_being_cut(self):
        ui = menu.Ui(message="słowo "*30, level="bad")
        lines = menu.render(menu.message_lines(ui, 40), menu.Theme(False), 40)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(menu.width_of(line) <= 40 for line in lines))

    def test_screen_sends_only_changed_lines(self):
        class Stream(io.StringIO):
            def fileno(self):
                raise OSError()
        stream = Stream()
        screen = menu.Screen(stream)
        screen.paint(["one", "two", "three"])
        first = stream.getvalue()
        self.assertIn("\x1b[2J", first)
        stream.seek(0)
        stream.truncate()
        screen.paint(["one", "two", "three"])
        self.assertEqual(stream.getvalue(), "")
        screen.paint(["one", "TWO", "three"])
        self.assertIn("TWO", stream.getvalue())
        self.assertNotIn("one", stream.getvalue())
        self.assertNotIn("three", stream.getvalue())
        stream.seek(0)
        stream.truncate()
        screen.paint(["one"])
        self.assertIn("\x1b[2;1H\x1b[J", stream.getvalue())


class TextTests(unittest.TestCase):
    def messages(self):
        """Every literal HubiError message and appended diagnostic in the package."""
        found = []
        for path in sorted((ROOT/"hubi6").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                first = node.args[0]
                is_error = getattr(node.func, "id", "") in ("HubiError", "Unavailable")
                is_issue = getattr(node.func, "attr", "") == "append" and path.name in ("inventory.py", "git.py", "storage.py") \
                    or (getattr(node.func, "attr", "") == "append" and path.name == "tmux.py" and isinstance(first, ast.Constant)
                        and isinstance(first.value, str) and " " in first.value)
                if not (is_error or is_issue):
                    continue
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((path.name, node.lineno, first.value))
                elif isinstance(first, ast.JoinedStr):
                    text = "".join(part.value if isinstance(part, ast.Constant) else "X1" for part in first.values)
                    found.append((path.name, node.lineno, text))
                elif isinstance(first, ast.BinOp) and isinstance(first.left, ast.Constant):
                    found.append((path.name, node.lineno, first.left.value+"X1"))
        return found

    def test_every_error_has_polish_wording(self):
        messages = self.messages()
        self.assertGreater(len(messages), 100)
        missing = [m for m in messages if pl(m[2]) == m[2]]
        self.assertFalse(missing, missing)

    def test_template_fields_are_preserved(self):
        self.assertEqual(pl("Agent executable unavailable: codex"), "Brak programu agenta: codex")
        self.assertIn("0123abcd", pl("Workload EXITED: 0123abcd; stop/reconcile it before creating a new generation"))
        self.assertEqual(pl("Invalid repository x: broken symlink"), "Nieprawidłowe repozytorium x: zerwany symlink")
        self.assertEqual(pl("something nobody translated"), "something nobody translated")

    def test_age_and_plural(self):
        self.assertEqual([age(5), age(125), age(7500), age(200000)], ["5 s", "2 min", "2 h 05", "2 d 7 h"])
        self.assertEqual([plural(n, "a", "b", "c") for n in (1, 2, 5, 12, 22)], ["a", "b", "c", "c", "b"])

    def test_status_label_is_inert(self):
        r = workload("claude", "rev", "pra#[fg=red]ca", State.RUNNING, created=1).record
        self.assertNotIn("#", label(r))
        self.assertIn("claude/rev", label(r))
        self.assertLessEqual(len(label(r)), 60)
        self.assertTrue(NEW_PANE_HINT)
        self.assertIn("codex/1 · praca", label(workload("codex", "primary", "praca", State.RUNNING, created=1).record))

    def test_default_label_maps_both_ways_and_never_changes_identity(self):
        self.assertEqual([name_pl("primary"), name_pl("rev"), name_pl("Primary")], ["1", "rev", "Primary"])
        self.assertEqual([instance_of(x) for x in ("", "  ", "1", " 1 ", "główna", "GŁÓWNA", " rev ", "primary", "glowna", "2")],
                         ["primary", "primary", "primary", "primary", "primary", "primary", "rev", "primary", "glowna", "2"])
        # The stored record and its key keep the ASCII name; only what is drawn changes.
        r = workload("claude", "primary", "praca", State.RUNNING, created=1).record
        self.assertEqual((r.instance, r.key), ("primary", key_id("claude", ref("praca").path, "primary")))
        self.assertEqual(r.data()["instance"], "primary")

    def test_ui_configuration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"c.toml"
            path.write_text("[ui]\nrefresh = 5\ncolor = false\nicons = false\n")
            c = Config.load(str(path))
            self.assertEqual((c.ui_refresh, c.ui_color, c.ui_icons), (5, False, False))
            # Each Codex session must own its processes: no shared background server.
            self.assertEqual(c.agents["codex"], ["codex", "--no-daemon"])
            path.write_text('[agents]\ncodex = ["codex"]\n')
            self.assertEqual(Config.load(str(path)).agents["codex"], ["codex"])
            for bad in ("[ui]\nrefresh = 0\n", "[ui]\ncolor = 1\n", "[ui]\nfancy = true\n"):
                path.write_text(bad)
                with self.assertRaises(HubiError):
                    Config.load(str(path))


class LifecycleUnitTests(unittest.TestCase):
    def app(self, found):
        app = App(Config.load())
        app.stopped = []
        app.find = lambda identity, snapshot=None, health_check=True: found
        app.stop = lambda identity, expected=None, repair=False: app.stopped.append((identity, expected))
        return app

    def test_reap_rules_per_type_status_and_signal(self):
        now = time.time()
        exited = lambda kind, **kw: workload(kind, "a", "alpha", State.EXITED, created=now, **kw)
        # A terminal's `exit` carries the last command's status: any status is the user's own way out.
        for gone in (exited("terminal", status=0), exited("terminal", status=1), exited("terminal", status=127),
                     exited("codex", status=0), exited("claude", status=0)):
            app = self.app(gone)
            self.assertIs(app.reap_clean_exit(gone.record.id), gone, (gone.record.kind, gone.session.exit_status))
            self.assertEqual(app.stopped, [(gone.record.id, gone.record.generation)])
        two_panes = exited("terminal", status=1)
        two_panes.session.panes = 2
        live_scope = exited("terminal", status=1)
        live_scope.unit = Unit(live_scope.record.scope, "active")
        for kept in (exited("codex", status=1), exited("claude", status=3),          # failed agents stay (contract 19)
                     exited("terminal", status=None, exit_signal="9"),               # killed: not the user's exit
                     exited("terminal", status=0, exit_signal="15"), exited("codex", status=None, exit_signal="9"),
                     exited("terminal", status=None),                                # status unknown
                     exited("terminal", status=1, attached=1), two_panes, live_scope,
                     workload("terminal", "a", "alpha", State.RUNNING, created=now)):
            app = self.app(kept)
            self.assertIsNone(app.reap_clean_exit(kept.record.id), (kept.record.kind, kept.session.exit_status, kept.session.exit_signal))
            self.assertEqual(app.stopped, [])

    def test_tidy_message_says_a_non_zero_code(self):
        now = time.time()
        term = workload("terminal", "primary", "alpha", State.EXITED, created=now, status=1)
        clean = workload("terminal", "2", "alpha", State.EXITED, created=now, status=0)
        self.assertEqual(menu.tidied([term]), "terminal/1: koniec pracy (kod 1), posprzątane")
        self.assertEqual(menu.tidied([clean]), "terminal/2: koniec pracy, posprzątane")
        self.assertEqual(menu.tidied([term, clean]), "terminal/1 (kod 1), terminal/2: koniec pracy, posprzątane")

    def test_menu_terminal_mode_cannot_be_suspended(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before, handler = termios.tcgetattr(slave), signal.getsignal(signal.SIGTSTP)
        fake = type("Tty", (), {"fileno": lambda self: slave})()
        with patch("hubi6.tui.sys.stdin", fake), patch("hubi6.tui.sys.stdout", io.StringIO()):
            with terminal(paste=True):
                self.assertEqual(termios.tcgetattr(slave)[6][termios.VSUSP], b"\x00")
                self.assertTrue(callable(signal.getsignal(signal.SIGTSTP)))
            self.assertEqual(termios.tcgetattr(slave), before)
            self.assertEqual(signal.getsignal(signal.SIGTSTP), handler)
            with terminal():                         # plain CLI commands keep normal job control
                self.assertEqual(termios.tcgetattr(slave)[6][termios.VSUSP], before[6][termios.VSUSP])


class FakeTmux:
    def __init__(self, titles):
        self.table = titles

    def titles(self):
        return self.table


class FakeGit:
    def __init__(self):
        self.summaries = 0

    def summary(self, reference):
        self.summaries += 1
        return "## main\n M file\n"

    def resolve(self, value):
        return ref(Path(value).name)

    def home(self):
        raise HubiError("Home workspace is disabled in the configuration")


class FakeApp:
    """Records lifecycle calls; nothing here touches tmux, systemd or a PTY."""

    def __init__(self, workloads=(), titles=None):
        self.c = replace(Config.load(), ui_color=False)
        self.workloads, self.calls, self.r = list(workloads), [], None
        self.tmux, self.git = FakeTmux(titles or {}), FakeGit()
        self.fail_start = None
        self.saved = {}                               # (kind, path) -> names with a saved conversation, newest first

    def resumable(self, kind, path):
        return list(self.saved.get((kind, path), []))

    def snapshot(self):
        return snapshot(self.workloads)

    def start(self, kind, path, instance="primary", resume=False):
        self.calls.append(("start", kind, path, instance, resume))
        if self.fail_start:
            raise HubiError(self.fail_start)
        w = workload(kind, instance, Path(path).name, State.RUNNING, created=time.time())
        self.workloads.append(w)
        return w.record

    def attach(self, identity, mode="normal"):
        self.calls.append(("attach", identity, mode))
        return 0

    def stop(self, identity, expected=None, repair=False):
        self.calls.append(("stop", identity, expected, repair))
        self.workloads = [w for w in self.workloads if w.record.id != identity]

    def find(self, identity, snapshot=None, health_check=True):
        for w in self.workloads:
            if w.record.id == identity:
                return w
        raise HubiError("Workload ID missing or ambiguous; use hubi list")

    def reap_clean_exit(self, identity):
        return None


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TickReader:
    """Times out after the requested wait on a fake clock; presses q at the end."""

    def __init__(self, clock, until, keys=()):
        self.clock, self.until, self.keys, self.waits = clock, until, list(keys), []

    def key(self, timeout=None):
        self.waits.append(timeout)
        if self.keys and self.clock.now >= self.keys[0][0]:
            return self.keys.pop(0)[1]
        if self.clock.now >= self.until:
            return "q"
        self.clock.now += timeout
        return None


class LoopTests(unittest.TestCase):
    """Menu.loop timing: spinner ticks repaint, snapshots stay every ui_refresh."""

    def run_loop(self, workloads, titles, *, seconds=6, idle=False, screen="main", keys=()):
        class Stream(io.StringIO):
            def fileno(self):
                raise OSError()
        app = FakeApp(workloads, titles)
        app.c = replace(app.c, ui_refresh=2)
        calls = []
        snap = app.snapshot
        app.snapshot = lambda: calls.append(clock.now) or snap()
        clock = FakeClock()
        reader = TickReader(clock, clock.now+seconds, [(clock.now+t, k) for t, k in keys])
        m = menu.Menu(app, reader, menu.Screen(Stream()))
        m.clock = clock
        m.touched = clock.now-(120 if idle else 0)
        m.ui.screen = screen
        frames = []
        draw = m.draw
        m.draw = lambda: frames.append(m.view.frame) or draw()
        self.assertEqual(m.loop(), 98)
        return calls, reader.waits, frames, m

    def busy(self):
        claude = workload("claude", "primary", "praca", State.RUNNING, created=1)
        return [claude], {claude.record.session: ("claude", "◐ Praca")}

    def test_working_agent_animates_without_extra_snapshots(self):
        calls, waits, frames, m = self.run_loop(*self.busy())
        self.assertTrue(all(w <= menu.TICK+1e-9 for w in waits[:-1]), waits)
        self.assertGreater(len(set(frames)), 30)                         # ~8 frames a second for 6 s
        gaps = [b-a for a, b in zip(calls, calls[1:])]
        self.assertTrue(all(g >= 2-1e-6 for g in gaps), gaps)               # snapshots still every ui_refresh
        self.assertLessEqual(len(calls), 4)

    def test_without_work_the_loop_sleeps_as_before(self):
        idle = workload("codex", "primary", "praca", State.RUNNING, created=1)
        calls, waits, frames, m = self.run_loop([idle], {})
        self.assertEqual(set(waits[:-1]), {2})
        self.assertEqual(len(calls), len(waits))
        calls, waits, frames, m = self.run_loop([idle], {}, seconds=40, idle=True)
        self.assertEqual(set(waits[:-1]), {10})                             # quiet backoff after a minute

    def test_no_quiet_backoff_while_an_agent_works(self):
        calls, waits, frames, m = self.run_loop(*self.busy(), seconds=12, idle=True, screen="help")
        self.assertEqual(set(waits[:-1]), {2})                              # not visible: no ticks, no 10 s backoff
        gaps = [b-a for a, b in zip(calls, calls[1:])]
        self.assertTrue(all(abs(g-2) < 1e-6 for g in gaps), gaps)

    def test_a_key_still_brings_a_fresh_snapshot(self):
        calls, waits, frames, m = self.run_loop(*self.busy(), seconds=3, keys=[(.5, "?")])
        self.assertTrue(any(abs(c-(1000.5+menu.TICK*0)) < menu.TICK+1e-6 for c in calls), calls)
        self.assertEqual(m.ui.screen, "help")


class ControllerTests(unittest.TestCase):
    def make(self, workloads=(), titles=None, **env):
        rd, wr = os.pipe()
        self.addCleanup(os.close, rd)
        self.addCleanup(os.close, wr)
        self.wr = wr

        class Stream(io.StringIO):
            def fileno(self):
                raise OSError()
        app = FakeApp(workloads, titles)
        with patch.dict(os.environ, env):
            m = menu.Menu(app, menu.Keys(rd), menu.Screen(Stream()))
        m.refresh()
        return m, app

    def later(self, data, delay=.2):
        import threading
        timer = threading.Timer(delay, os.write, (self.wr, data))
        timer.start()
        self.addCleanup(timer.join)

    def press(self, m, *keys):
        result = None
        for key in keys:
            with patch("hubi6.tui.sys.stdout", io.StringIO()):
                result = m.handle(key)
            m.refresh()
            m.draw()
        return result

    def text(self, m):
        return plain(m.screen.stream.getvalue())

    def test_digit_attaches_at_once(self):
        items, titles = sample()
        m, app = self.make(items[:2], titles)
        first = menu.build_view(app.snapshot(), titles).items[0]
        self.assertIsNone(self.press(m, "1"))
        self.assertEqual(app.calls, [("attach", first.id, "normal")])

    def test_attached_session_asks_how_to_join(self):
        busy = workload("terminal", "primary", "alpha", State.ATTACHED, created=1, attached=2)
        m, app = self.make([busy])
        self.later(b"s")
        self.press(m, "1")
        self.assertEqual(app.calls, [("attach", busy.record.id, "shared")])
        self.assertIn("ma już 2 klientów", self.text(m))
        m, app = self.make([busy])
        self.later(b"z")
        self.press(m, "1")
        self.assertEqual(app.calls, [])

    def test_stop_requires_q_and_passes_the_generation(self):
        items, titles = sample()
        m, app = self.make(items[:2], titles)
        target = menu.build_view(app.snapshot(), titles).items[0]
        self.later(b"n")
        self.press(m, "d", "1")
        self.assertEqual(app.calls, [])
        self.assertIn("Anulowano", self.text(m))
        self.later(b"q")
        self.press(m, "d", "1")
        self.assertEqual(app.calls, [("stop", target.id, target.generation, False)])
        self.assertIn("zatrzymano", self.text(m))

    def test_any_other_key_cancels_a_pending_stop(self):
        items, titles = sample()
        m, app = self.make(items[:2], titles)
        self.press(m, "d", "g")
        self.assertEqual((m.ui.verb, m.ui.screen, app.calls), ("", "main", []))

    def test_new_agent_is_two_keys_and_attaches(self):
        m, app = self.make()
        self.press(m, "L", "3")                      # keys are not case sensitive
        started = app.workloads[-1].record
        self.assertEqual(app.calls, [("start", "claude", ref("praca").path, "primary", False), ("attach", started.id, "normal")])
        self.assertEqual(m.ui.screen, "main")

    def test_named_instance_and_resume(self):
        m, app = self.make()
        os.write(self.wr, b"rev\n")
        self.press(m, "c", "r", "n", "1")
        self.assertEqual(app.calls[0], ("start", "codex", ref("alpha").path, "rev", True))
        self.assertEqual((m.ui.instance, m.ui.resume), ("", False))   # options do not leak into the next start

    def test_invalid_instance_name_is_refused_in_polish(self):
        m, app = self.make()
        os.write(self.wr, "zła nazwa\n".encode())
        self.press(m, "l", "n")
        self.assertEqual(m.ui.instance, "")
        self.assertIn("Nazwa sesji", m.ui.message)
        self.assertNotIn("instanc", m.ui.message.lower())
        self.assertEqual(app.calls, [])

    def test_name_prompt_and_the_default_name(self):
        for typed in ("\n", "1\n", "główna\n", "Główna \n"):
            m, app = self.make()
            os.write(self.wr, typed.encode())
            self.press(m, "t", "n", "1")
            self.assertEqual(app.calls[0], ("start", "terminal", ref("alpha").path, "primary", False), typed)

    def test_resume_toggle_reports_itself(self):
        m, app = self.make()
        self.press(m, "c", "r")
        self.assertTrue(m.ui.resume)
        self.assertIn("wróci rozmowa tej sesji", m.ui.message)
        self.press(m, "r")
        self.assertFalse(m.ui.resume)
        self.assertEqual(m.ui.message, "Nowa rozmowa")

    def test_terminal_has_no_resume(self):
        m, app = self.make()
        self.press(m, "t", "r")
        self.assertFalse(m.ui.resume)
        self.assertIn("nic tu nie robi", m.ui.message)

    def test_new_session_next_to_a_running_one_gets_a_number(self):
        running = workload("claude", "primary", "alpha", State.RUNNING, created=1)
        m, app = self.make([running])
        self.press(m, "l", "1")
        started = app.workloads[-1].record
        self.assertEqual(app.calls, [("start", "claude", ref("alpha").path, "2", False), ("attach", started.id, "normal")])
        self.press(m, "l", "1")
        self.assertEqual(app.calls[2], ("start", "claude", ref("alpha").path, "3", False))
        # Another type or another repository still gets the default name.
        self.press(m, "c", "1")
        self.assertEqual(app.calls[4][:4], ("start", "codex", ref("alpha").path, "primary"))
        self.press(m, "l", "2")
        self.assertEqual(app.calls[6][:4], ("start", "claude", ref("beta").path, "primary"))

    def test_resume_resumes_even_where_the_agent_already_runs(self):
        # Reported by the owner: with Codex running in the repository, "wznów" only entered the running session.
        taken = [workload("codex", name, "praca", State.RUNNING, created=n) for n, name in enumerate(("primary", "test", "wznow"), 1)]
        m, app = self.make(taken)
        self.press(m, "c", "r", "3")
        started = app.workloads[-1].record
        self.assertEqual(app.calls, [("start", "codex", ref("praca").path, "2", True), ("attach", started.id, "normal")])
        self.assertEqual((m.ui.instance, m.ui.resume), ("", False))
        # The same from the repository view.
        m, app = self.make(taken)
        m.open_repo(ref("praca"))
        self.press(m, "r", "c")
        self.assertEqual(app.calls[0], ("start", "codex", ref("praca").path, "2", True))

    def test_claude_resume_returns_the_stopped_session_with_a_saved_conversation(self):
        # Reported by the owner: resumed conversations landed under other names (the reviewer in "główna").
        m, app = self.make()
        app.saved[("claude", ref("alpha").path)] = ["rev"]
        self.press(m, "l", "r", "1")
        self.assertEqual(app.calls[0], ("start", "claude", ref("alpha").path, "rev", True))
        self.assertEqual((m.ui.instance, m.ui.resume), ("", False))

    def test_claude_resume_asks_which_name_when_several_are_saved(self):
        for typed, expected in ((b"1\n", "primary"), (b"rev\n", "rev"), (b"\n", "rev")):
            m, app = self.make()
            app.saved[("claude", ref("alpha").path)] = ["rev", "primary"]
            os.write(self.wr, typed)
            self.press(m, "l", "r", "1")
            self.assertEqual(app.calls[0], ("start", "claude", ref("alpha").path, expected, True), typed)

    def test_claude_resume_refuses_an_unknown_name_and_skips_running_ones(self):
        m, app = self.make()
        app.saved[("claude", ref("alpha").path)] = ["rev", "primary"]
        os.write(self.wr, b"inna\n")
        self.press(m, "l", "r", "1")
        self.assertEqual(app.calls, [])
        self.assertIn("Nie ma zatrzymanej sesji claude/inna", m.ui.message)
        # The saved session that already runs is not offered again; nothing saved → free name and Claude's list.
        running = workload("claude", "primary", "alpha", State.RUNNING, created=1)
        m, app = self.make([running])
        app.saved[("claude", ref("alpha").path)] = ["primary"]
        self.press(m, "l", "r", "1")
        self.assertEqual(app.calls[0], ("start", "claude", ref("alpha").path, "2", True))

    def test_automatic_name_never_reuses_or_replaces_another_session(self):
        dead = workload("claude", "primary", "alpha", State.EXITED, created=1, status=1)
        gap = workload("claude", "3", "alpha", State.RUNNING, created=2)
        m, app = self.make([dead, gap])
        self.press(m, "l", "1")                      # no question and nothing stopped
        self.assertEqual(app.calls[0][:4], ("start", "claude", ref("alpha").path, "2"))
        self.press(m, "l", "1")
        self.assertEqual(app.calls[2][:4], ("start", "claude", ref("alpha").path, "4"))
        self.assertNotIn("stop", [call[0] for call in app.calls])
        self.assertEqual(menu.free_name([], "claude", ref("alpha").path), "primary")

    def test_typed_name_of_a_running_session_enters_it(self):
        running = workload("claude", "rev", "alpha", State.RUNNING, created=1)
        m, app = self.make([running])
        os.write(self.wr, b"rev\n")
        self.press(m, "l", "r", "n", "1")
        self.assertEqual(app.calls, [("attach", running.record.id, "normal")])
        self.assertEqual((m.ui.instance, m.ui.resume), ("", False))

    def test_typed_name_of_a_finished_session_replaces_it_only_after_q(self):
        dead = workload("claude", "rev", "alpha", State.EXITED, created=1, status=1)
        m, app = self.make([dead])
        os.write(self.wr, b"rev\n")
        self.press(m, "l", "n")
        self.later(b"n")
        self.press(m, "1")
        self.assertEqual(app.calls, [])
        os.write(self.wr, b"rev\n")
        self.press(m, "l", "n")
        self.later(b"q")
        self.press(m, "1")
        self.assertEqual([call[0] for call in app.calls], ["stop", "start", "attach"])
        self.assertEqual(app.calls[1][:4], ("start", "claude", ref("alpha").path, "rev"))

    def test_failed_start_is_reported_in_polish(self):
        m, app = self.make()
        app.fail_start = "Agent executable unavailable: codex"
        self.press(m, "c", "1")
        self.assertEqual(m.ui.message, "Brak programu agenta: codex")
        self.assertEqual(m.ui.level, "bad")

    def test_exit_keys(self):
        m, _ = self.make()
        self.assertEqual(self.press(m, "q"), 98)
        self.assertEqual(self.press(m, "eof"), 0)
        self.assertIsNone(self.press(m, "x"))
        self.assertIn("nic tu nie robi", m.ui.message)
        m, _ = self.make(HUBI_AUTOLOGIN="1")
        self.assertEqual(self.press(m, "x"), 99)

    def test_burst_and_unknown_keys_are_explained(self):
        m, app = self.make()
        self.press(m, "burst")
        self.assertIn("Zignorowano serię znaków", m.ui.message)
        self.press(m, "z")
        self.assertIn("Klawisz „z” nic tu nie robi", m.ui.message)
        self.press(m, "9")
        self.assertIn("Nie ma sesji numer 9", m.ui.message)
        self.assertEqual(app.calls, [])

    def test_back_from_every_screen(self):
        m, _ = self.make()
        for opener in ("l", "r", "?"):
            for back in ("0", "esc", "back"):
                self.press(m, opener)
                self.assertNotEqual(m.ui.screen, "main", opener)
                self.press(m, back)
                self.assertEqual(m.ui.screen, "main", (opener, back))

    def test_repository_view_and_details(self):
        items, titles = sample()
        m, app = self.make(items, titles)
        self.press(m, "r", "3")
        self.assertEqual((m.ui.screen, m.ui.repo.path, app.git.summaries), ("repo", ref("praca").path, 1))
        self.assertIn("M file", self.text(m))
        self.press(m, "0", "i", "1")
        self.assertEqual(m.ui.screen, "detail")
        self.press(m, "v")
        self.assertEqual(app.calls[-1][2], "view")

    def test_github_open_local_and_clone_needs_typed_tak(self):
        m, app = self.make()
        catalogue = type("C", (), {"stale": False, "error": "", "repos": []})()
        entries = [{"nameWithOwner": "o/alpha", "isPrivate": False, "isFork": False, "isArchived": False, "local": True,
                    "local_paths": [ref("alpha").path]},
                   {"nameWithOwner": "o/fresh", "isPrivate": True, "isFork": False, "isArchived": False, "local": False,
                    "local_paths": []}]
        cloned = []
        with patch.object(m.github, "cached", return_value=catalogue), patch.object(m.github, "view", return_value=entries), \
                patch.object(m.github, "clone", side_effect=lambda name: cloned.append(name) or ref("fresh")):
            self.press(m, "g", "1")
            self.assertEqual((m.ui.screen, m.ui.repo.path), ("repo", ref("alpha").path))
            self.press(m, "0", "g")
            self.later(b"nie\n")
            self.press(m, "2")
            self.assertEqual(cloned, [])
            self.assertIn("anulowane", m.ui.message)
            self.later(b"tak\n")
            self.press(m, "2")
            self.assertEqual(cloned, ["o/fresh"])
            self.assertEqual((m.ui.screen, m.ui.repo.path), ("repo", ref("fresh").path))


@unittest.skipUnless(LAB, "dashboard lifecycle only under the disposable UID")
class DashboardRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        self.root = Path.home()/("menu-root-"+self._testMethodName)
        self.root.mkdir(exist_ok=True)
        self.path = self.root/"only"
        if not self.path.exists():
            import subprocess
            self.path.mkdir()
            subprocess.run(["git", "-C", str(self.path), "init", "-q"], check=True)
        self.config = Path.home()/("menu-"+self._testMethodName+".toml")
        base = (Path.home()/".config/hubi6/config.toml").read_text()
        self.config.write_text('repos_root = "'+str(self.root)+'"\n'+base)
        self.app = App(Config.load(str(self.config)))

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)
            if any(x.record.id == w.record.id for x in self.app.runtime_snapshot().workloads):
                self.app.stop(w.record.id, repair=True)

    def menu(self, **env):
        client = Client([], command=[sys.executable, "-m", "hubi6", "--config", str(self.config)], ui="dashboard",
                        env=env, size=(26, 65))
        self.addCleanup(client.close)
        client.read_until("AGENCI I TERMINALE".encode())
        return client

    def press(self, client, key, needle, timeout=5):
        time.sleep(.12)                              # a person never follows a redraw within the quiet window
        client.output = b""
        client.send(key)
        client.read_until(needle.encode(), timeout)

    def only(self):
        return wait_for(lambda: self.app.runtime_snapshot().workloads)[0]

    def attached(self, identity):
        def check():
            session = self.app.find(identity).session
            return session is not None and session.attached == 1
        wait_for(check)

    def test_start_attaches_and_clean_exit_returns_to_a_tidy_menu(self):
        client = self.menu()
        self.press(client, b"t", "Wybierz repozytorium")
        client.output = b""
        time.sleep(.12)
        client.send(b"1")
        w = self.only()
        self.attached(w.record.id)
        self.assertEqual(self.app.find(w.record.id).record.kind, "terminal")
        client.send(b"exit\n")
        client.read_until("posprzątane".encode(), 10)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_session_names_in_a_real_runtime(self):
        from contextlib import redirect_stdout
        from hubi6.cli import main
        show = lambda *args: self.app.tmux.command(*args).stdout
        detach = lambda: (client.send(b"\x02d"), client.read_until("AGENCI I TERMINALE".encode()))
        client = self.menu()
        # A typed name.
        self.press(client, b"t", "Wybierz repozytorium")
        self.assertNotIn("nazwa sesji:", plain(client.output))
        self.press(client, b"n", "Nazwa sesji [automatyczna]:")
        client.output = b""
        client.send(b"drugi\n")
        client.read_until("Wybierz repozytorium".encode())
        self.assertIn("nazwa sesji: drugi", plain(client.output))
        time.sleep(.12)
        client.send(b"1")
        named = self.only().record
        self.attached(named.id)
        self.assertEqual(named.instance, "drugi")
        detach()
        # Automatic names: the default one first, then numbers; nothing is entered or replaced.
        for expected in ("primary", "2"):
            before = {w.record.id for w in self.app.runtime_snapshot().workloads}
            self.press(client, b"t", "Wybierz repozytorium")
            client.output = b""
            time.sleep(.12)
            client.send(b"1")
            fresh = wait_for(lambda: [w for w in self.app.runtime_snapshot().workloads if w.record.id not in before])[0].record
            self.attached(fresh.id)
            self.assertEqual(fresh.instance, expected)
            detach()
        self.assertEqual(sorted(w.record.instance for w in self.app.runtime_snapshot().workloads), ["2", "drugi", "primary"])
        # The default name is display only: identity, record and JSON keep "primary".
        default = next(w.record for w in self.app.runtime_snapshot().workloads if w.record.instance == "primary")
        self.assertEqual(show("show-options", "-v", "-t", default.pane, "@hubi6-label").strip(), "$ terminal/1 · only")
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.config), "list"])
        for expected in ("terminal/1", "terminal/drugi", "terminal/2"):
            self.assertIn(expected, out.getvalue())
        self.assertNotIn("terminal/primary", out.getvalue())
        # The command line keeps "start if needed": the Polish default name addresses the running session.
        with redirect_stdout(io.StringIO()):
            main(["--config", str(self.config), "start", "terminal", str(self.path), "--instance", "główna", "--json"])
        self.assertEqual(len(self.app.runtime_snapshot().workloads), 3)

    def test_session_ended_after_switching_inside_tmux_is_tidied_too(self):
        first = self.app.start("terminal", str(self.path), "first")
        second = self.app.start("terminal", str(self.path), "second")
        client = self.menu()
        time.sleep(.12)
        client.send(b"1")
        self.attached(first.id)
        self.app.tmux.command("switch-client", "-c", self.app.tmux.command(
            "list-clients", "-F", "#{client_name}").stdout.strip(), "-t", "="+second.session)
        self.attached(second.id)
        client.send(b"exit\n")
        client.read_until("terminal/second".encode(), 10)
        client.read_until("posprzątane".encode(), 10)
        self.assertEqual([w.record.id for w in self.app.runtime_snapshot().workloads], [first.id])

    def test_failed_exit_is_kept_and_reported(self):
        self.config.write_text(self.config.read_text().replace(
            'codex = [', 'codex = ["/bin/sh", "-c", "sleep 1; exit 3"]\n#codex = [', 1))
        self.app = App(Config.load(str(self.config)))
        client = self.menu()
        self.press(client, b"c", "Wybierz repozytorium")
        client.output = b""
        time.sleep(.12)
        client.send(b"1")
        client.read_until("kod 3".encode(), 10)
        w = self.only()
        self.assertEqual(w.state, State.EXITED)
        self.assertEqual(w.session.exit_status, 3)

    def test_stop_needs_a_deliberate_q_and_a_paste_cannot_give_it(self):
        r = self.app.start("terminal", str(self.path))
        client = self.menu()
        time.sleep(.12)
        client.output = b""
        client.send(b"d1q")                           # one burst: nothing happens
        client.read_until("Zignorowano".encode())
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        self.press(client, b"d", "ZATRZYMAJ")
        client.send(b"1")
        time.sleep(.03)
        client.send(b"q")                             # typed before the question is on screen: dropped
        client.read_until("q = TAK".encode())
        time.sleep(.3)
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        self.press(client, b"n", "Anulowano")
        self.assertEqual(self.app.find(r.id).state, State.RUNNING)
        self.press(client, b"d", "ZATRZYMAJ")
        self.press(client, b"1", "q = TAK")
        self.press(client, b"q", "zatrzymano", 10)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_suspend_keys_and_signal_do_not_freeze_the_menu(self):
        client = self.menu()
        client.send(b"\x1a")
        client.p.send_signal(signal.SIGTSTP)
        time.sleep(.3)
        with open(f"/proc/{client.p.pid}/stat") as stream:
            self.assertNotEqual(stream.read().rsplit(")", 1)[1].split()[0], "T")
        self.press(client, b"?", "POMOC")

    def test_exit_codes_and_terminal_restoration(self):
        for sig, code in ((signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGTERM, 143)):
            client = self.menu()
            client.p.send_signal(sig)
            self.assertEqual(client.p.wait(3), code)
            self.assertEqual(termios.tcgetattr(client.slave), client.before)
        for data, code, env in ((b"q", 98, {}), (b"\x04", 0, {}), (b"x", 99, {"HUBI_AUTOLOGIN": "1"})):
            client = self.menu(**env)
            time.sleep(.12)
            client.send(data)
            self.assertEqual(client.p.wait(3), code)
            self.assertEqual(termios.tcgetattr(client.slave), client.before)
        client = self.menu()                           # outside autologin `x` is not an exit
        self.press(client, b"x", "nic tu nie robi")

    def test_session_presentation_and_guards_in_tmux(self):
        r = self.app.start("terminal", str(self.path), "named")
        show = lambda *args: self.app.tmux.command(*args).stdout
        self.assertEqual(show("show-options", "-v", "-t", r.pane, "@hubi6-label").strip(), "$ terminal/named · only")
        self.assertIn("detach-client -s ="+r.session, show("show-hooks", "-p", "-t", r.pane))
        # Replacing the placeholder process must not leave a hook error parked in the pane.
        self.assertEqual(show("display-message", "-p", "-t", r.pane, "#{pane_in_mode}").strip(), "0")
        self.assertEqual(show("show-options", "-w", "-v", "-t", r.pane, "window-size").strip(), "latest")
        self.app.preflight()
        keys = show("list-keys", "-T", "prefix")
        self.assertFalse([line for line in keys.splitlines() if line.split()[3] == "C-z"], keys)
        self.assertEqual(self.app.tmux.titles().keys(), {r.session})

    def test_terminal_exit_with_any_status_is_tidied(self):
        w = self.app.start("terminal", str(self.path))
        client = self.menu()
        time.sleep(.12)
        client.output = b""
        client.send(b"1")
        self.attached(w.id)
        client.send(b"false; exit 3\n")
        client.read_until("terminal/1: koniec pracy (kod 3), posprzątane".encode(), 10)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_signalled_terminal_and_failed_agent_stay(self):
        w = self.app.start("terminal", str(self.path))
        client = self.menu()
        time.sleep(.12)
        client.output = b""
        client.send(b"1")
        self.attached(w.id)
        client.send(b"kill -9 $$\n")
        client.read_until("przerwana sygnałem".encode(), 10)
        client.read_until("przerwany".encode(), 5)                      # the detail screen's state cell
        kept = self.only()
        self.assertEqual((kept.record.id, kept.state, kept.session.exit_signal), (w.id, State.EXITED, "9"))
        self.app.stop(w.id)
        self.config.write_text(self.config.read_text().replace(
            'codex = [', 'codex = ["/bin/sh", "-c", "sleep 1; exit 4"]\n#codex = [', 1))
        app = App(Config.load(str(self.config)))
        agent = app.start("codex", str(self.path))
        wait_for(lambda: app.find(agent.id).state == State.EXITED, 6)
        self.assertIsNone(app.reap_clean_exit(agent.id))                # contract 19: a failed agent stays
        self.assertEqual(app.find(agent.id).session.exit_status, 4)

    def test_one_command_terminal_tidies_after_a_failing_exit(self):
        client = Client(["--config", str(self.config), "terminal", "only"], ui="dashboard")
        self.addCleanup(client.close)
        w = self.only()
        self.attached(w.record.id)
        client.send(b"exit 5\n")
        self.assertEqual(client.p.wait(5), 0)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_one_command_opens_and_tidies(self):
        client = Client(["--config", str(self.config), "terminal", "only"], ui="dashboard")
        self.addCleanup(client.close)
        w = self.only()
        self.attached(w.record.id)
        client.send(b"exit\n")
        self.assertEqual(client.p.wait(5), 0)
        self.assertFalse(self.app.runtime_snapshot().workloads)

    def test_dashboard_fault_falls_back_to_the_line_menu(self):
        script = ("from unittest.mock import patch;from hubi6.cli import main;import sys;"
                  "patch('hubi6.menu.Menu.draw',side_effect=RuntimeError('injected')).start();"
                  "sys.exit(main(['--config',"+repr(str(self.config))+"]))")
        client = Client([], command=[sys.executable, "-c", script], ui="dashboard")
        self.addCleanup(client.close)
        client.read_until(b"Choice + Enter:")
        self.assertIn("menu klasyczne".encode(), client.output)


if __name__ == "__main__":
    unittest.main()
