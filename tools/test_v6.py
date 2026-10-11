#!/usr/bin/env python3
"""Complete disposable-UID suite with unconditional teardown."""
import sys
import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v6_lab

parser = argparse.ArgumentParser()
parser.add_argument('--require-clean-build', action='store_true')
parser.add_argument('--real-agents', action='store_true')
args = parser.parse_args()
if args.require_clean_build:
    assert not json.loads((v6_lab.SOURCE/'dist/hubi6/SOURCE.json').read_text())['dirty']

try:
    v6_lab.create()
    v6_lab.as_user("systemd-run", "--user", "--wait", "--pipe", "--collect", "--unit=hubi6-test-driver.service",
                    "--property=WorkingDirectory="+str(v6_lab.HOME/"project"), "--", "env", "HUBI6_LAB=1",
                    "HUBI6_REQUIRE_CLEAN_BUILD="+('1' if args.require_clean_build else '0'),
                    "/usr/bin/python3", "-m", "unittest", "discover", "-s", "tests_v6", "-p", "test_*.py", "-v")
    v6_lab.as_user("systemd-run", "--user", "--wait", "--pipe", "--collect", "--unit=hubi6-benchmark.service",
                    "--property=WorkingDirectory="+str(v6_lab.HOME/"project"), "--", "env", "HUBI6_LAB=1",
                    "/usr/bin/python3", "-m", "tests_v6.benchmark")
    if args.real_agents:
        v6_lab.copy_real_agents()
        v6_lab.as_user("systemd-run", "--user", "--wait", "--pipe", "--collect", "--unit=hubi6-real-agents.service",
                      "--property=WorkingDirectory="+str(v6_lab.HOME/"project"), "--", "env", "HUBI6_LAB=1",
                      "/usr/bin/python3", "-m", "tests_v6.real_agents")
finally:
    if v6_lab.MARKER.exists():
        v6_lab.cleanup()
