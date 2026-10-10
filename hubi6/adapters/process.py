import logging
import os
import subprocess
import time

from ..core.model import HubiError


class Runner:
    def __init__(self):
        self.calls = 0
        self.elapsed = 0.0

    def run(self, argv, *, env=None, input=None, timeout=10, check=True, cwd=None, interactive=False):
        self.calls += 1
        started = time.monotonic()
        clean = dict(os.environ if env is None else env)
        for k in ("TMUX", "TMUX_PANE", "HUBI_ACTIVE", "HUBI6_ACTIVE"):
            clean.pop(k, None)
        clean.update({"LC_ALL": "C.UTF-8", "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": ""})
        clean.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        clean.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={clean['XDG_RUNTIME_DIR']}/bus")
        try:
            child = subprocess.Popen([str(x) for x in argv], env=clean, cwd=cwd,
                                     encoding="utf-8", errors="surrogateescape", close_fds=True,
                                     stdin=subprocess.PIPE if input is not None else None,
                                     stdout=None if interactive else subprocess.PIPE,
                                     stderr=None if interactive else subprocess.PIPE)
            try:
                stdout, stderr = child.communicate(input, timeout=None if interactive else timeout)
            except BaseException:
                # Only the exact helper/client created here. Workload signals go
                # exclusively through Systemd.kill's verified scope boundary.
                child.kill()
                # A systemd --pipe service can still hold these descriptors.
                # Waiting for EOF here would postpone the caller's cgroup cleanup.
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
                child.wait()
                raise
            result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HubiError(f"{os.path.basename(str(argv[0]))}: {type(e).__name__}") from e
        finally:
            elapsed = time.monotonic() - started
            self.elapsed += elapsed
            # Never log argv, stdin, environment or command output.
            logging.getLogger("hubi6").debug("subprocess executable=%s elapsed_ms=%.2f", os.path.basename(str(argv[0])), elapsed*1000)
        if check and result.returncode:
            raise HubiError(f"{os.path.basename(str(argv[0]))} failed (exit {result.returncode})")
        return result
