#!/usr/bin/env python3
"""Send a signal to Hubi while its menu is blocked on input."""

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: signal_runner.py HUBI SIGNAL EXPECTED_RC", file=sys.stderr)
        return 2

    hubi, signal_name, expected_text = sys.argv[1:]
    expected = int(expected_text)
    signum = getattr(signal, f"SIG{signal_name}")
    process = subprocess.Popen(
        [hubi],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )
    time.sleep(0.25)
    process.send_signal(signum)
    output, _ = process.communicate(timeout=3)
    text = output.decode("utf-8", errors="replace")
    if process.returncode != expected or f"odebrano {signal_name}" not in text:
        print(text, file=sys.stderr)
        print(
            f"expected rc={expected} and signal diagnostic; got rc={process.returncode}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
