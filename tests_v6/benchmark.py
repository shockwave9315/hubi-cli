"""Reproducible real snapshot benchmark, run only in the disposable UID."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import statistics
import time

from hubi6.services import App
from tests_v6.support import bootstrap, repo
from tests_v6.test_runtime import Client, wait_for


def measure(app, count=9):
    samples = [app.snapshot() for _ in range(count)]
    assert all(s.subprocesses == 2 for s in samples)
    return {"median_ms": statistics.median(s.elapsed_ms for s in samples),
            "p95_ms": sorted(s.elapsed_ms for s in samples)[-1], "subprocesses": 2,
            "repos": len(samples[-1].repos), "workloads": len(samples[-1].workloads)}


def main():
    app = bootstrap()
    results = []
    # Use a separate repo root, but the same runtime inventory identity, so the
    # benchmark covers real tmux and systemd plus filesystem scaling independently.
    root = Path.home()/"benchmark-repos"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(exist_ok=True)
    fixture = repo("benchmark-template")
    measured = App(replace(app.c, repos=root))
    for target in (7, 200, 1000):
        for i in range(len(list(root.iterdir())), target):
            (root/f"repo-{i:04}").mkdir()
            shutil.copytree(fixture/".git", root/f"repo-{i:04}/.git")
        results.append(measure(measured))
        if target == 7:
            records = [measured.start("codex" if i % 2 == 0 else "claude", str(root/f"repo-{i:04}")) for i in range(6)]
            client = Client(["attach", records[0].id, "--mode", "shared"])
            try:
                wait_for(lambda: measured.find(records[0].id).session.attached == 1)
                results.append(measure(measured))
            finally:
                client.close()
                for r in records:
                    measured.stop(r.id)
    records = []
    try:
        for i in range(50):
            records.append(app.start("codex", str(fixture), f"bench{i}"))
        results.append(measure(measured))
    finally:
        for r in records:
            app.stop(r.id)
    print(json.dumps({"snapshots": results}, indent=2))


if __name__ == "__main__":
    main()
