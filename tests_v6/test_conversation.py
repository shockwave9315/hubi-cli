"""Each session name owns one Claude conversation. Storage on fakes; agent arguments only under the lab UID."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from hubi6.config import Config
from hubi6.core.model import HubiError, Record, display_name, key_id
from hubi6.services import App, newest_codex_conversation
from hubi6.storage import Store
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_menu import ref
from tests_v6.test_runtime import wait_for

U1 = "0b0e6f5c-1d2a-4c3b-8e9f-0a1b2c3d4e5f"
U2 = "9f8e7d6c-5b4a-4321-8fed-cba987654321"
U3 = "01a1620d-93e6-7022-a280-f0e7b9712c4d"


def codex_file(day, conversation, cwd, *, n=0, written=None, **meta):
    """A Codex conversation file as Codex 0.163 writes it: session_meta first."""
    day.mkdir(parents=True, exist_ok=True)
    path = day/f"rollout-2026-10-22T05-20-{n:02d}-{conversation}.jsonl"
    payload = {"id": conversation, "cwd": cwd, "source": "cli", "thread_source": "user", **meta}
    path.write_text(json.dumps({"type": "session_meta", "payload": payload})+"\n"+'{"type":"event_msg"}\n')
    if written is not None:
        os.utime(path, (written, written))
    return path


class CodexFileTests(unittest.TestCase):
    """Which open file is the session's Codex conversation."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.base = os.path.realpath(tmp)+"/sessions"
        self.day = Path(self.base)/"2026/10/22"

    def pick(self, *paths, cwd="/r"):
        return newest_codex_conversation([str(p) for p in paths], self.base, cwd)

    def test_the_conversation_written_last_counts(self):
        # After /new Codex keeps the old file open too; the one it writes now wins.
        old = codex_file(self.day, U1, "/r", n=1, written=1000)
        new = codex_file(self.day, U2, "/r", n=2, written=2000)
        self.assertEqual(self.pick(old, new), U2)
        os.utime(old, (3000, 3000))                    # back to the old one (/resume in Codex)
        self.assertEqual(self.pick(old, new), U1)
        self.assertIsNone(self.pick())

    def test_files_that_are_not_this_sessions_conversation_never_count(self):
        main = codex_file(self.day, U1, "/r", n=1, written=1000)
        others = [
            codex_file(self.day, U2, "/elsewhere", n=2, written=2000),            # another repository
            # A helper agent's thread: Codex 0.163 marks it both ways; either one is enough.
            codex_file(self.day, U3, "/r", n=3, written=2000, thread_source="subagent"),
            codex_file(self.day, U2, "/r", n=6, written=2000,
                       source={"subagent": {"thread_spawn": {"parent_thread_id": U1}}}),
        ]
        mismatch = self.day/f"rollout-2026-10-22T05-20-04-{U3}.jsonl"
        mismatch.write_text(json.dumps({"type": "session_meta", "payload": {"id": U2, "cwd": "/r"}})+"\n")
        broken = self.day/f"rollout-2026-10-22T05-20-05-{U3}.jsonl"
        broken.write_text("{oops\n")
        listed = self.day/f"rollout-2026-10-22T05-20-07-{U2}.jsonl"
        listed.write_text("[1]\n")                     # JSON, but not an object
        outside = codex_file(Path(self.base).parent/"elsewhere", U2, "/r", written=2000)
        plain = Path(self.base)/"history.jsonl"
        plain.write_text("{}\n")
        for other in [*others, mismatch, broken, listed, outside, plain, self.day/"missing.jsonl", "socket:[1]"]:
            with self.subTest(other=str(other)[-40:]):
                self.assertEqual(self.pick(main, other), U1)
                self.assertIsNone(self.pick(other))


class BindingStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        base = Path(tmp)
        os.chmod(base, 0o700)
        (base/"run").mkdir(mode=0o700)
        self.store = Store(replace(Config.load(), state_base=base/"state", runtime_base=base/"run"))

    def record(self, instance="primary", name="praca"):
        return Record("a"*20, os.getuid(), "claude", instance, key_id("claude", ref(name).path, instance), "b"*16, ref(name))

    def test_binding_survives_and_is_per_name(self):
        main, rev = self.record(), self.record("rev")
        self.assertIsNone(self.store.conversation(main.key))
        self.store.bind_conversation(main, U1)
        self.store.bind_conversation(rev, U2)
        self.assertEqual((self.store.conversation(main.key), self.store.conversation(rev.key)), (U1, U2))
        path = self.store.c.conversations/(main.key+".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_newest_binding_first_and_rebinding_replaces(self):
        main, rev = self.record(), self.record("rev")
        self.store.bind_conversation(main, U1)
        time.sleep(.01)
        self.store.bind_conversation(rev, U2)
        self.assertEqual([b["instance"] for b in self.store.bindings()], ["rev", "primary"])
        time.sleep(.01)
        self.store.bind_conversation(main, U2)
        self.assertEqual([b["instance"] for b in self.store.bindings()], ["primary", "rev"])
        self.assertEqual(self.store.conversation(main.key), U2)

    def test_damaged_binding_is_an_actionable_error_and_skipped_in_lists(self):
        main = self.record()
        self.store.bind_conversation(main, U1)
        path = self.store.c.conversations/(main.key+".json")
        for content in ("{oops", json.dumps({"conversation": "not-a-uuid", "kind": "claude", "repo": "/x", "instance": "primary"})):
            with self.subTest(content=content[:10]):
                path.write_text(content)
                with self.assertRaisesRegex(HubiError, "start a new conversation"):
                    self.store.conversation(main.key)
                self.assertEqual(self.store.bindings(), [])
        with self.assertRaises(HubiError):
            self.store.bind_conversation(main, "../../etc")

    def test_binding_filed_under_another_name_is_refused(self):
        main, other = self.record(), self.record("x")
        self.store.bind_conversation(main, U1)
        source = self.store.c.conversations/(main.key+".json")
        (self.store.c.conversations/(other.key+".json")).write_bytes(source.read_bytes())
        os.chmod(self.store.c.conversations/(other.key+".json"), 0o600)
        with self.assertRaisesRegex(HubiError, "start a new conversation"):
            self.store.conversation(other.key)             # never another name's conversation
        self.assertEqual([b["instance"] for b in self.store.bindings()], ["primary"])

    def test_a_new_conversation_forgets_the_saved_one(self):
        main = self.record()
        self.store.bind_conversation(main, U1)
        self.store.unbind_conversation(main.key)
        self.assertIsNone(self.store.conversation(main.key))
        self.store.unbind_conversation(main.key)      # nothing saved: no error

    def test_odd_claude_session_file_means_unknown(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp/"sessions").mkdir()
        w = SimpleNamespace(session=SimpleNamespace(pid=os.getpid()), record=SimpleNamespace(repo=SimpleNamespace(path="/r")))
        app = App(self.store.c)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp)}):
            for content in ("[1]", '"x"', "{oops"):
                with self.subTest(content=content):
                    (tmp/"sessions"/f"{os.getpid()}.json").write_text(content)
                    self.assertIsNone(app.claude_conversation(w))

    def test_default_name_is_one_everywhere(self):
        self.assertEqual((display_name("primary"), display_name("rev")), ("1", "rev"))


