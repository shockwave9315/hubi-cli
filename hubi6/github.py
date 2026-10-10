from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .adapters.git import normalized_origin, reference
from .config import private_dir
from .core.model import HubiError
from .storage import atomic_json, private_json


FIELDS = "nameWithOwner,url,isPrivate,isFork,isArchived,description,pushedAt"


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", value, re.ASCII) or value.split("/")[1] in (".", ".."):
        raise HubiError("Expected GitHub owner/repository")
    return value


@dataclass
class Catalogue:
    repos: list
    cached: bool
    stale: bool
    error: str = ""


class GitHub:
    def __init__(self, app):
        self.app, self.c, self.r = app, app.c, app.r
        key = hashlib.sha256(self.c.github_owner.encode()).hexdigest()[:16]
        self.cache = self.c.cache_base/("github-"+key+".json")

    def cached(self):
        try:
            data = private_json(self.cache)
            repos = self.validate(data["repos"])
            return Catalogue(repos, True, time.time()-data["time"] > self.c.github_ttl)
        except (OSError, ValueError, HubiError, KeyError, TypeError):
            return None

    @staticmethod
    def validate(data):
        if not isinstance(data, list):
            raise HubiError("GitHub returned invalid JSON")
        repos = []
        for item in data:
            if not isinstance(item, dict):
                raise HubiError("Invalid repository metadata")
            name = repo_name(item.get("nameWithOwner", ""))
            repos.append({"nameWithOwner": name, "url": "https://github.com/"+name,
                          "isPrivate": bool(item.get("isPrivate")), "isFork": bool(item.get("isFork")),
                          "isArchived": bool(item.get("isArchived")), "description": str(item.get("description") or ""),
                          "pushedAt": str(item.get("pushedAt") or "")})
        return sorted(repos, key=lambda r: r["nameWithOwner"].lower())

    def list(self, refresh=False, offline=False):
        cache = self.cached()
        if offline:
            if cache:
                return cache
            raise HubiError("No GitHub cache available offline")
        if cache and not cache.stale and not refresh:
            return cache
        try:
            limit = 100
            deadline = time.monotonic()+30
            while True:
                args = ["gh", "repo", "list"]
                if self.c.github_owner:
                    args.append(self.c.github_owner)
                args += ["--json", FIELDS, "--limit", str(limit)]
                result = self.r.run(args, timeout=max(0.1, deadline-time.monotonic()))
                data = self.validate(json.loads(result.stdout))
                if len(data) < limit:
                    break
                if limit >= 102400 or time.monotonic() >= deadline:
                    raise HubiError("GitHub discovery limit/timeout reached; refusing a truncated catalogue")
                limit *= 2  # gh paginates internally up to --limit
            if not self.c.github_owner:
                # repo list only lists owned repositories. Include collaborators
                # and organization memberships using the same gh authentication.
                # Decode successive page arrays: works with gh 2.46 (no --slurp).
                result = self.r.run(["gh", "api", "--paginate", "user/repos?per_page=100&affiliation=owner,collaborator,organization_member"],
                                    timeout=max(.1, deadline-time.monotonic()))
                rest = result.stdout.lstrip()
                decoder = json.JSONDecoder()
                accessible = []
                while rest:
                    page, end = decoder.raw_decode(rest)
                    if not isinstance(page, list):
                        raise HubiError("Invalid GitHub pagination response")
                    accessible.extend({"nameWithOwner": r["full_name"], "isPrivate": r["private"], "isFork": r["fork"],
                                       "isArchived": r["archived"], "description": r.get("description"), "pushedAt": r.get("pushed_at")}
                                      for r in page)
                    rest = rest[end:].lstrip()
                combined = {r["nameWithOwner"].lower(): r for r in data+self.validate(accessible)}
                data = sorted(combined.values(), key=lambda r: r["nameWithOwner"].lower())
            private_dir(self.c.cache_base, create=True)
            atomic_json(self.cache, {"time": time.time(), "repos": data})
            return Catalogue(data, False, False)
        except (HubiError, ValueError, KeyError, TypeError) as e:
            if cache:
                cache.stale, cache.error = True, "GitHub unavailable; showing saved catalogue"
                return cache
            raise HubiError("GitHub discovery failed; check gh auth status or try offline cache") from e

    def view(self, catalogue):
        local, _ = self.app.git.discover()
        origins = {}
        for ref in local:
            if ref.origin:
                origins.setdefault(ref.origin, []).append(ref.path)
        return [dict(repo, local_paths=origins.get(normalized_origin(repo["url"]), []),
                     local=bool(origins.get(normalized_origin(repo["url"])))) for repo in catalogue.repos]

    @contextmanager
    def clone_lock(self):
        self.c.prepare()
        fd = os.open(self.c.runtime/"clone.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise HubiError("Another clone is in progress") from e
            yield
        finally:
            os.close(fd)

    def cleanup_interrupted(self):
        """Only owned marked staging directories, under the clone lock."""
        for p in self.c.repos.glob(".hubi6-clone-*"):
            try:
                lease = private_json(p/"lease.json")
                if lease["runtime"] != self.c.runtime_id or p.is_symlink() or p.stat().st_uid != os.getuid():
                    continue
                self.stop_clone_unit(lease["unit"])
                shutil.rmtree(p)
            except (OSError, HubiError, KeyError, ValueError):
                continue

    def stop_clone_unit(self, unit):
        pattern = r"hubi6-"+self.c.runtime_id+r"-clone-[a-f0-9]{16}\.service"
        if not re.fullmatch(pattern, unit):
            raise HubiError("Foreign clone unit refused")
        # Unique generation, exact unit, KillMode=control-group, bounded service
        # timeout. This cleans git and credential-helper descendants too.
        self.r.run(["systemctl", "--user", "stop", unit], timeout=10, check=False)

    def clone(self, name):
        name = repo_name(name)
        self.c.repos.mkdir(parents=True, exist_ok=True)
        with self.clone_lock():
            self.cleanup_interrupted()
            origins, _ = self.app.git.discover()
            match = normalized_origin("https://github.com/"+name)
            if any(r.origin == match for r in origins):
                raise HubiError("Repository is already LOCAL")
            destination = self.c.repos/name.split("/")[1]
            if destination.exists() or destination.is_symlink():
                raise HubiError("Destination exists; nothing overwritten")
            token = uuid.uuid4().hex[:16]
            staging = self.c.repos/(".hubi6-clone-"+token)
            staging.mkdir(mode=0o700)
            unit = f"hubi6-{self.c.runtime_id}-clone-{token}.service"
            atomic_json(staging/"lease.json", {"runtime": self.c.runtime_id, "unit": unit})
            try:
                # HTTPS and existing gh credential helper. Never read/store a token.
                self.r.run(["systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet",
                            "--unit="+unit, "--description=Hubi6 GitHub clone", "--property=KillMode=control-group",
                            "--property=RuntimeMaxSec=60", "--property=TimeoutStopSec=3",
                            "--setenv=HOME="+str(self.c.home), "--setenv=GIT_TERMINAL_PROMPT=0", "--",
                            shutil.which("gh") or "gh", "repo", "clone", "https://github.com/"+name+".git", str(staging/"repo"),
                            "--", "--config", "credential.helper=", "--config", "credential.helper=!gh auth git-credential"], timeout=65)
                ref = reference(staging/"repo")
                if ref.origin != match:
                    raise HubiError("Cloned origin does not match requested repository")
                # Linux renameat2 NOREPLACE closes the destination-check race.
                rename_noreplace(staging/"repo", destination)
                return reference(destination)
            finally:
                self.stop_clone_unit(unit)
                shutil.rmtree(staging, ignore_errors=True)


def rename_noreplace(source, destination):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise HubiError("Atomic no-replace rename requires Linux renameat2")
    result = fn(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result:
        error = ctypes.get_errno()
        raise HubiError(f"Atomic clone publish refused (errno {error}); nothing overwritten")
