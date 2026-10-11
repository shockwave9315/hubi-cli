"""Optional smoke of copied real binaries; no owner credentials or API calls."""
from dataclasses import replace
import json
from pathlib import Path
import time

from hubi6.services import App
from hubi6.core.model import HubiError
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
            result = {"type": kind, "version": version, "state": state, "output": output.strip(),
                      "authenticated": False, "scope": r.scope}
            results.append(result)
        finally:
            started = time.monotonic()
            real.stop(r.id)
            result["stop_ms"] = (time.monotonic()-started)*1000
        # Resume without credentials/history may show onboarding or report no
        # sessions. Exercise the real command safely; never claim inference or
        # conversation-state persistence from this unauthenticated smoke.
        try:
            resumed = real.start(kind, str(path), resume=True)
        except HubiError as e:
            result["resume"] = "No usable authenticated conversation: "+str(e)
            for w in real.runtime_snapshot().workloads:
                result["resume_output"] = real.tmux.command("capture-pane", "-p", "-S", "-", "-t", w.record.pane).stdout.strip()
                real.stop(w.record.id, repair=True)
        else:
            time.sleep(.4)
            result["resume"] = real.find(resumed.id).state.value
            result["resume_output"] = real.tmux.command("capture-pane", "-p", "-S", "-", "-t", resumed.pane).stdout.strip()
            real.stop(resumed.id)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
