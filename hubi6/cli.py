import argparse
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import traceback

from . import __version__
from .config import Config
from .core.model import HubiError, State, Unavailable, display
from .i18n import DEFAULT_INSTANCE, DEFAULT_NAME, HEALTH_PL, RUNTIME_PL, STATE_PL, instance_of, name_pl, pl


class PolishHelp(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        # argparse passes prefix="" when it only wants the program name of a subcommand.
        return super().add_usage(usage, actions, groups, "użycie: " if prefix is None else prefix)


def polish(**kw):
    """A parser whose usage line, section titles and -h text are Polish."""
    p = argparse.ArgumentParser(formatter_class=PolishHelp, add_help=False, **kw)
    p._positionals.title, p._optionals.title = "argumenty", "opcje"
    p.add_argument("-h", "--help", action="help", help="pokaż tę pomoc")
    return p


def parser():
    p = polish(prog="hubi", description="Hubi v6: agenci i terminale w systemd/tmux. Bez polecenia otwiera menu.")
    p._positionals.title = "polecenia"
    p.add_argument("--version", action="version", version="Hubi "+__version__, help="pokaż wersję")
    p.add_argument("--config", help="inny plik konfiguracji TOML")
    p.add_argument("--debug", action="store_true", help="pełne ślady błędów")
    p.add_argument("--classic", action="store_true", help="klasyczne menu liniowe (awaryjne)")
    sub = p.add_subparsers(dest="command", parser_class=polish)
    as_json = "wynik w formacie JSON"
    for name, text in (("doctor", "diagnostyka środowiska"), ("list", "lista sesji")):
        c = sub.add_parser(name, help=text)
        c.add_argument("--json", action="store_true", help=as_json)
    c = sub.add_parser("start", help="uruchom sesję w tle i wypisz jej ID")
    c.add_argument("type", choices=("codex", "claude", "terminal"), help="rodzaj sesji")
    c.add_argument("repo", help="nazwa albo ścieżka repozytorium; ~ = katalog domowy (bez repo)")
    c.add_argument("--instance", default="primary", metavar="NAZWA", help=f"nazwa sesji (domyślnie: {DEFAULT_NAME})")
    c.add_argument("--resume", action="store_true", help="wznów rozmowę")
    c.add_argument("--json", action="store_true", help=as_json)
    for name in ("codex", "claude", "terminal"):
        if name == "terminal":
            c = sub.add_parser(name, help="uruchom terminal w repozytorium albo w katalogu domowym (jeśli trzeba) i wejdź")
            c.add_argument("repo", nargs="?", help="nazwa albo ścieżka repozytorium; bez niej albo ~ = katalog domowy (bez repo)")
        else:
            # An agent never falls back to ~: with bypassPermissions it would act on
            # the whole home directory. A missing or empty $REPO must be an error.
            c = sub.add_parser(name, help=f"uruchom {name} w repozytorium (jeśli trzeba) i wejdź")
            c.add_argument("repo", help="nazwa albo ścieżka repozytorium; ~ = katalog domowy (bez repo), tylko jawnie")
        c.add_argument("instance", nargs="?", default="primary", metavar="NAZWA",
                       help=f"nazwa sesji (domyślnie: {DEFAULT_NAME})")
        if name != "terminal":
            c.add_argument("--resume", action="store_true", help="wznów rozmowę")
        c.add_argument("--mode", choices=("normal", "view", "shared", "takeover"), default="normal", help="tryb wejścia")
    names = (f"Sesja „{DEFAULT_NAME}” to nazwa domyślna i można ją pominąć (w JSON: name „{DEFAULT_NAME}”, "
             f"instance „{DEFAULT_INSTANCE}”). Inne sesje znajdziesz w `hubi list`.")
    c = sub.add_parser("pane", help="panel tmux działającej sesji (np. %%14), do wysyłania do niej z innych narzędzi",
                       epilog="Kody wyjścia: 0 = wypisano panel; 4 = tej sesji teraz nie ma albo nie działa (ponów później); "
                              "2 = każdy inny błąd, np. nieznane repozytorium. "+names)
    c.add_argument("type", choices=("codex", "claude", "terminal"), help="rodzaj sesji")
    c.add_argument("repo", help="nazwa albo ścieżka repozytorium; ~ = katalog domowy (bez repo)")
    c.add_argument("instance", nargs="?", default="primary", metavar="NAZWA",
                   help=f"nazwa sesji (domyślnie: {DEFAULT_NAME})")
    c.add_argument("--json", action="store_true", help=as_json+", z gniazdem tmux")
    c = sub.add_parser("send", help="wyślij tekst do działającej sesji po nazwie (wklejenie + Enter)",
                       epilog="Kody wyjścia: 0 = wysłano; 4 = tej sesji teraz nie ma, nie działa albo ktoś przewija jej panel, "
                              "nic nie wysłano (ponów później); 2 = każdy inny błąd, np. nieznane repozytorium albo znaki "
                              "sterujące w treści. "+names)
    c.add_argument("type", choices=("codex", "claude", "terminal"), help="rodzaj sesji")
    c.add_argument("repo", help="nazwa albo ścieżka repozytorium; ~ = katalog domowy (bez repo)")
    c.add_argument("instance", nargs="?", default="primary", metavar="NAZWA",
                   help=f"nazwa sesji (domyślnie: {DEFAULT_NAME})")
    c.add_argument("-m", "--message", metavar="TEKST", help="treść; bez tej opcji czytana ze standardowego wejścia")
    c.add_argument("--no-enter", action="store_true", help="tylko wklej, bez naciskania Enter")
    for name, text in (("stop", "zatrzymaj sesję"), ("inspect", "szczegóły sesji (JSON)"), ("attach", "wejdź do sesji"),
                       ("reconcile", "napraw niespójną sesję")):
        c = sub.add_parser(name, help=text)
        c.add_argument("id", help="ID sesji z polecenia list")
        if name in ("stop", "reconcile"):
            c.add_argument("--generation", help="odrzuć nieaktualne potwierdzenie")
        if name == "attach":
            c.add_argument("--mode", choices=("normal", "view", "shared", "takeover"), default="normal", help="tryb wejścia")
    c = sub.add_parser("github", help="repozytoria na GitHubie")
    g = c.add_subparsers(dest="github_command", required=True, parser_class=polish)
    ls = g.add_parser("list", help="lista repozytoriów")
    ls.add_argument("--json", action="store_true", help=as_json)
    ls.add_argument("--refresh", action="store_true", help="pobierz listę na nowo")
    ls.add_argument("--offline", action="store_true", help="tylko pamięć podręczna, bez sieci")
    clone = g.add_parser("clone", help="sklonuj repozytorium do katalogu roboczego")
    clone.add_argument("repo", help="nazwa repozytorium na GitHubie")
    return p


def menu(app, classic):
    """The Polish dashboard, with the line-oriented menu as a safety net.

    Hubi is the SSH login entry point: an unexpected fault in the richer menu
    must degrade to the reviewed fallback instead of leaving no menu at all.
    """
    from .tui import run as classic_menu
    if classic or os.environ.get("HUBI6_UI") == "classic":
        return classic_menu(app)
    from .menu import run as dashboard
    try:
        return dashboard(app)
    except (HubiError, OSError):
        raise                                          # expected errors and a vanished terminal are not faults
    except Exception as e:
        logging.getLogger("hubi6").error("Dashboard failed type=%s; using classic menu", type(e).__name__)
        print(f"Hubi: nowe menu zgłosiło błąd ({type(e).__name__}); uruchamiam menu klasyczne.", file=sys.stderr)
        return classic_menu(app)


def execute(args):
    from .services import App
    app = App(Config.load(args.config))
    if args.debug:
        handler = logging.StreamHandler()
        logger = logging.getLogger("hubi6")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    # Read-only commands never create state, config, logs, locks or runtime dirs.
    # send writes nothing but its per-session lock and needs no log.
    if args.command not in ("list", "doctor", "inspect", "pane", "send"):
        app.c.prepare()
        handler = RotatingFileHandler(app.c.state_base/"hubi6.log", maxBytes=1048576, backupCount=3)
        logging.getLogger("hubi6").addHandler(handler)
    if args.command is None:
        return menu(app, args.classic)
    if args.command == "doctor":
        from .doctor import doctor
        result = doctor(app)
        print(json.dumps(result, ensure_ascii=True, indent=2) if args.json else
              "Diagnostyka: "+result["status"]+"\n"+
              "\n".join(f"{k}: {display(v)}" for k, v in result["checks"].items()))
        return {"PASS": 0, "WARN": 3, "FAIL": 1}[result["status"]]
    if args.command == "list":
        s = app.snapshot()
        if args.json:
            print(json.dumps(s.data(), ensure_ascii=True, indent=2))
        else:
            print("Runtime: "+RUNTIME_PL.get(s.runtime_health, s.runtime_health))
            for w in s.workloads:
                print(f"{w.record.id}  {w.record.kind}/{name_pl(w.record.instance)}  {STATE_PL[w.state]}  repo:{HEALTH_PL[w.repo_health]}  "
                      f"klienci:{w.session.attached if w.session else 0}  {display(w.record.repo.path)}")
            for issue in s.issues:
                print(pl(display(issue)), file=sys.stderr)
        return 0 if s.runtime_health == "OK" and all(w.state != State.UNKNOWN for w in s.workloads) else 1
    if args.command == "start":
        r = app.start(args.type, args.repo, instance_of(args.instance), args.resume)
        print(json.dumps(r.data(), ensure_ascii=True) if args.json else r.id)
    elif args.command in ("codex", "claude", "terminal"):
        return app.open(args.command, args.repo, instance_of(args.instance), getattr(args, "resume", False), args.mode)
    elif args.command in ("stop", "reconcile"):
        app.stop(args.id, args.generation, repair=args.command == "reconcile")
    elif args.command == "inspect":
        print(json.dumps(app.find(args.id).data(), ensure_ascii=True, indent=2))
    elif args.command == "pane":
        w = app.pane(args.type, args.repo, instance_of(args.instance))
        print(json.dumps({"pane": w.session.pane, "socket": str(app.c.socket), "id": w.record.id,
                          "tmux_session": w.record.session, "state": w.state.value}, ensure_ascii=True)
              if args.json else w.session.pane)
    elif args.command == "send":
        if args.message is None:
            if sys.stdin is None or sys.stdin.isatty():
                raise HubiError("Message missing: use -m TEXT or standard input")
            text = sys.stdin.read()
        else:
            text = args.message
        app.send(args.type, args.repo, instance_of(args.instance), text, enter=not args.no_enter)
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
                    print(f"{'lokalne' if repo['local'] else 'zdalne':8} {repo['nameWithOwner']}"
                          f"{' prywatne' if repo['isPrivate'] else ''}{' fork' if repo['isFork'] else ''}{' archiwum' if repo['isArchived'] else ''}")
                if result.error:
                    print(pl(result.error), file=sys.stderr)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    from .tui import SignalExit, terminal
    try:
        if args.command is None:
            try:
                os.fstat(sys.stdin.fileno())
            except (AttributeError, OSError, ValueError):
                return 0  # A genuinely closed stdin is EOF, not a prompt error.
        # Also restore the terminal around noninteractive lifecycle/attach commands
        # invoked from a TTY. Signal exits unwind clone staging and terminal state.
        with terminal():
            return execute(args)
    except SignalExit as e:
        if e.code == 129:
            silence_hungup_output()
        return e.code
    except KeyboardInterrupt:
        return 130
    except Unavailable as e:
        # The session is absent or not running now: a tool may retry later.
        print("Hubi: "+pl(display(e)), file=sys.stderr)
        return 4
    except HubiError as e:
        if args.debug:
            traceback.print_exc()
        print("Hubi: "+pl(display(e)), file=sys.stderr)
        return 2
    except OSError as e:
        if e.errno in (errno.EIO, errno.EBADF, errno.EPIPE):
            silence_hungup_output()
            return 129
        if args.debug:
            traceback.print_exc()
        print(f"Hubi: {type(e).__name__}; operacja nie powiodła się. Uruchom `hubi doctor`.", file=sys.stderr)
        return 1
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        logging.getLogger("hubi6").error("Unhandled exception type=%s", type(e).__name__)
        print(f"Hubi: {type(e).__name__}; operacja nie powiodła się. Uruchom `hubi doctor`.", file=sys.stderr)
        return 1


def silence_hungup_output():
    # Prevent CPython's final flush from replacing SIGHUP/129 with exit 120.
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                os.dup2(fd, stream.fileno())
            except (AttributeError, OSError, ValueError):
                pass
    finally:
        os.close(fd)
