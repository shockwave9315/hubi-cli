#!/usr/bin/env python3
"""Complete disposable-UID suite with unconditional teardown."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v6_lab

try:
    v6_lab.create()
    v6_lab.as_user("systemd-run", "--user", "--wait", "--pipe", "--collect", "--unit=hubi6-test-driver.service",
                    "--property=WorkingDirectory="+str(v6_lab.HOME/"project"), "--", "env", "HUBI6_LAB=1",
                    "/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests_v6", "-p", "test_*.py", "-v")
    v6_lab.as_user("systemd-run", "--user", "--wait", "--pipe", "--collect", "--unit=hubi6-benchmark.service",
                    "--property=WorkingDirectory="+str(v6_lab.HOME/"project"), "--", "env", "HUBI6_LAB=1",
                    "/usr/bin/python3", "-m", "tests_v6.benchmark")
finally:
    if v6_lab.MARKER.exists():
        v6_lab.cleanup()
