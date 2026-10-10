from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hubi6.config import Config
from hubi6.core.model import HubiError
from hubi6.github import GitHub, rename_noreplace
from hubi6.services import App
from hubi6.storage import atomic_json


class FakeRunner:
    def __init__(self):
        self.calls = 0
        self.argv = []
        self.fail = False
        self.count = 0
        self.clone_fail = False
        self.hook = None

    def run(self, args, **kwargs):
        self.calls += 1
        args = list(map(str, args))
        self.argv.append(args)
        if args[:3] == ["gh", "repo", "list"]:
            if self.fail:
                raise HubiError("Offline")
            limit = int(args[args.index("--limit")+1])
            data = [{"nameWithOwner": f"owner/repo{i}", "isPrivate": True, "isFork": True, "isArchived": True} for i in range(min(limit, self.count))]
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        if args[0] == "systemd-run":
            ix = args.index("clone")
            url, dest = args[ix+1:ix+3]
            Path(dest).mkdir()
            if self.clone_fail:
                raise KeyboardInterrupt()
            subprocess.run(["git", "-C", dest, "init", "-q"], check=True)
            subprocess.run(["git", "-C", dest, "remote", "add", "origin", url], check=True)
            if self.hook:
                self.hook()
        return subprocess.CompletedProcess(args, 0, "", "")


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        c = Config.load()
        self.c = replace(c, repos=root/"repos", cache_base=root/"cache", state_base=root/"state", runtime_base=root, github_owner="owner")
        self.r = FakeRunner()
        self.app = App(self.c, self.r)
        # The config's production invariant remains enforced by Config.load;
        # unit tests replace paths solely to avoid touching real runtime data.
        self.gh = GitHub(self.app)

    def test_discovery_pagination_private_forks_archived(self):
        self.r.count = 205
        result = self.gh.list()
        self.assertEqual(len(result.repos), 205)
        self.assertEqual(len(self.r.argv), 3)
        self.assertTrue(all(r["isPrivate"] and r["isFork"] and r["isArchived"] for r in result.repos))

    def test_accessible_collaborator_and_org_pagination(self):
        gh = GitHub(App(replace(self.c, github_owner=""), self.r))
        original = self.r.run
        def run(args, **kwargs):
            if args[:2] == ["gh", "api"]:
                page = [{"full_name": "org/shared", "private": True, "fork": False, "archived": False}]
                return subprocess.CompletedProcess(args, 0, json.dumps(page)+"\n[]\n", "")
            return original(args, **kwargs)
        self.r.run = run
        self.assertEqual(gh.list().repos[0]["nameWithOwner"], "org/shared")

    def test_cache_ttl_offline(self):
        self.r.count = 1
        self.gh.list()
        calls = self.r.calls
        self.assertTrue(self.gh.list().cached)
        self.assertEqual(self.r.calls, calls)
        self.r.fail = True
        self.assertTrue(self.gh.list(refresh=True).stale)
        self.assertTrue(self.gh.list(offline=True).cached)

    def test_no_cache_offline_and_auth_failure(self):
        with self.assertRaises(HubiError):
            self.gh.list(offline=True)
        self.r.fail = True
        with self.assertRaises(HubiError):
            self.gh.list()

    def test_corrupt_cache(self):
        self.c.cache_base.mkdir()
        self.gh.cache.write_text("garbage")
        self.assertIsNone(self.gh.cached())

    def test_local_is_origin_not_directory(self):
        path = self.c.repos/"different-name"
        path.mkdir(parents=True)
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "git@github.com:OWNER/repo0.git"], check=True)
        self.r.count = 2
        view = self.gh.view(self.gh.list())
        self.assertTrue(view[0]["local"])
        self.assertFalse(view[1]["local"])

    def test_clone_atomic_https_existing_auth(self):
        ref = self.gh.clone("owner/private")
        self.assertEqual(ref.origin, "github.com/owner/private")
        args = next(x for x in self.r.argv if x[0] == "systemd-run")
        self.assertIn("https://github.com/owner/private.git", args)
        self.assertIn("credential.helper=!gh auth git-credential", args)
        self.assertFalse(list(self.c.repos.glob(".hubi6-clone-*")))
        with self.assertRaises(HubiError):
            self.gh.clone("owner/private")

    def test_clone_destination_collision(self):
        (self.c.repos/"private").mkdir(parents=True)
        (self.c.repos/"private"/"keep").write_text("owner data")
        with self.assertRaises(HubiError):
            self.gh.clone("owner/private")
        self.assertEqual((self.c.repos/"private"/"keep").read_text(), "owner data")

    def test_clone_interruption_cleanup(self):
        self.r.clone_fail = True
        with self.assertRaises(KeyboardInterrupt):
            self.gh.clone("owner/private")
        self.assertFalse((self.c.repos/"private").exists())
        self.assertFalse(list(self.c.repos.glob(".hubi6-clone-*")))
        self.assertTrue(any(x[:3] == ["systemctl", "--user", "stop"] for x in self.r.argv))

    def test_publish_collision_race(self):
        def create():
            (self.c.repos/"private").mkdir()
        self.r.hook = create
        with self.assertRaises(HubiError):
            self.gh.clone("owner/private")
        self.assertEqual(list((self.c.repos/"private").iterdir()), [])

    def test_sigkill_staging_recovery(self):
        staging = self.c.repos/".hubi6-clone-dead"
        staging.mkdir(parents=True, mode=0o700)
        atomic_json(staging/"lease.json", {"runtime": self.c.runtime_id,
                    "unit": f"hubi6-{self.c.runtime_id}-clone-{'a'*16}.service"})
        self.gh.clone("owner/new")
        self.assertFalse(staging.exists())

    def test_no_replace_directory_or_symlink(self):
        root = Path(self.temp.name)
        src, dst = root/"src", root/"dst"
        src.mkdir()
        dst.symlink_to(root/"missing")
        with self.assertRaises(HubiError):
            rename_noreplace(src, dst)
        self.assertTrue(dst.is_symlink())

    def test_invalid_names(self):
        for name in ("../x", "owner/..", "owner/repo;id", "owner/repo\n", "https://github.com/a/b", "owner/repo/extra"):
            with self.subTest(name=name), self.assertRaises(HubiError):
                self.gh.clone(name)
