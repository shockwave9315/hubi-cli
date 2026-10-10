import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import sys

from . import __version__
from .config import Config
from .core.model import HubiError, display


def parser():
    p = argparse.ArgumentParser(prog="hubi", description="Hubi v6 systemd/tmux workloads")
    p.add_argument("--version", action="version", version="Hubi "+__version__)
    p.add_argument("--config")
    p.add_argument("--debug", action="store_true")
    sub = p.add_subparsers(dest="command")
    for name in ("doctor", "list"):
        c = sub.add_parser(name)
        c.add_argument("--json", action="store_true")
    c = sub.add_parser("start")
    c.add_argument("type", choices=("codex", "claude", "terminal"))
    c.add_argument("repo")
    c.add_argument("--instance", default="primary")
    c.add_argument("--resume", action="store_true")
    c.add_argument("--json", action="store_true")
    for name in ("stop", "inspect", "attach", "reconcile"):
        c = sub.add_parser(name)
        c.add_argument("id")
        if name in ("stop", "reconcile"):
            c.add_argument("--generation", help="Reject a stale confirmation")
        if name == "attach":
            c.add_argument("--mode", choices=("normal", "view", "shared", "takeover"), default="normal")
    c = sub.add_parser("github")
    g = c.add_subparsers(dest="github_command", required=True)
    ls = g.add_parser("list")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--refresh", action="store_true")
    ls.add_argument("--offline", action="store_true")
    clone = g.add_parser("clone")
    clone.add_argument("repo")
    return p


def execute(args):
    from .services import App
    app = App(Config.load(args.config))
    if args.debug:
        handler = logging.StreamHandler()
        logger = logging.getLogger("hubi6")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    # Read-only commands never create state, config, logs, locks or runtime dirs.
    if args.command not in ("list", "doctor", "inspect"):
        app.c.prepare()
        handler = RotatingFileHandler(app.c.state_base/"hubi6.log", maxBytes=1048576, backupCount=3)
        logging.getLogger("hubi6").addHandler(handler)
    if args.command is None:
        from .tui import run
        return run(app)
    if args.command == "doctor":
        from .doctor import doctor
        result = doctor(app)
        print(json.dumps(result, ensure_ascii=True, indent=2) if args.json else
              "Doctor: "+("PASS" if result["ready"] else "FAIL")+"\n"+
              "\n".join(f"{k}: {display(v)}" for k, v in result["checks"].items()))
        return 0 if result["ready"] else 1
    if args.command == "list":
        s = app.snapshot()
        if args.json:
            print(json.dumps(s.data(), ensure_ascii=True, indent=2))
        else:
            print("Runtime: "+s.runtime_health)
            for w in s.workloads:
                print(f"{w.record.id}  {w.record.kind}/{w.record.instance}  {w.state.value}  repo:{w.repo_health.value}  "
                      f"clients:{w.session.attached if w.session else 0}  {display(w.record.repo.path)}")
            for issue in s.issues:
                print(display(issue), file=sys.stderr)
        return 0
    if args.command == "start":
        r = app.start(args.type, args.repo, args.instance, args.resume)
        print(json.dumps(r.data(), ensure_ascii=True) if args.json else r.id)
    elif args.command in ("stop", "reconcile"):
        app.stop(args.id, args.generation, repair=args.command == "reconcile")
    elif args.command == "inspect":
        print(json.dumps(app.find(args.id).data(), ensure_ascii=True, indent=2))
    elif args.command == "attach":
        return app.attach(args.id, args.mode)
    elif args.command == "github":
        from .github import GitHub
        gh = GitHub(app)
        if args.github_command == "clone":
            print(gh.clone(args.repo).path)
        else:
            result = gh.list(args.refresh, args.offline)
            view = gh.view(result)
            if args.json:
                print(json.dumps({"repos": view, "cached": result.cached, "stale": result.stale, "error": result.error}, ensure_ascii=True, indent=2))
            else:
                for repo in view:
                    print(f"{'LOCAL' if repo['local'] else 'NOT LOCAL':9} {repo['nameWithOwner']}"
                          f"{' private' if repo['isPrivate'] else ''}{' fork' if repo['isFork'] else ''}{' archived' if repo['isArchived'] else ''}")
                if result.error:
                    print(result.error, file=sys.stderr)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    from .tui import SignalExit, terminal
    try:
        # Also restore the terminal around noninteractive lifecycle/attach commands
        # invoked from a TTY. Signal exits unwind clone staging and terminal state.
        with terminal():
            return execute(args)
    except SignalExit as e:
        return e.code
    except KeyboardInterrupt:
        return 130
    except HubiError as e:
        print("Hubi: "+display(e), file=sys.stderr)
        return 2
    except Exception as e:
        logging.getLogger("hubi6").error("Unhandled exception type=%s", type(e).__name__)
        print(f"Hubi: {type(e).__name__}; operation failed. Run doctor/inspect.", file=sys.stderr)
        return 1
