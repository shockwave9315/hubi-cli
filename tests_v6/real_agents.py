"""Optional smoke of copied real binaries; no owner credentials or API calls."""
from dataclasses import replace
import json
from pathlib import Path
import time

from hubi6.services import App
from tests_v6.support import bootstrap, repo


def main():
    app = bootstrap()
    path = repo("real-agent-smoke")
    results = []
    for kind in ("codex", "claude"):
        executable = Path.home()/("real-"+kind)
        if not executable.exists():
            raise RuntimeError("Copy a real binary into the isolated HOME first")
        version = app.r.run([str(executable), "--version"]).stdout.strip()
        argv = [str(executable)]
        if kind == "codex" and (Path.home()/"real-codex-package").exists():
            argv = [str(Path.home()/"real-node"), str(Path.home()/"real-codex-package/bin/codex.js")]
            version = app.r.run([*argv, "--version"]).stdout.strip()
        if kind == "claude":
            # Same v5 argument policy; root restrictions don't apply to this UID.
            argv += ["--permission-mode", "bypassPermissions"]
        real = App(replace(app.c, agents=dict(app.c.agents, **{kind: argv})))
        r = real.start(kind, str(path))
        try:
            time.sleep(1)
            state = real.find(r.id).state.value
            output = real.tmux.command("capture-pane", "-p", "-S", "-", "-t", r.pane).stdout
            if state != "RUNNING" or not output.strip():
                raise AssertionError("Real executable did not reach a visible CLI state")
            results.append({"type": kind, "version": version, "state": state, "output": output.strip(),
                            "authenticated": False, "scope": r.scope})
        finally:
            real.stop(r.id)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
