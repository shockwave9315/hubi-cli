"""Each session name owns one Claude conversation. Storage on fakes; agent arguments only under the lab UID."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from hubi6.config import Config
from hubi6.core.model import HubiError, Record, display_name, key_id
from hubi6.services import App
from hubi6.storage import Store
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_menu import ref
from tests_v6.test_runtime import wait_for

U1 = "0b0e6f5c-1d2a-4c3b-8e9f-0a1b2c3d4e5f"
U2 = "9f8e7d6c-5b4a-4321-8fed-cba987654321"


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

    def run_agent(self, kind="claude", instance="primary", resume=False):
        """Start, read the agent's argv and env from its marker, stop. Returns (argv tail, env)."""
        before = set(self.path.glob(".agent-*"))
        r = self.app.start(kind, str(self.path), instance, resume)
        marker = wait_for(lambda: next(iter(set(self.path.glob(".agent-*"))-before), None))
        data = json.loads(wait_for(lambda: marker.read_text()))
        self.app.stop(r.id)
        return data["argv"][1:], data["env"]

    def saved(self, instance="primary"):
        return self.app.store.conversation(key_id("claude", str(self.path), instance))

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

    def test_codex_is_unchanged(self):
        args, env = self.run_agent("codex", resume=True)
        self.assertEqual(args[-1], "resume")
        self.assertEqual(env["HUBI6_NAME"], "1")
        self.assertEqual(self.app.resumable("codex", str(self.path)), [])

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
