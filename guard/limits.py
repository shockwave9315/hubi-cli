#!/usr/bin/env python3
"""Read-only Codex rate-limit probe for watchdog.sh.

Starts `codex app-server` over stdio, sends only `initialize`, `initialized`
and `account/rateLimits/read`, then exits. It never starts a turn and never
calls any method that consumes credits or reset credits.

Output (one line, key=value, for Bash):
  status=ok five_left=<0-100|unknown> five_reset=<epoch|unknown>
            week_left=<0-100|unknown> week_reset=<epoch|unknown>
            allowed=<true|false|unknown>
  status=error reason=<short reason>
`--parse FILE` parses a saved JSON-RPC result (for tests) without starting Codex.
"""
import json
import os
import select
import signal
import subprocess
import sys
import time

ALLOWED_METHODS = {"initialize", "account/rateLimits/read"}
FIVE_H, WEEK = 300, 10080
TIMEOUT = float(os.environ.get("WD_LIMITS_TIMEOUT", "20"))


def window_left(snapshot, minutes):
    """Remaining % and reset epoch of the window with this duration, or unknown."""
    found = []
    for key in ("primary", "secondary"):
        w = snapshot.get(key) if isinstance(snapshot, dict) else None
        if isinstance(w, dict) and w.get("windowDurationMins") == minutes:
            found.append(w)
    if len(found) != 1:  # missing or ambiguous: never guess
        return "unknown", "unknown"
    w = found[0]
    used = w.get("usedPercent")
    if not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= 100:
        return "unknown", "unknown"
    reset = w.get("resetsAt")
    reset = str(reset) if isinstance(reset, int) and not isinstance(reset, bool) and reset > 0 else "unknown"
    return str(100 - used), reset


def summarize(result):
    if not isinstance(result, dict):
        return "status=error reason=bad-result"
    by_id = result.get("rateLimitsByLimitId")
    snapshot = by_id.get("codex") if isinstance(by_id, dict) else None
    if snapshot is None:
        snapshot = result.get("rateLimits")
        if isinstance(snapshot, dict) and snapshot.get("limitId") not in (None, "codex"):
            snapshot = None
    if not isinstance(snapshot, dict):
        return "status=error reason=no-codex-limit"
    five, five_reset = window_left(snapshot, FIVE_H)
    week, week_reset = window_left(snapshot, WEEK)
    allowed = result.get("ordinaryUsageAllowed")
    allowed = str(allowed).lower() if isinstance(allowed, bool) else "unknown"
    return (f"status=ok five_left={five} five_reset={five_reset} "
            f"week_left={week} week_reset={week_reset} allowed={allowed}")


def rpc(proc, msg):
    if "id" in msg:
        assert msg["method"] in ALLOWED_METHODS, msg["method"]
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def read_response(proc, want_id, deadline):
    buf = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            return None, "eof"
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want_id:
                if "error" in msg:
                    return None, "rpc-error"
                return msg.get("result"), None
    return None, "timeout"


def stop_group(proc):
    """TERM, then KILL, for the whole process group; reap the direct child."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        # The direct child is gone; make sure no descendant survives in the group.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return


def probe():
    try:
        # Własna grupa procesów: `codex` to wrapper Node z natywnym potomkiem.
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return "status=error reason=spawn"
    try:
        deadline = time.monotonic() + TIMEOUT
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"clientInfo": {"name": "praca-watchdog", "version": "1"}}})
        _, err = read_response(proc, 1, deadline)
        if err:
            return f"status=error reason=init-{err}"
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"initialized"}\n')
        proc.stdin.flush()
        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read",
                   "params": {"excludeResetCreditDetails": True}})
        result, err = read_response(proc, 2, deadline)
        if err:
            return f"status=error reason=read-{err}"
        return summarize(result)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        stop_group(proc)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--parse":
        try:
            with open(sys.argv[2]) as f:
                print(summarize(json.load(f)))
        except (OSError, ValueError):
            print("status=error reason=bad-json")
    else:
        print(probe())
