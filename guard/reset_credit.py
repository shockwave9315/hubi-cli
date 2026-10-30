#!/usr/bin/env python3
"""Consume ONE Codex rate-limit reset credit (owner-approved, v3.2).

Called only by watchdog.sh, and only while the owner's consent file
`reset.armed` exists. Usage: reset_credit.py <idempotency-uuid>

Sends `initialize`, `initialized`, `account/rateLimits/read` (to pick the
available credit that expires first), `account/rateLimitResetCredit/consume`
with the given idempotency key (a retry with the same key never spends a
second credit), then reads the limits again.

Output (one line, key=value, for Bash):
  status=ok outcome=<reset|nothingToReset|noCredit|alreadyRedeemed|unknown>
            credits_before=<n|unknown> five_left=<..> week_left=<..>
  status=error reason=<short reason>
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limits  # noqa: E402  (read_response, stop_group, summarize)

ALLOWED_METHODS = {"initialize", "account/rateLimits/read",
                   "account/rateLimitResetCredit/consume"}
OUTCOMES = {"reset", "nothingToReset", "noCredit", "alreadyRedeemed"}
TIMEOUT = float(os.environ.get("WD_RESET_TIMEOUT", "40"))
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def rpc(proc, msg):
    assert msg["method"] in ALLOWED_METHODS, msg["method"]
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def earliest_credit(result):
    """Id of the available credit that expires first (no expiry = last), or None."""
    summary = result.get("rateLimitResetCredits") if isinstance(result, dict) else None
    credits = summary.get("credits") if isinstance(summary, dict) else None
    if not isinstance(credits, list):
        return None, summary.get("availableCount") if isinstance(summary, dict) else None
    usable = [c for c in credits if isinstance(c, dict) and c.get("status") == "available"
              and isinstance(c.get("id"), str) and c.get("id")]
    usable.sort(key=lambda c: c.get("expiresAt") if isinstance(c.get("expiresAt"), int) else 2**62)
    count = summary.get("availableCount")
    return (usable[0]["id"] if usable else None), count


def consume(key):
    try:
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return "status=error reason=spawn"
    try:
        deadline = time.monotonic() + TIMEOUT
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"clientInfo": {"name": "praca-watchdog", "version": "1"}}})
        _, err = limits.read_response(proc, 1, deadline)
        if err:
            return f"status=error reason=init-{err}"
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"initialized"}\n')
        proc.stdin.flush()
        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        before, err = limits.read_response(proc, 2, deadline)
        if err:
            return f"status=error reason=read-{err}"
        credit, count = earliest_credit(before)
        count = count if isinstance(count, int) and not isinstance(count, bool) else "unknown"
        params = {"idempotencyKey": key}
        if credit:
            params["creditId"] = credit
        rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "account/rateLimitResetCredit/consume",
                   "params": params})
        res, err = limits.read_response(proc, 3, deadline)
        if err:
            return f"status=error reason=consume-{err}"
        outcome = res.get("outcome") if isinstance(res, dict) else None
        outcome = outcome if outcome in OUTCOMES else "unknown"
        rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "account/rateLimits/read",
                   "params": {"excludeResetCreditDetails": True}})
        after, err = limits.read_response(proc, 4, deadline)
        tail = "five_left=unknown week_left=unknown"
        if not err:
            s = limits.summarize(after)
            m5 = re.search(r"five_left=(\S+)", s)
            mw = re.search(r"week_left=(\S+)", s)
            tail = f"five_left={m5.group(1) if m5 else 'unknown'} week_left={mw.group(1) if mw else 'unknown'}"
        return f"status=ok outcome={outcome} credits_before={count} {tail}"
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        limits.stop_group(proc)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not UUID.match(sys.argv[1]):
        print("status=error reason=bad-key")
        sys.exit(0)
    print(consume(sys.argv[1]))
