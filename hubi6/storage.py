from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .config import private_dir
from .core.model import HubiError, Record, HEX20, KINDS, UUID, key_id


def atomic_json(path: Path, data):
    fd, temp = tempfile.mkstemp(prefix=".hubi6-"+path.name+"-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def private_json(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as f:
        s = os.fstat(f.fileno())
        if s.st_uid != os.getuid() or not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_size > 1048576:
            raise HubiError("Unsafe metadata file")
        return json.load(f)


class Store:
    def __init__(self, config):
        self.c = config

    def path(self, record):
        return self.c.records/(record.id+".json")

    def save(self, record):
        atomic_json(self.path(record), record.data())

    def load(self, path):
        return Record.parse(private_json(path), self.c.runtime_id, os.getuid())

    def all(self):
        records, errors = {}, []
        if not self.c.records.exists():
            return records, errors
        try:
            private_dir(self.c.records)
            for path in self.c.records.glob("*.json"):
                try:
                    r = self.load(path)
                    if path.name != r.id+".json":
                        raise HubiError("Metadata filename disagrees with identity")
                    records[r.id] = r
                except (HubiError, OSError, ValueError) as e:
                    errors.append(f"{path.name}: {type(e).__name__}; reconcile using a surviving runtime receipt, otherwise inspect/move this file manually")
        except HubiError as e:
            errors.append(str(e))
        return records, errors

    def conversation(self, key):
        """The agent conversation bound to one session name, or None."""
        data = self._binding(key)
        return data["conversation"] if data else None

    def _binding(self, key):
        if not HEX20.fullmatch(key):
            raise HubiError("Invalid lock identity")
        try:
            data = private_json(self.c.conversations/(key+".json"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, HubiError) as e:
            raise HubiError("Saved conversation of this session is unreadable; start a new conversation") from e
        if (not isinstance(data, dict) or not isinstance(data.get("conversation"), str) or
                not UUID.fullmatch(data["conversation"]) or data.get("kind") not in KINDS or
                not isinstance(data.get("repo"), str) or not isinstance(data.get("instance"), str)):
            raise HubiError("Saved conversation of this session is unreadable; start a new conversation")
        try:
            owner = key_id(data["kind"], data["repo"], data["instance"])
        except HubiError:
            owner = None
        if owner != key:                               # a file copied or moved under another name
            raise HubiError("Saved conversation of this session is unreadable; start a new conversation")
        return data

    def bind_conversation(self, record, conversation):
        """Remember that this session name holds this conversation; resume returns to it."""
        if not UUID.fullmatch(conversation):
            raise HubiError("Saved conversation of this session is unreadable; start a new conversation")
        self.c.prepare()
        atomic_json(self.c.conversations/(record.key+".json"),
                    {"kind": record.kind, "repo": record.repo.path, "instance": record.instance,
                     "conversation": conversation, "bound": time.time()})

    def unbind_conversation(self, key):
        """Forget the conversation of one session name: a new conversation replaces it."""
        if not HEX20.fullmatch(key):
            raise HubiError("Invalid lock identity")
        try:
            (self.c.conversations/(key+".json")).unlink()
        except FileNotFoundError:
            pass

    def bindings(self):
        """Every readable binding, newest first. Damaged files are skipped, never removed."""
        found = []
        if not self.c.conversations.is_dir():
            return found
        for path in self.c.conversations.glob("*.json"):
            try:
                data = self._binding(path.stem)
            except HubiError:
                continue
            if data:
                found.append(data)
        return sorted(found, key=lambda d: d.get("bound", 0), reverse=True)

    def remove(self, record):
        self.path(record).unlink(missing_ok=True)
        for suffix in ("payload", "cancel"):
            (self.c.runtime/(record.id+"."+suffix)).unlink(missing_ok=True)

    def cleanup_temps(self):
        # Old interrupted atomic writes only. Never unlink flock files during
        # operation: a waiter may already have opened their inode (ABA hazard).
        for directory in (self.c.records, self.c.runtime):
            for path in directory.glob(".hubi6-*"):
                try:
                    s = path.lstat()
                    if (stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and
                        not s.st_mode & 0o077 and time.time()-s.st_mtime > 86400):
                        path.unlink()
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self, key):
        if not HEX20.fullmatch(key):
            raise HubiError("Invalid lock identity")
        self.c.prepare()
        self.cleanup_temps()
        fd = os.open(self.c.runtime/"locks"/key, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if s.st_uid != os.getuid() or not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077:
                raise HubiError("Unsafe lifecycle lock")
            end = time.monotonic()+self.c.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise HubiError("Workload busy: lifecycle lock timed out")
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)
