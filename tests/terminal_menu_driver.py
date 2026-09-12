#!/usr/bin/env python3
import os
import pty
import select
import sys
import time


def read_until(fd: int, expected: bytes, output: bytearray, timeout: float = 8.0) -> None:
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
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(sys.argv[1], sys.argv[1:])

    output = bytearray()
    try:
        read_until(fd, "TERMINALE".encode(), output)
        os.write(fd, b"1\n")
        read_until(fd, "TERMINAL — aaa-orphan".encode(), output)
        read_until(fd, "[x] Zakończ terminal".encode(), output)
        os.write(fd, b"x\n")
        read_until(fd, "Zakończyć terminal".encode(), output)
        os.write(fd, b"y\n")
        # Successful cleanup returns to the terminal list with the orphan gone.
        first_list_end = len(output)
        read_until(fd, "TERMINALE".encode(), output[first_list_end:])
        os.write(fd, b"b\n")
        _, status = os.waitpid(pid, 0)
        sys.stdout.buffer.write(output)
        return os.waitstatus_to_exitcode(status)
    except Exception as exc:
        print(f"terminal menu driver: {exc}", file=sys.stderr)
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