@unittest.skipUnless(LAB, "agent arguments only under the disposable UID")
class ConversationRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.app = App(Config.load())
        name = "conv-"+self._testMethodName
        shutil.rmtree(Path.home()/"repos"/name, ignore_errors=True)
        self.path = repo(name)
        shutil.rmtree(self.app.c.conversations, ignore_errors=True)
        self.projects = Path.home()/".claude/projects/-lab"
        self.projects.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for w in self.app.runtime_snapshot().workloads:
            self.app.stop(w.record.id, repair=True)
        shutil.rmtree(Path.home()/".claude/projects", ignore_errors=True)
        shutil.rmtree(Path.home()/".claude/sessions", ignore_errors=True)
        shutil.rmtree(Path.home()/".codex", ignore_errors=True)

    def run_agent(self, kind="claude", instance="primary", resume=False):
        """Start, read the agent's argv and env from its marker, stop. Returns (argv tail, env)."""
        before = set(self.path.glob(".agent-*"))
        r = self.app.start(kind, str(self.path), instance, resume)
        marker = wait_for(lambda: next(iter(set(self.path.glob(".agent-*"))-before), None))
        data = json.loads(wait_for(lambda: marker.read_text()))
        self.app.stop(r.id)
        return data["argv"][1:], data["env"]

    def saved(self, instance="primary", kind="claude"):
        return self.app.store.conversation(key_id(kind, str(self.path), instance))

    def test_new_conversation_gets_a_fixed_id_and_resume_returns_to_it(self):
        args, env = self.run_agent()
        self.assertEqual(env["HUBI6_NAME"], "1")
        self.assertEqual(args[-2], "--session-id")
        first = args[-1]
        self.assertEqual(self.saved(), first)
        (self.projects/(first+".jsonl")).write_text("{}\n")      # Claude writes it on the first message
        args, _ = self.run_agent(resume=True)
        self.assertEqual(args[-2:], ["--resume", first])
        self.assertEqual(self.saved(), first)
        args, _ = self.run_agent(resume=True)                     # again: still the same conversation
        self.assertEqual(args[-2:], ["--resume", first])

    def test_saved_but_never_written_conversation_starts_fresh_on_resume(self):
        args, _ = self.run_agent()
        first = args[-1]
        args, _ = self.run_agent(resume=True)                     # no transcript: nothing to resume
        self.assertEqual(args[-2], "--session-id")
        self.assertNotEqual(args[-1], first)
        self.assertEqual(self.saved(), args[-1])

    def test_names_never_share_a_conversation(self):
        main, _ = self.run_agent()
        rev, env = self.run_agent(instance="rev")
        self.assertEqual(env["HUBI6_NAME"], "rev")
        self.assertNotEqual(main[-1], rev[-1])
        for conversation in (main[-1], rev[-1]):
            (self.projects/(conversation+".jsonl")).write_text("{}\n")
        self.assertEqual(self.run_agent(instance="rev", resume=True)[0][-2:], ["--resume", rev[-1]])
        self.assertEqual(self.run_agent(resume=True)[0][-2:], ["--resume", main[-1]])
        self.assertEqual(self.app.resumable("claude", str(self.path)), ["rev", "primary"])

    def test_name_without_a_saved_conversation_offers_claudes_own_list(self):
        args, _ = self.run_agent(instance="stary", resume=True)
        self.assertEqual(args[-1], "--resume")
        self.assertNotIn("--session-id", args)
        self.assertIsNone(self.saved("stary"))

    def test_new_conversation_replaces_the_saved_one(self):
        first = self.run_agent()[0][-1]
        (self.projects/(first+".jsonl")).write_text("{}\n")
        second = self.run_agent()[0][-1]
        self.assertNotEqual(first, second)
        self.assertEqual(self.saved(), second)

    def follow(self, conversation, *, transcript=True, cwd=None, pid=None, instance="primary"):
        """A running session whose Claude moved to another conversation (/clear); returns its record."""
        before = set(self.path.glob(".agent-*"))
        r = self.app.start("claude", str(self.path), instance)
        marker = wait_for(lambda: next(iter(set(self.path.glob(".agent-*"))-before), None))
        agent = int(marker.name.split("-")[1])
        sessions = Path.home()/".claude/sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions/f"{agent}.json").write_text(json.dumps({"pid": pid or agent, "sessionId": conversation,
                                                          "cwd": cwd or str(self.path)}))
        if transcript:
            (self.projects/(conversation+".jsonl")).write_text("{}\n")
        return r

    def test_clear_in_claude_moves_the_name_to_the_new_conversation_at_stop(self):
        r = self.follow(U2)
        self.assertNotEqual(self.saved(), U2)
        self.app.stop(r.id)
        self.assertEqual(self.saved(), U2)
        self.assertEqual(self.run_agent(resume=True)[0][-2:], ["--resume", U2])

    def test_capture_after_attach_needs_a_matching_file_and_transcript(self):
        for kw in ({"transcript": False}, {"cwd": "/elsewhere"}, {"pid": 1}):
            with self.subTest(**kw):
                r = self.follow(U2, **kw)
                first = self.saved()
                self.app.capture_conversations()
                self.assertEqual(self.saved(), first)
                self.app.stop(r.id)
                self.assertEqual(self.saved(), first)
        r = self.follow(U2)
        self.app.capture_conversations()                     # what App.attach does on return
        self.assertEqual(self.saved(), U2)
        self.app.stop(r.id)

    def test_never_moves_a_name_onto_another_names_conversation(self):
        first = self.run_agent()[0][-1]                       # "1" holds this conversation
        r = self.follow(first, instance="rev")              # in "rev" the owner opens it from Claude's list
        rev = self.saved("rev")
        self.assertNotEqual(rev, first)
        self.app.stop(r.id)
        self.assertEqual((self.saved(), self.saved("rev")), (first, rev))   # still one name per conversation

    def test_codex_without_a_saved_conversation_shows_its_list(self):
        args, env = self.run_agent("codex", resume=True)
        self.assertEqual(args[-1], "resume")
        self.assertEqual(env["HUBI6_NAME"], "1")
        self.assertEqual(self.app.resumable("codex", str(self.path)), [])

    def codex_files(self, *conversations, **meta):
        day, cwd = Path.home()/".codex/sessions/2026/10/22", meta.pop("cwd", str(self.path))
        return [codex_file(day, c, cwd, n=n, **meta) for n, c in enumerate(conversations)]

    def codex_running(self, *files, instance="primary"):
        """A running Codex session that keeps these conversation files open, as Codex does."""
        before = set(self.path.glob(".agent-*"))
        (self.path/".open").write_text("\n".join(map(str, files)))
        r = self.app.start("codex", str(self.path), instance)
        marker = wait_for(lambda: next(iter(set(self.path.glob(".agent-*"))-before), None))
        opened = self.path/(".opened-"+marker.name.split("-")[1])
        wait_for(lambda: opened.exists() and opened.read_text() == str(len(files)))
        return r

    def test_codex_resume_returns_to_the_conversation_it_wrote(self):
        r = self.codex_running(*self.codex_files(U3))
        self.assertIsNone(self.saved(kind="codex"))
        self.app.stop(r.id)                                   # noted before it ends
        self.assertEqual(self.saved(kind="codex"), U3)
        self.assertEqual(self.app.resumable("codex", str(self.path)), ["primary"])
        (self.path/".open").unlink()
        args, _ = self.run_agent("codex", resume=True)
        self.assertEqual(args[-2:], ["resume", U3])
        args, _ = self.run_agent("codex", resume=True)        # again: still the same conversation
        self.assertEqual(args[-2:], ["resume", U3])

    def test_new_codex_conversation_replaces_the_saved_one(self):
        # Review of #14: after a new conversation ended by /exit, resume silently opened the older one.
        path, = self.codex_files(U3)
        self.app.stop(self.codex_running(path).id)
        self.assertEqual(self.saved(kind="codex"), U3)
        (self.path/".open").unlink()
        self.run_agent("codex")                              # a new conversation Hubi never got to note
        self.assertIsNone(self.saved(kind="codex"))
        args, _ = self.run_agent("codex", resume=True)
        self.assertEqual(args[-1], "resume")                 # Codex's list, never the older U3
        self.assertNotIn(U3, args)

    def test_codex_follows_the_conversation_it_writes_now(self):
        old, new = self.codex_files(U1, U2)
        os.utime(old, (1000, 1000))
        r = self.codex_running(old, new)                     # /new in Codex: both files stay open
        self.app.capture_conversations()                     # what App.attach does on return
        self.assertEqual(self.saved(kind="codex"), U2)
        os.utime(new, (500, 500))                            # back to the old one, which it writes again
        self.app.send("codex", str(self.path), "primary", "dalej")   # a tool's message notes it too
        self.assertEqual(self.saved(kind="codex"), U1)
        self.app.stop(r.id)

    def test_codex_never_takes_another_repositorys_or_a_helpers_conversation(self):
        files = self.codex_files(U1, cwd="/elsewhere")+self.codex_files(U2, thread_source="subagent")
        r = self.codex_running(*files)
        self.app.stop(r.id)
        self.assertIsNone(self.saved(kind="codex"))

    def test_codex_saved_conversation_that_is_gone_shows_the_list(self):
        path, = self.codex_files(U3)
        self.app.stop(self.codex_running(path).id)
        (self.path/".open").unlink()
        path.unlink()
        args, _ = self.run_agent("codex", resume=True)
        self.assertEqual(args[-1], "resume")
        self.assertNotIn(U3, args)

    def test_damaged_binding_refuses_resume_until_a_new_conversation(self):
        self.run_agent()
        path = self.app.c.conversations/(key_id("claude", str(self.path), "primary")+".json")
        path.write_text("{oops")
        with self.assertRaisesRegex(HubiError, "start a new conversation"):
            self.app.start("claude", str(self.path), "primary", True)
        args, _ = self.run_agent()
        self.assertEqual(self.saved(), args[-1])


if __name__ == "__main__":
    unittest.main()
