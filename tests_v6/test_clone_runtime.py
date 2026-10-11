import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from hubi6.github import GitHub
from hubi6.core.model import HubiError
from hubi6.services import App
from tests_v6.support import LAB, bootstrap, repo
from tests_v6.test_runtime import wait_for


@unittest.skipUnless(LAB, "requires disposable UID")
class CloneRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = bootstrap()
        import shutil
        shutil.rmtree(Path.home()/"repos/private-fixture", ignore_errors=True)
        cls.bin = Path.home()/"fake-bin"
        cls.bin.mkdir(exist_ok=True)
        source = repo("clone-source")
        (source/"README").write_text("synthetic private repository")
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                        "add", "README"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Lab", "-c", "user.email=lab@example.invalid",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        gh = cls.bin/"gh"
        gh.write_text('''#!/usr/bin/python3
import json,os,subprocess,sys,time
from pathlib import Path
args=sys.argv[1:]
if args[:2]==["repo","clone"]:
 url,dest=args[2:4]
 if "auth-error" in url:
  print("authentication required: ghp_syntheticSecretDoNotShow https://user:password@github.com/a/b",file=sys.stderr)
  sys.exit(42)
 if "slow" in url:
  Path(dest).mkdir()
  Path(dest,"partial").write_text("interrupted")
  time.sleep(30)
 else:
  subprocess.run(["git","clone","-q","--no-hardlinks",str(Path.home()/"repos/clone-source"),dest,*args[5:]],check=True)
  subprocess.run(["git","-C",dest,"remote","set-url","origin",url],check=True)
elif args[:2]==["repo","list"]:print(json.dumps([{"nameWithOwner":"owner/private-fixture","isPrivate":True}]))
elif args[0]=="api":print("[]")
else:sys.exit(1)
''')
        gh.chmod(0o700)

    def setUp(self):
        self.environment = patch.dict(os.environ, {"PATH": str(self.bin)+":"+os.environ["PATH"]})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_private_clone_real_git_and_service(self):
        gh = GitHub(self.app)
        view = gh.view(gh.list(refresh=True))
        self.assertTrue(view[0]["isPrivate"])
        self.assertFalse(view[0]["local"])
        ref = gh.clone("owner/private-fixture")
        self.assertTrue((Path(ref.path)/"README").is_file())
        helpers=subprocess.run(['git','-C',ref.path,'config','--local','--get-all','credential.helper'],capture_output=True,text=True)
        self.assertFalse(helpers.stdout.strip())
        self.assertTrue(gh.view(gh.list())[0]["local"])
        r = self.app.start("terminal", ref.path)
        self.app.stop(r.id)

    def test_clone_term_sigkill_and_staging_recovery(self):
        for signum in (15, 9):
            proc = subprocess.Popen([sys.executable, "-m", "hubi6", "github", "clone", "owner/slow"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_for(lambda: any(self.app.c.repos.glob(".hubi6-clone-*/repo/partial")))
                proc.send_signal(signum)
                proc.wait(5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                with GitHub(self.app).clone_lock():
                    GitHub(self.app).cleanup_interrupted()
            self.assertFalse(list(self.app.c.repos.glob(".hubi6-clone-*")))
            self.assertFalse((self.app.c.repos/"slow").exists())
            units = self.app.r.run(["systemctl", "--user", "list-units", "--all", "--no-legend", f"hubi6-{self.app.c.runtime_id}-clone-*.service"]).stdout
            self.assertNotIn("running", units)

    def test_auth_error_redaction_timeout_and_path(self):
        gh=GitHub(self.app)
        with self.assertRaises(HubiError) as error:
            gh.clone('owner/auth-error')
        message=str(error.exception)
        self.assertIn('authentication required',message)
        self.assertNotIn('syntheticSecret',message)
        self.assertNotIn('password',message)
        self.assertIn('[REDACTED]',message)
        fast=GitHub(App(replace(self.app.c,clone_timeout=.25)))
        with self.assertRaisesRegex(HubiError,'timed out'):
            fast.clone('owner/slow-timeout')
        self.assertFalse(list(self.app.c.repos.glob('.hubi6-clone-*')))
        self.assertFalse((self.app.c.repos/'slow-timeout').exists())
