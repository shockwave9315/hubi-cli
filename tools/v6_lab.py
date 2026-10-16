#!/usr/bin/env python3
"""Root-only, disposable UID harness. Does not operate on the caller's user manager.

sudo python3 tools/v6_lab.py create|sync|run|cleanup
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys

USER = "hubi6test"
HOME = Path("/home/hubi6test")
MARKER = HOME / ".hubi6-disposable-lab"
SOURCE = Path(__file__).resolve().parent.parent


def run(argv, **kw):
    kw.setdefault("check", True)
    return subprocess.run([str(x) for x in argv], **kw)


def identity():
    entry = pwd.getpwnam(USER)
    assert entry.pw_uid != 0 and entry.pw_uid != 1000
    assert entry.pw_dir == str(HOME) and MARKER.read_text().strip() == "hubi6-only"
    return entry


def as_user(*argv, **kw):
    uid = identity().pw_uid
    return run(["runuser", "-u", USER, "--", "env", "-i", f"HOME={HOME}", f"USER={USER}",
                "PATH=/usr/local/bin:/usr/bin:/bin", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", *argv], **kw)


def sync():
    entry = identity()
    project = HOME / "project"
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(SOURCE, project, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
    for p in [project, *project.rglob("*")]:
        os.chown(p, entry.pw_uid, entry.pw_gid, follow_symlinks=False)


def create():
    try:
        pwd.getpwnam(USER)
    except KeyError:
        assert not HOME.exists()
        run(["useradd", "--create-home", "--shell", "/bin/bash", USER])
        MARKER.write_text("hubi6-only\n")
        MARKER.chmod(0o644)   # readable by the lab UID even under a private umask (v6 workloads run with 0077)
    entry = identity()
    run(["loginctl", "enable-linger", USER])
    run(["systemctl", "start", f"user@{entry.pw_uid}.service"])
    run(["systemctl", "set-property", "--runtime", f"user-{entry.pw_uid}.slice",
         "TasksMax=4096", "MemoryMax=2G", "CPUWeight=40"])
    sync()
    setup_ssh(entry)
    print(json.dumps({"user": USER, "uid": entry.pw_uid, "home": str(HOME)}))


def setup_ssh(entry):
    """An independent loopback-only sshd, permitting ONLY the marked lab UID."""
    settings = HOME/".hubi6-ssh.json"
    unit = f"hubi6-lab-{entry.pw_uid}-sshd.service"
    if settings.exists():
        return
    key = HOME/"test-login-key"
    host = HOME/"test-host-key"
    for p in (key, host):
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", p])
    os.chown(key, entry.pw_uid, entry.pw_gid)
    auth = HOME/"test-authorized-keys"
    shutil.copy(str(key)+".pub", auth)
    auth.chmod(0o600)
    os.chown(auth, entry.pw_uid, entry.pw_gid)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    conf = HOME/"test-sshd.conf"
    conf.write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {host}\nPidFile {HOME}/test-sshd.pid\n"
                    f"AllowUsers {USER}\nPermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                    f"PubkeyAuthentication yes\nAuthorizedKeysFile {auth}\nUsePAM yes\nPrintMotd no\n")
    run(["/usr/sbin/sshd", "-t", "-f", conf])
    run(["systemd-run", "--quiet", "--collect", "--unit="+unit,
         "--description=Hubi6 disposable SSH lab", "--property=KillMode=control-group",
         "--property=MemoryMax=256M", "--property=TasksMax=64", "--", "/usr/sbin/sshd", "-D", "-e", "-f", conf])
    settings.write_text(json.dumps({"port": port, "key": str(key), "unit": unit}))
    settings.chmod(0o644)


def cleanup():
    entry = identity()
    verification = as_user("systemctl", "--user", "list-units", "--all", "--no-legend", "hubi6-*.scope",
                           capture_output=True, text=True, check=False)
    print("Pre-cleanup workload scopes: "+(verification.stdout.strip() or "none"), flush=True)
    socket_path = f"/run/user/{entry.pw_uid}/hubi6/tmux.sock"
    counts = {'scopes': len(verification.stdout.splitlines())}
    for kind in ('sessions', 'clients'):
        result = as_user("tmux", "-N", "-S", socket_path, "list-"+kind,
                         capture_output=True, text=True, check=False)
        counts[kind] = len(result.stdout.splitlines())
    result = as_user('systemctl', '--user', 'list-units', '--all', '--no-legend', 'hubi6-*-clone-*.service',
                     capture_output=True, text=True, check=False)
    counts['clone_units'] = len(result.stdout.splitlines())
    print('PRE-TEARDOWN: '+json.dumps(counts), flush=True)
    if (HOME/".hubi6-ssh.json").exists():
        run(["systemctl", "stop", f"hubi6-lab-{entry.pw_uid}-sshd.service"])
    # Stop ONLY the manager for the marked disposable UID. Kernel enforces separation.
    as_user("systemctl", "--user", "stop", "hubi6-tmux.service", check=False)
    socket_gone = not Path(socket_path).exists()
    run(["loginctl", "disable-linger", USER])
    run(["systemctl", "stop", f"user@{entry.pw_uid}.service"])
    run(["loginctl", "terminate-user", USER], check=False)
    run(["systemctl", "revert", f"user-{entry.pw_uid}.slice"])
    processes = subprocess.run(
        ["ps", "-u", str(entry.pw_uid), "-o", "pid="], capture_output=True, text=True)
    assert not processes.stdout.strip(), processes.stdout
    for base in (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm")):
        for p in base.iterdir():
            if p.lstat().st_uid == entry.pw_uid:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
    run(["userdel", "--remove", USER])
    assert not HOME.exists() and not Path(f"/run/user/{entry.pw_uid}").exists()
    print(f"CLEAN: uid {entry.pw_uid}; no processes, home, runtime, linger or slice overrides")
    assert socket_gone, 'Test socket survived service stop (lab subsequently removed)'
    assert not any(counts.values()), 'Lab was removed, but test left runtime objects before teardown'


def copy_real_agents():
    """Copy executable artifacts only, never the owner's auth/config directories."""
    entry = identity()
    sources = {
        Path('/home/dev/.local/node-v22.23.2/bin/node'): HOME/'real-node',
        Path('/home/dev/.local/share/claude/versions/2.1.296'): HOME/'real-claude',
        Path('/home/dev/.npm-global/lib/node_modules/@openai/codex'): HOME/'real-codex-package',
    }
    for source, destination in sources.items():
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        for path in [destination, *destination.rglob('*')] if destination.is_dir() else [destination]:
            os.chown(path, entry.pw_uid, entry.pw_gid, follow_symlinks=False)
    wrapper = HOME/'real-codex'
    wrapper.write_text('#!/bin/sh\nexec '+str(HOME/'real-node')+' '+str(HOME/'real-codex-package/bin/codex.js')+' "$@"\n')
    wrapper.chmod(0o755)
    os.chown(wrapper, entry.pw_uid, entry.pw_gid)


def main():
    assert os.getuid() == 0, "Use sudo for the isolated lab supervisor only"
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "sync", "run", "cleanup"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "create":
        create()
    elif args.action == "sync":
        sync()
    elif args.action == "cleanup":
        cleanup()
    else:
        # Tests themselves run as the disposable user, beneath its user manager.
        as_user("systemd-run", "--user", "--wait", "--pipe", "--collect",
                "--unit=hubi6-test-driver.service", "--property=WorkingDirectory="+str(HOME/"project"),
                "--", "env", "HUBI6_LAB=1", *args.args)


if __name__ == "__main__":
    main()
