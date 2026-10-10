import configparser
import os
from pathlib import Path
import re
import uuid
import tempfile
from urllib.parse import urlparse

from ..core.model import HubiError, RepoHealth, RepoRef, safe_path


def normalized_origin(url: str) -> str:
    """GitHub HTTPS, ssh:// and SCP spellings share one case-insensitive key."""
    url = url.strip()
    match = re.fullmatch(r"(?:[^/@:]+@)?github\.com:([^\s]+)", url, re.I)
    if match:
        path = match[1]
    else:
        parsed = urlparse(url)
        if parsed.hostname is None or parsed.hostname.lower() != "github.com":
            return ""
        if parsed.scheme not in ("https", "http", "ssh", "git"):
            return ""
        path = parsed.path.lstrip("/")
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return ""
    return "github.com/"+path.lower()


def git_directory(path: Path) -> Path:
    marker = path/".git"
    if marker.is_dir():
        result = marker.resolve() if marker.is_symlink() else marker
    elif marker.is_file():
        value = marker.read_text(errors="surrogateescape").strip()
        if not value.startswith("gitdir: "):
            raise HubiError("Invalid .git file")
        result = (path/value[8:]).resolve()
    else:
        raise HubiError("Not a Git working directory")
    try:
        value = (result/"HEAD").read_text().strip()
    except OSError as e:
        raise HubiError("Missing Git HEAD") from e
    if not value.startswith("ref: refs/") and not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise HubiError("Invalid Git HEAD")
    return result


def read_origin(gitdir: Path) -> str:
    config = gitdir/"config"
    if (gitdir/"commondir").is_file():
        config = (gitdir/(gitdir/"commondir").read_text().strip())/"config"
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(config, encoding="utf-8")
        # Do not expose embedded credentials in origins to JSON or the UI.
        return normalized_origin(parser.get('remote "origin"', "url", fallback=""))
    except (configparser.Error, OSError, UnicodeError):
        return ""


def reference(path: Path, *, resolved=False) -> RepoRef:
    path = path if resolved else path.resolve(strict=True)
    safe_path(str(path))
    gitdir = git_directory(path)
    s, g = path.stat(), gitdir.stat()
    try:
        workspace = (gitdir/"hubi6-workspace-id").read_text().strip()
    except FileNotFoundError:
        workspace = ""
    if workspace and not re.fullmatch(r"[a-f0-9]{32}", workspace):
        raise HubiError("Malformed Hubi workspace identity")
    return RepoRef(str(path), s.st_dev, s.st_ino, str(gitdir), g.st_dev, g.st_ino, g.st_ctime_ns, read_origin(gitdir), workspace)


def pin_workspace(ref: RepoRef) -> RepoRef:
    """A UUID in untracked Git administrative data defeats inode reuse too."""
    current = reference(Path(ref.path))
    identity = lambda r: (r.device, r.inode, r.git_device, r.git_inode)
    if identity(current) != identity(ref):
        raise HubiError("Repository changed while pinning workspace identity")
    path = Path(ref.git_dir)/"hubi6-workspace-id"
    if not current.workspace_id:
        fd, temporary = tempfile.mkstemp(prefix=".hubi6-id-", dir=ref.git_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(uuid.uuid4().hex+"\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    result = reference(Path(ref.path))
    if identity(result) != identity(ref) or not result.workspace_id:
        raise HubiError("Repository changed while pinning workspace identity")
    return result


def health(ref: RepoRef) -> RepoHealth:
    path = Path(ref.path)
    try:
        s = path.stat()
    except FileNotFoundError:
        return RepoHealth.MISSING
    except OSError:
        return RepoHealth.INVALID
    if (s.st_dev, s.st_ino) != (ref.device, ref.inode):
        return RepoHealth.REPLACED
    try:
        gitdir = git_directory(path)
        g = gitdir.stat()
        if (str(gitdir), g.st_dev, g.st_ino) != (ref.git_dir, ref.git_device, ref.git_inode):
            return RepoHealth.REPLACED
        if ref.workspace_id and (gitdir/"hubi6-workspace-id").read_text().strip() != ref.workspace_id:
            return RepoHealth.REPLACED
        return RepoHealth.OK
    except FileNotFoundError:
        return RepoHealth.REPLACED if ref.workspace_id else RepoHealth.INVALID
    except (HubiError, OSError, UnicodeError):
        return RepoHealth.INVALID


class Git:
    def __init__(self, config, runner):
        self.c, self.r = config, runner

    def env(self):
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def discover(self):
        """Filesystem-only render path. Git validates selected repos before start."""
        refs, errors = [], []
        try:
            with os.scandir(self.c.repos) as stream:
                entries = sorted(stream, key=lambda entry: os.fsencode(entry.name))
        except FileNotFoundError:
            return refs, []
        except OSError as e:
            return refs, [f"Cannot read repo root: {type(e).__name__}"]
        for entry in entries:
            if entry.name.startswith(".hubi6-") or not entry.is_dir():
                continue
            path = Path(entry.path)
            try:
                ref = reference(path, resolved=not entry.is_symlink())
                if not Path(ref.path).is_relative_to(self.c.repos) or Path(ref.path) == self.c.repos:
                    raise HubiError("Repository escapes repo root")
                refs.append(ref)
            except (HubiError, OSError, UnicodeError) as e:
                if (path/".git").exists():
                    errors.append(f"Invalid repository {path.name}: {type(e).__name__}")
        return refs, errors

    def resolve(self, value: str) -> RepoRef:
        path = Path(safe_path(value)).expanduser()
        if not path.is_absolute():
            path = self.c.repos/path
        try:
            ref = reference(path)
        except (OSError, UnicodeError) as e:
            raise HubiError("Repository is unavailable") from e
        self.validate(ref)
        return ref

    def validate(self, ref: RepoRef):
        path = Path(ref.path)
        if not path.is_relative_to(self.c.repos) or path == self.c.repos or health(ref) != RepoHealth.OK:
            raise HubiError("Repository missing, invalid, replaced or outside repo root")
        result = self.r.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], env=self.env())
        if Path(result.stdout.rstrip("\n")).resolve() != path:
            raise HubiError("Select the Git repository root")
        return ref

    def summary(self, ref):
        p = self.r.run(["git", "-C", ref.path, "status", "--short", "--branch"], env=self.env())
        return p.stdout
