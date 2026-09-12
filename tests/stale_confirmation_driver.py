#!/usr/bin/env python3
import os
import pty
import select
import sys
import time
from pathlib import Path


def read_until(fd: int, expected: bytes, output: bytearray, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while expected not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"did not see {expected!r}")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            raise RuntimeError(f"child exited before {expected!r}")
        output.extend(chunk)


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    ready = Path(os.environ["HUBI_TEST_CONFIRM_READY"])
    release = Path(os.environ["HUBI_TEST_CONFIRM_RELEASE"])
    prompt = os.environ["HUBI_TEST_CONFIRM_PROMPT"].encode()
    refusal = os.environ["HUBI_TEST_CONFIRM_REFUSAL"].encode()

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(sys.argv[1], sys.argv[1:])

    output = bytearray()
    try:
        read_until(fd, prompt, output)
        ready.touch()
        deadline = time.monotonic() + 15.0
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("replacement did not release confirmation barrier")
            time.sleep(0.01)
        os.write(fd, b"y\n")
        read_until(fd, refusal, output)
        read_until(fd, "Enter, aby kontynuować".encode(), output)
        os.write(fd, b"\n")
        os.waitpid(pid, 0)
        sys.stdout.buffer.write(output)
        return 0
    except Exception as exc:
        print(f"stale confirmation driver: {exc}", file=sys.stderr)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        sys.stdout.buffer.write(output)
        return 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
