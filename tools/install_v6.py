#!/usr/bin/env python3
"""Explicit per-user install/uninstall of a built v6 bundle. No v5 lifecycle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


def replace_file(path, data, mode):
    """Replace the directory entry; never write through an old launcher symlink."""
    fd, name = tempfile.mkstemp(prefix='.hubi6-install-', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name,mode)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(*argv, check=True):
    return subprocess.run(argv, check=check, text=True, capture_output=True)


def file_hash(path):
    """Only regular files owned by the installing UID authorize removal."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Refusing unverified installation file: "+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remove_owned(path, expected):
    try:
        actual = file_hash(path)
    except FileNotFoundError:
        return
    if actual != expected:
        raise ValueError("Installed file was changed; preserving it: "+str(path))
    path.unlink()


def zero_state(c):
    # Check ALL v6 runtime IDs on this UID, not only the selected journal.
    from hubi6.adapters.systemd import parse_units, PROPERTIES
    from hubi6.core.model import HubiError
    units = run("systemctl", "--user", "show", "hubi6-*.scope", "--property="+PROPERTIES)
    if any(u.live or u.active == "unknown" for u in parse_units(units.stdout).values()):
        raise HubiError("Stop all v6 scopes before install/uninstall")
    service = run("systemctl", "--user", "is-active", "hubi6-tmux.service", check=False)
    if c.socket.exists() or service.returncode == 0:
        clients = run("tmux", "-N", "-S", str(c.socket), "list-clients", "-F", "#{client_pid}", check=False)
        if clients.stdout.strip():
            raise HubiError("Detach all v6 clients before install/uninstall")
        tmux = run("tmux", "-N", "-S", str(c.socket), "list-sessions", "-F", "#{session_name}", check=False)
        if tmux.stdout.strip() or tmux.returncode and tmux.stderr.strip() != "no server running on "+str(c.socket):
            # Empty-server tmux versions report "no sessions" or "no current target".
            if tmux.stdout.strip() or tmux.stderr.strip() not in ("no sessions", "no current target"):
                raise HubiError("Remove all v6 sessions/clients before install/uninstall; inventory unavailable or nonempty")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--as-hubi", action="store_true", help="Install hubi alias, preserving the previous binary for rollback")
    parser.add_argument("--allow-dirty", action="store_true", help="Explicitly allow a development bundle (never cutover)")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Run as the target Unix user, never sudo/root")
    bundle = Path(__file__).resolve().parent
    sys.path.insert(0, str(bundle))
    from hubi6.config import Config, private_dir
    from hubi6.core.model import HubiError
    from hubi6.storage import atomic_json, private_json
    from hubi6.adapters.git import Git
    from hubi6.adapters.process import Runner
    c = Config.load()
    prefix = c.home/".local/lib/hubi6"
    bin_dir = c.home/".local/bin"
    registry = c.home/".local/share/hubi6"
    manifest = registry/"install.json"
    config = c.home/".config/hubi6"
    units = c.home/".config/systemd/user"
    zero_state(c)
    if args.action == "install":
        if run("systemctl", "--user", "is-active", "hubi-tmux.service", check=False).returncode == 0:
            raise HubiError("v5 runtime is active; follow the clean cutover runbook first")
        if prefix == bundle:
            raise HubiError("Install from the build bundle, not the installed directory")
        if not (bundle/"SOURCE.json").is_file():
            raise HubiError("Build a fresh v6 bundle before installing")
        source = json.loads((bundle/"SOURCE.json").read_text())
        if source['dirty'] and not args.allow_dirty:
            raise HubiError("Dirty development bundle; build from clean source or explicitly use --allow-dirty")
        for name, expected in source['files'].items():
            if name == 'tools/build_v6.py':
                continue
            path = bundle/('install.py' if name == 'tools/install_v6.py' else name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise HubiError("Bundle source hash mismatch: "+name)
        previous = private_json(manifest) if manifest.exists() else {}
        if previous.get("uninstalling"):
            raise HubiError("Finish the interrupted uninstall before installing again")
        private_dir(registry, create=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        units.mkdir(parents=True, exist_ok=True)
        run("systemctl", "--user", "stop", "hubi6-tmux.service", check=False)
        if prefix.exists():
            shutil.rmtree(prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, prefix, ignore=shutil.ignore_patterns("__pycache__"))
        prefix.chmod(0o700)
        alias = args.as_hubi or previous.get("as_hubi", False)
        if alias and (bin_dir/"hubi").exists() and not previous.get("as_hubi"):
            shutil.copy2(bin_dir/"hubi", registry/"hubi-v5-backup")
        launcher = '#!/usr/bin/env python3\nimport os\nos.execv('+repr(str(prefix/"hubi"))+', ['+repr(str(prefix/"hubi"))+'] + __import__("sys").argv[1:])\n'
        for name in (["hubi6", "hubi"] if alias else ["hubi6"]):
            replace_file(bin_dir/name,launcher.encode(),0o755)
        replace_file(config/"tmux.conf",(bundle/"config/hubi6-tmux.conf").read_bytes(),0o644)
        replace_file(units/"hubi6-tmux.service",(bundle/"systemd/user/hubi6-tmux.service").read_bytes(),0o644)
        atomic_json(manifest, {"as_hubi": alias, "launcher_sha256": hashlib.sha256(launcher.encode()).hexdigest(),
                               "unit_sha256": file_hash(units/"hubi6-tmux.service"),
                               "tmux_sha256": file_hash(config/"tmux.conf"),
                               "backup_sha256": file_hash(registry/"hubi-v5-backup") if (registry/"hubi-v5-backup").exists() else "",
                               "revision": (bundle/"REVISION").read_text().strip()})
        run("systemctl", "--user", "daemon-reload")
        run("systemctl", "--user", "enable", "--now", "hubi6-tmux.service")
        print("Installed v6; run hubi6 doctor (full PASS required before owner smoke)")
    else:
        if not manifest.is_file():
            if any(path.exists() or path.is_symlink() for path in
                   (prefix, bin_dir/"hubi6", units/"hubi6-tmux.service", c.state_base, c.cache_base, c.runtime_base/"hubi6")):
                raise HubiError("No managed v6 installation; refusing unmanaged removal")
            if registry.exists():
                private_dir(registry)
                registry.rmdir()  # An empty registry after a completed cleanup is safe.
            print("V6 is already uninstalled")
            return 0
        private_dir(registry)
        data = private_json(manifest)
        data["uninstalling"] = True
        atomic_json(manifest, data)  # Retain ownership receipts until ALL cleanup succeeds.
        run("systemctl", "--user", "disable", "--now", "hubi6-tmux.service", check=False)
        from hubi6.adapters.systemd import Systemd
        stopped = Systemd(c, Runner()).unit(c.service)
        if stopped.active not in ("inactive", "failed") or stopped.pid:
            raise HubiError("Cannot verify v6 service stopped; uninstall can be retried")
        remove_owned(bin_dir/"hubi6", data["launcher_sha256"])
        if data["as_hubi"]:
            path, backup = bin_dir/"hubi", registry/"hubi-v5-backup"
            # Keep the backup until final cleanup. A retry after restoration
            # recognizes its content instead of removing or overwriting it.
            expected_backup = data.get("backup_sha256", "")
            if backup.exists() or backup.is_symlink():
                if expected_backup and file_hash(backup) != expected_backup:
                    raise HubiError("Fallback backup changed; preserving installation registry")
                expected_backup = file_hash(backup)
                data["backup_sha256"] = expected_backup
                atomic_json(manifest, data)
            try:
                current = file_hash(path)
            except FileNotFoundError:
                current = ""
            if current and current not in (data["launcher_sha256"], expected_backup):
                raise HubiError("hubi launcher changed; refusing to overwrite it with fallback")
            if expected_backup and not backup.exists() and current != expected_backup:
                raise HubiError("Fallback backup is missing; preserving the current launcher and registry")
            if current != expected_backup and backup.exists():
                replace_file(path, backup.read_bytes(), stat.S_IMODE(backup.stat().st_mode))
            elif current == data["launcher_sha256"]:
                path.unlink()
        # Already removed files and already disabled/absent units are normal on retry.
        remove_owned(units/"hubi6-tmux.service", data["unit_sha256"] if "unit_sha256" in data else file_hash(bundle/"systemd/user/hubi6-tmux.service"))
        run("systemctl", "--user", "daemon-reload")
        refs, issues = Git(c, Runner()).discover()
        for ref in refs:
            marker = Path(ref.git_dir)/"hubi6-workspace-id"
            if marker.is_file() and not marker.is_symlink() and marker.stat().st_uid == os.getuid() and re.fullmatch(r"[a-f0-9]{32}\n?", marker.read_text()):
                marker.unlink()
        # All v6 scopes/sessions have already been proven absent. Removing these
        # private namespaces also removes unused lock/temp files without lock races.
        for path in (prefix, config, c.state_base, c.cache_base, c.runtime_base/"hubi6"):
            if path.exists() or path.is_symlink():
                private_dir(path)
                if path == config:
                    remove_owned(config/"tmux.conf", data["tmux_sha256"] if "tmux_sha256" in data else file_hash(bundle/"config/hubi6-tmux.conf"))
                shutil.rmtree(path)
        # Remove our receipt last. Unknown registry entries are preserved.
        backup = registry/"hubi-v5-backup"
        if set(registry.iterdir())-{manifest, backup}:
            raise HubiError("Unknown installation registry files; preserving them for manual inspection")
        remove_owned(backup, data.get("backup_sha256", ""))
        manifest.unlink()
        registry.rmdir()
        print("Uninstalled v6; no workloads restored. Re-enable v5 explicitly for rollback.")
        if issues:
            print("Some repos were unreadable; workspace UUIDs there were preserved.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print("Hubi v6 install: "+str(e), file=sys.stderr)
        raise SystemExit(2)
