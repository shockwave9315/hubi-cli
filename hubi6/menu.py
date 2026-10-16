"""Polish single-key dashboard.

Frames are rendered from one complete snapshot by pure functions; lifecycle
stays in services.App. One key is one action. Input that is not a single clean
key (a paste, or several keys delivered together) is discarded and reported,
and anything that stops or creates something is confirmed on a drained
terminal. The line-oriented menu in tui.py remains available as a fallback.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import select
import signal
import sys
import termios
import threading
import time
import unicodedata

from .core.model import HubiError, RepoHealth, State, actions, display
from .github import GitHub
from .i18n import DEFAULT_INSTANCE, HEALTH_PL, KIND_PL, RUNTIME_PL, STATE_PL, age, instance_of, name_pl, place, pl, plural
from .tui import Input, terminal

PAGE = 9
KINDS = {"l": "claude", "c": "codex", "t": "terminal"}
HOME_KEY = "h"            # the home workspace: a shell or an agent outside any repository
HOME_NOTE = "bez repo"
SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tmux", "python3", "sleep", ""}
GENERIC_TITLES = {"", "claude", "claude code", "codex", "openai codex"}

STYLES = {
    "bold": "1", "dim": "2", "rev": "7",
    "key": "1;38;5;221", "head": "1;38;5;255", "section": "1;38;5;110",
    "claude": "38;5;209", "codex": "38;5;79", "terminal": "38;5;75",
    "ok": "38;5;78", "busy": "1;38;5;221", "idle": "38;5;110", "warn": "38;5;221",
    "bad": "1;38;5;203", "muted": "38;5;245", "confirm": "1;38;5;232;48;5;221",
    "on": "1;38;5;232;48;5;221",
}


@dataclass(frozen=True)
class Glyphs:
    icons: dict
    spinner: str
    ok: str
    bad: str
    clients: str
    cursor: str
    rule: str
    more: str
    sep: str


UNICODE = Glyphs({"claude": "✳", "codex": ">_", "terminal": "$"},
                 "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
                 "●", "✗", "◉", "▸", "─", "…", "·")
ASCII = Glyphs({"claude": "C", "codex": ">_", "terminal": "$"}, "|/-\\", "*", "!", "@", ">", "-", "~", "-")


class Theme:
    def __init__(self, color=True):
        self.color = color

    def __call__(self, text, style=""):
        if not self.color or not style or not text:
            return text
        codes = ";".join(STYLES[name] for name in style.split())
        return f"\033[{codes}m{text}\033[0m"


# ---------------------------------------------------------------- text metrics

def char_width(char):
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def width_of(text):
    return sum(char_width(char) for char in text)


def clip(text, width, more="…"):
    """Shorten to at most `width` terminal cells; never wraps."""
    if width <= 0:
        return ""
    if width_of(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        cells = char_width(char)
        if used+cells > width-1:
            break
        out.append(char)
        used += cells
    return "".join(out)+more


def pad(text, width, *, right=False, more="…"):
    text = clip(text, width, more)
    gap = " "*max(0, width-width_of(text))
    return gap+text if right else text+gap


def paint_line(segments, theme, width):
    """Segments are plain strings or (text, style). The result fits `width`."""
    out, used = [], 0
    for segment in segments:
        text, style = (segment, "") if isinstance(segment, str) else segment
        if used >= width:
            break
        if used+width_of(text) > width:
            text = clip(text, width-used)
        used += width_of(text)
        out.append(theme(text, style))
    return "".join(out)


def flow(items, width, indent=1):
    """Wrap (key, label) hints into as few lines as fit, never splitting one."""
    lines, current, used = [], [" "*indent], indent
    for key, label in items:
        need = width_of(key)+1+width_of(label)
        if used > indent and used+2+need > width:
            lines.append(current)
            current, used = [" "*indent], indent
        if used > indent:
            current.append("  ")
            used += 2
        current += [(key, "key"), (" "+label, "muted")]
        used += need
    if used > indent:
        lines.append(current)
    return lines


# ---------------------------------------------------------------- view model

@dataclass
class Item:
    id: str
    kind: str
    instance: str
    repo: str
    path: str
    state: State
    health: RepoHealth
    clients: int
    created: float
    exit_status: object
    exit_signal: str
    title: str
    busy: bool
    spinner: str
    issues: list
    generation: str

    @property
    def name(self):
        return f"{self.kind}/{name_pl(self.instance)}"


@dataclass
class RepoRow:
    ref: object
    name: str
    branch: str
    items: list


@dataclass
class View:
    health: str
    issues: list
    items: list
    repos: list
    now: float
    home: object = None       # RepoRow of the home workspace, None when disabled/unavailable


def places(view):
    """Where sessions can live, in menu order: the home workspace, then repositories."""
    return ([view.home] if view.home else [])+view.repos


def describe(kind, command, title, repo):
    """A short, informative activity line, or '' when the title is generic."""
    if kind == "terminal":
        return "" if command in SHELLS else command
    text = title.strip()
    if len(text) > 1 and not text[0].isalnum() and text[1] == " ":
        text = text[2:].strip()
    head, sep, tail = text.rpartition(" | ")
    if sep and tail.strip() == repo:
        text = head.strip()
    if len(text) == 1 and not text.isalnum():         # a bare spinner frame: no topic yet
        return ""
    return "" if text.lower() in GENERIC_TITLES or text == repo else text


def working(title):
    """The glyph an agent puts in front of its title while it works, or ''.

    Codex animates a braille spinner. Claude Code shows a half circle while it
    works and an eight-spoked asterisk while it waits, so "any symbol" is not
    enough: only glyphs from spinner sets count.
    """
    head = title[:1]
    if not head or title[1:2] not in ("", " "):
        return ""
    code = ord(head)
    spinning = 0x2801 <= code <= 0x28ff or 0x25d0 <= code <= 0x25d7 or 0x25dc <= code <= 0x25e1 or 0x25f0 <= code <= 0x25f7
    return head if spinning else ""


def free_name(items, kind, path):
    """The automatic name for a new session: the default one, else the first free number.

    Every recorded session of that type in that repository counts as taken,
    whatever its state, so a new session never reuses or replaces another one.
    """
    taken = {item.instance for item in items if item.kind == kind and item.path == path}
    if DEFAULT_INSTANCE not in taken:
        return DEFAULT_INSTANCE
    number = 2
    while str(number) in taken:
        number += 1
    return str(number)


def branch_of(ref):
    """Branch name from the HEAD file. Filesystem only: no Git subprocess per row."""
    try:
        with open(os.path.join(ref.git_dir, "HEAD"), encoding="utf-8", errors="replace") as stream:
            value = stream.read(256).strip()
    except OSError:
        return ""
    if value.startswith("ref: refs/heads/"):
        return value[16:]
    return value[:7] if value and not value.startswith("ref:") else ""


def build_view(snapshot, titles, now=None, home=None):
    items = []
    for w in snapshot.workloads:
        r, session = w.record, w.session
        repo = place(r.repo)
        command, title = titles.get(r.session, ("", ""))
        live = w.state in (State.RUNNING, State.ATTACHED)
        spinner = working(title)
        items.append(Item(r.id, r.kind, r.instance, repo, r.repo.path, w.state, w.repo_health,
                          session.attached if session else 0, r.created,
                          session.exit_status if session else None, session.exit_signal if session else "",
                          describe(r.kind, command, title, repo) if live else "",
                          bool(spinner) and live and r.kind != "terminal", spinner, list(w.issues), r.generation))
    # Creation order: starting something never renumbers what is already there.
    items.sort(key=lambda item: (item.created, item.id))
    repos = [RepoRow(ref, os.path.basename(ref.path), branch_of(ref), [i for i in items if i.path == ref.path])
             for ref in snapshot.repos]
    row = RepoRow(home, place(home), HOME_NOTE, [i for i in items if i.path == home.path]) if home else None
    return View(snapshot.runtime_health, list(snapshot.issues), items, repos, now or time.time(), row)


@dataclass
class Ui:
    screen: str = "main"
    verb: str = ""              # main: "" | "stop" | "info"
    kind: str = ""              # pick: workload type being created, "" = open repo
    instance: str = ""          # session name typed by the user, "" = automatic
    resume: bool = False
    page: int = 0
    cursor: int = -1
    repo: object = None         # selected RepoRef
    selected: str = ""          # selected workload id
    message: str = ""
    level: str = "muted"
    git: str = ""
    autologin: bool = False
    history: list = field(default_factory=list)


# ---------------------------------------------------------------- rendering

def state_cell(item, glyphs):
    if item.health != RepoHealth.OK:
        return HEALTH_PL[item.health], "bad"
    if item.state == State.EXITED:
        if item.exit_signal:
            return "przerwany", "bad"
        if item.exit_status == 0:
            return "zakończony", "muted"
        return f"błąd (kod {item.exit_status})" if item.exit_status is not None else "zakończony", "bad"
    if item.state in (State.RUNNING, State.ATTACHED):
        if item.busy:
            return ("pracuje "+item.spinner).strip(), "busy"
        return ("działa", "ok") if item.kind == "terminal" else ("czeka", "idle")
    return STATE_PL[item.state], "warn" if item.state == State.STARTING else "bad"


def head(view, title, width, glyphs):
    good = view.health == "OK"
    left = [(" HUBI v6", "head"), "  ", (glyphs.ok if good else glyphs.bad, "ok" if good else "bad"),
            (" runtime "+RUNTIME_PL.get(view.health, view.health), "muted" if good else "bad")]
    if title:
        left += ["   ", (title, "section")]
    clock = time.strftime("%H:%M", time.localtime(view.now))
    used = sum(width_of(s if isinstance(s, str) else s[0]) for s in left)
    if used+len(clock)+2 <= width:
        left += [" "*(width-used-len(clock)), (clock, "muted")]
    return left


def workload_row(index, item, width, glyphs, *, selected=False, labelled=True):
    text, style = state_cell(item, glyphs)
    lead = [(glyphs.cursor if selected else " ", "key"), (pad(str(index) if labelled else glyphs.sep, 2), "key" if labelled else "muted"), " "]
    icon = (pad(glyphs.icons[item.kind], 3), item.kind)
    clients = (pad(f"{glyphs.clients}{item.clients}" if item.clients else "", 3), "warn")
    when = (pad(age(time.time()-item.created), 8, right=True), "muted")
    rest = width-(4+3+13+3+8+2)
    if rest < 18:      # very narrow: name only
        return lead+[icon, (pad(item.name+"@"+item.repo, max(4, width-4-3-13)), "bold"), " ", (pad(text, 12), style)]
    name = max(10, rest*55//100)
    return lead+[icon, (pad(item.name, name), "bold"), " ", (pad(item.repo, rest-name), ""), " ",
                 (pad(text, 12), style), " ", clients, when]


def repo_row(label, row, width, glyphs, *, selected=False, kind=""):
    lead = [(glyphs.cursor if selected else " ", "key"), (pad(label or glyphs.sep, 2), "key" if label else "muted"), " "]
    rest = width-4
    name = max(12, min(22, rest*36//100))
    branch = max(0, min(16, rest-name-1))
    tail = max(0, rest-name-branch-2)
    cells = lead+[(pad(row.name, name), "bold" if label else ""), " ", (pad(row.branch, branch), "muted"), " "]
    marks = [i for i in row.items if not kind or i.kind == kind]
    used = shown = 0
    for item in marks:
        piece = f"{glyphs.icons[item.kind]} {name_pl(item.instance)}  "
        if used+width_of(piece) > tail:
            break
        cells.append((piece, item.kind))
        used += width_of(piece)
        shown += 1
    if shown < len(marks) and used+len(f"+{len(marks)-shown}") <= tail:
        cells.append((f"+{len(marks)-shown}", "muted"))   # never hide a session without saying so
    return cells


def rule(width, glyphs):
    return [(" "+glyphs.rule*max(0, width-2), "muted")]


def message_lines(ui, width, limit=3):
    """The status message, word-wrapped instead of being cut off."""
    if not ui.message:
        return [[""]]
    lines, current = [], ""
    for word in ui.message.split(" "):
        candidate = current+" "+word if current else word
        if current and width_of(candidate) > width-2:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return [[(" "+pad(line.strip(), width-2)+" " if ui.level == "confirm" else " "+line.strip(), ui.level)] for line in lines[:limit]]


def issue_lines(view, limit=2):
    return [[(" ! "+pl(display(issue)), "bad")] for issue in view.issues[:limit]]


def frame_main(view, ui, width, height, glyphs):
    top = [head(view, "", width, glyphs)]+issue_lines(view)
    pages = max(1, (len(view.items)+PAGE-1)//PAGE)
    page = min(ui.page, pages-1)
    shown = view.items[page*PAGE:page*PAGE+PAGE]
    banner = {"stop": "ZATRZYMAJ: naciśnij numer sesji (inny klawisz anuluje)",
              "info": "SZCZEGÓŁY: naciśnij numer sesji (inny klawisz anuluje)"}.get(ui.verb, "")
    title = [(" AGENCI I TERMINALE", "section"), (f"  {len(view.items)}", "muted")]
    if pages > 1:
        title.append((f"   strona {page+1}/{pages}, spacja = następna", "muted"))
    rows, details = [], {}
    for number, item in enumerate(shown, 1):
        rows.append(workload_row(number, item, width, glyphs, selected=ui.cursor == number-1))
        if item.title:
            details[len(rows)-1] = [(" "*7+clip(display(item.title), width-8), "muted")]
    if not view.items:
        rows.append([("    Nic nie działa. ", "muted"), ("l", "key"), (" Claude  ", "muted"), ("c", "key"),
                     (" Codex  ", "muted"), ("t", "key"), (" terminal", "muted")])
    repos = [repo_row(HOME_KEY if row is view.home else "", row, width, glyphs, selected=ui.cursor == len(shown)+n)
             for n, row in enumerate(places(view))]
    hints = [("1-9", "wejdź"), ("l", "Claude"), ("c", "Codex"), ("t", "terminal"), ("d", "zatrzymaj"),
             ("i", "szczegóły"), ("r", "repo"), (HOME_KEY, "~ bez repo"), ("g", "GitHub"), ("?", "pomoc"),
             ("q", "shell" if ui.autologin else "wyjdź")]
    if view.home is None:
        hints.remove((HOME_KEY, "~ bez repo"))
    if ui.autologin:
        hints.append(("x", "rozłącz"))
    bottom = [rule(width, glyphs)]+flow(hints, width)
    bottom += [[(" "+pad(banner, width-2)+" ", "confirm")]] if banner and not ui.message else message_lines(ui, width)
    room = height-len(top)-len(bottom)-2          # two section titles
    keep_details = len(rows)+len(details)+len(repos) <= room
    if not keep_details:                           # busy agents keep theirs while space allows
        busy = {n: d for n, d in details.items() if shown[n].busy}
        details = busy if len(rows)+len(busy)+len(repos) <= room else {}
    body = []
    for n, row in enumerate(rows):
        body.append(row)
        if n in details:
            body.append(details[n])
    left = max(0, room-len(body))
    if len(repos) > left:
        hidden = len(repos)-max(0, left-1)
        repos = repos[:max(0, left-1)]+[[(f"    {glyphs.more} i jeszcze {hidden} (", "muted"), ("r", "key"), (" = wszystkie)", "muted")]]
    section = [(" REPOZYTORIA", "section"), (f"  {len(view.repos)}", "muted")]
    spare = height-len(bottom)-len(top)-2-len(body)-len(repos)
    gap = [[""]] if spare >= 2 else []
    lines = top+gap+[title]+body+gap+[section]+repos
    return lines[:max(0, height-len(bottom))]+bottom


def start_options(ui, *, talks):
    """What the next start will do, said loudly: the conversation switch and a typed name."""
    lines = []
    if talks:
        new = ("[ NOWA ROZMOWA ]", "on") if not ui.resume else ("  nowa rozmowa  ", "muted")
        old = ("[ WZNÓW ROZMOWĘ ]", "on") if ui.resume else ("  wznów rozmowę  ", "muted")
        lines.append([" ", new, " ", old, ("   r przełącza", "muted")])
    if ui.instance:
        lines.append([(" nazwa sesji: ", "muted"), (name_pl(ui.instance), "bold")])
    return lines


def frame_pick(view, ui, width, height, glyphs):
    resume = ui.resume and ui.kind not in ("", "terminal")
    what = "REPOZYTORIUM" if not ui.kind else ("WZNÓW " if resume else "NOWY ")+glyphs.icons[ui.kind]+" "+KIND_PL[ui.kind].upper()
    top = [head(view, what, width, glyphs)]
    if ui.kind:
        top += start_options(ui, talks=ui.kind != "terminal")
    top.append([(" Wybierz repo, otworzy się lista rozmów:" if resume else " Wybierz repozytorium:", "section")])
    pages = max(1, (len(view.repos)+PAGE-1)//PAGE)
    page = min(ui.page, pages-1)
    rows = [repo_row(str(n), row, width, glyphs, kind=ui.kind, selected=ui.cursor == n-1)
            for n, row in enumerate(view.repos[page*PAGE:page*PAGE+PAGE], 1)]
    if not view.repos:
        rows = [[("    Brak repozytoriów w katalogu roboczym. ", "muted"), ("g", "key"), (" = GitHub", "muted")]]
    if view.home:                                     # on every page: it is a key, not a number
        rows.insert(0, repo_row(HOME_KEY, view.home, width, glyphs, kind=ui.kind))
    hints = [("1-9", "start" if ui.kind else "otwórz")]
    if view.home:
        hints.append((HOME_KEY, "~ bez repo"))
    if ui.kind:
        hints.append(("n", "nazwa sesji"))
        if ui.kind != "terminal":
            hints.append(("r", "nowa/wznów"))
    if pages > 1:
        hints.append(("spacja", f"strona {page+1}/{pages}"))
    hints.append(("0", "wróć"))
    bottom = [rule(width, glyphs)]+flow(hints, width)+message_lines(ui, width)
    return (top+rows)[:max(0, height-len(bottom))]+bottom


def frame_repo(view, ui, width, height, glyphs):
    row = next((r for r in places(view) if r.ref.path == ui.repo.path), None)
    home = ui.repo.plain
    title = "KATALOG DOMOWY ~" if home else "REPO "+os.path.basename(ui.repo.path)
    top = [head(view, title, width, glyphs),
           [(" "+clip(display(ui.repo.path), width-2), "muted"),
            ("  "+(row.branch if row else ""), "muted" if home else "ok")]]
    top += start_options(ui, talks=True)
    items = row.items if row else []
    body = [[(" Sesje w katalogu domowym:" if home else " Sesje w tym repo:", "section")]] if items else []
    body += [workload_row(n, item, width, glyphs) for n, item in enumerate(items[:PAGE], 1)]
    hints = [("l", "Claude"), ("c", "Codex"), ("t", "terminal"), ("n", "nazwa sesji"), ("r", "nowa/wznów"),
             ("s", "status Gita"), ("0", "wróć")]
    if home:
        hints.remove(("s", "status Gita"))
    if items:
        hints.insert(0, ("1-9", "wejdź"))
    bottom = [rule(width, glyphs)]+flow(hints, width)+message_lines(ui, width)
    room = height-len(top)-len(body)-len(bottom)
    git = [line for line in ui.git.splitlines() if line.strip()]
    if git and room > 1:
        body.append([(" Git:", "section")])
        shown = git[:room-1] if len(git) <= room-1 else git[:max(0, room-2)]
        body += [[("  "+clip(display(line), width-3), "")] for line in shown]
        if len(shown) < len(git):
            body.append([(f"  {glyphs.more} i jeszcze {len(git)-len(shown)} wierszy", "muted")])
    return (top+body)[:max(0, height-len(bottom))]+bottom


def frame_detail(view, ui, width, height, glyphs):
    item = next((i for i in view.items if i.id == ui.selected), None)
    if item is None:
        return None
    text, style = state_cell(item, glyphs)
    count = item.clients
    top = [head(view, "SESJA", width, glyphs),
           [" ", (glyphs.icons[item.kind]+" ", item.kind), (item.name, "bold"), ("  "+glyphs.sep+"  "+item.repo, ""),
            "   ", (text, style)],
           [(" "+clip(display(item.path), width-2), "muted")],
           [(" stan: ", "muted"), (STATE_PL[item.state], ""), (f"   klienci: {count}   wiek: {age(view.now-item.created)}", "muted")]]
    if item.state == State.EXITED:
        code = "sygnał "+item.exit_signal if item.exit_signal else f"kod {item.exit_status}"
        top.append([(" zakończenie: ", "muted"), (code, "ok" if item.exit_status == 0 and not item.exit_signal else "bad")])
    if item.title:
        top.append([(" teraz: ", "muted"), (clip(display(item.title), width-9), "")])
    top.append([(" id: ", "muted"), (clip(item.id, width-6), "muted")])
    top += [[(" ! "+pl(display(issue)), "bad")] for issue in item.issues[:3]]
    allowed = actions(item.state)
    live = item.state in (State.RUNNING, State.ATTACHED)
    hints = []
    if live:
        hints += [("a", "wejdź"), ("v", "podgląd"), ("s", "dołącz obok"), ("o", "przejmij")]
    elif "view" in allowed:
        hints.append(("v", "podgląd wyjścia"))
    if "stop" in allowed:
        hints.append(("d", "zatrzymaj" if live else "usuń"))
    if "reconcile" in allowed:
        hints.append(("f", "napraw"))
    hints.append(("0", "wróć"))
    bottom = [rule(width, glyphs)]+flow(hints, width)+message_lines(ui, width)
    return top[:max(0, height-len(bottom))]+bottom


def frame_github(view, ui, width, height, glyphs, catalogue, repos):
    note = ""
    if catalogue is not None and catalogue.stale:
        note = "zapisana lista, może być nieaktualna"
    top = [head(view, "GITHUB", width, glyphs)]
    if catalogue is not None and catalogue.error:
        top.append([(" ! "+pl(catalogue.error), "warn")])
    elif note:
        top.append([(" "+note, "warn")])
    pages = max(1, (len(repos)+PAGE-1)//PAGE)
    page = min(ui.page, pages-1)
    rows = []
    for n, repo in enumerate(repos[page*PAGE:page*PAGE+PAGE], 1):
        flags = " ".join(word for word, on in (("prywatne", repo["isPrivate"]), ("fork", repo["isFork"]),
                                              ("archiwum", repo["isArchived"])) if on)
        local = ("lokalne", "ok") if repo["local"] else ("sklonuj", "muted")
        name = max(16, width-4-9-width_of(flags)-2)
        rows.append([(" ", ""), (pad(str(n), 2), "key"), " ", (pad(display(repo["nameWithOwner"]), name), "bold" if repo["local"] else ""),
                     " ", (pad(local[0], 8), local[1]), " ", (flags, "muted")])
    if not repos:
        rows.append([("    Brak repozytoriów.", "muted")])
    hints = [("1-9", "otwórz lub sklonuj"), ("r", "odśwież")]
    if pages > 1:
        hints.append(("spacja", f"strona {page+1}/{pages}"))
    hints.append(("0", "wróć"))
    bottom = [rule(width, glyphs)]+flow(hints, width)+message_lines(ui, width)
    return (top+rows)[:max(0, height-len(bottom))]+bottom


HELP = (
    ("1-9", "wejdź do sesji z listy, bez Entera"),
    ("l c t", "nowa sesja: Claude / Codex / terminal, potem repo albo h"),
    ("h", "katalog domowy ~: terminal albo agent poza repozytorium"),
    ("r n", "przy wyborze repo: wznów rozmowę / własna nazwa"),
    ("d", "zatrzymaj: numer sesji, potem q potwierdza"),
    ("i", "szczegóły: podgląd, dołączenie, przejęcie, naprawa"),
    ("r", "widok repozytorium i status Gita"),
    ("g", "repozytoria na GitHubie, klonowanie"),
    ("0", "wróć (także Esc i Backspace)"),
    ("strzałki", "kursor, Enter = wejdź"),
    ("q x", "q = shell, x = rozłącz SSH"),
    ("W sesji", ""),
    ("Ctrl-b d", "wyjście do menu, sesja działa dalej"),
    ("Ctrl-b )", "następna sesja, Ctrl-b ( poprzednia"),
    ("Ctrl-b L", "ostatnio używana sesja (przeskok tam i z powrotem)"),
    ("Ctrl-b s", "lista sesji (czytelna na szerokim ekranie)"),
    ("exit", "koniec programu = powrót do menu"),
)


def frame_help(view, ui, width, height, glyphs):
    top = [head(view, "POMOC", width, glyphs)]
    for key, text in HELP:
        if not text:
            top.append([(" "+key, "section")])
        else:
            top.append([" ", (pad(key, 10), "key"), (clip(text, width-12), "")])
    bottom = [rule(width, glyphs)]+flow([("0", "wróć")], width)+message_lines(ui, width)
    return top[:max(0, height-len(bottom))]+bottom


def render(lines, theme, width):
    return [paint_line(line, theme, width) for line in lines]


# ---------------------------------------------------------------- terminal I/O

def parse_key(chunk):
    """One clean key from one read, or None for a burst.

    Returns a printable character, a key name, or "" for a complete but
    unsupported key (ignored without being treated as a paste).
    """
    names = {b"\r": "enter", b"\n": "enter", b"\x1b": "esc", b"\x7f": "back", b"\x08": "back", b"\t": "tab",
             b"\x04": "eof", b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
             b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left",
             b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn", b"\x1b[Z": "tab"}
    if chunk in names:
        return names[chunk]
    if len(chunk) == 1:
        return chr(chunk[0]) if 32 <= chunk[0] < 127 else ""
    if chunk[:2] in (b"\x1b[", b"\x1bO") and len(chunk) <= 8 and 0x40 <= chunk[-1] <= 0x7e and \
            all(0x20 <= byte <= 0x3f for byte in chunk[2:-1]):
        return ""                                     # one complete escape sequence we do not use
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if len(text) == 1 and unicodedata.category(text)[0] != "C" else None


class Keys(Input):
    """Single-key reader on top of the line reader's quarantine and paste rules."""

    def __init__(self, fd, wake=None, **kw):
        super().__init__(fd, echo=True, **kw)
        self.wake = wake

    def settle(self):
        """Require 80 ms of silence, then drop anything still queued."""
        self.discard_burst(force=True)
        if os.isatty(self.fd):
            termios.tcflush(self.fd, termios.TCIFLUSH)

    def rearm(self):
        self.arm()
        self.last_byte = time.monotonic()
        self.finish_line()

    def swallow_paste(self, data):
        end = time.monotonic()+self.paste_timeout
        while b"\x1b[201~" not in data:
            ready, _, _ = select.select([self.fd], [], [], max(0, end-time.monotonic()))
            if not ready:
                break
            more = os.read(self.fd, 4096)
            if not more:
                break
            data = data[-5:]+more                     # keep a split closing marker
        self.last_byte = time.monotonic()
        self.finish_line()
        self.discard_burst()

    def key(self, timeout=None):
        """A key, "burst" (discarded input), "resize", "eof", or None on timeout."""
        self.discard_burst()
        watched = [self.fd]+([self.wake] if self.wake is not None else [])
        ready, _, _ = select.select(watched, [], [], timeout)
        if not ready:
            return None
        if self.wake is not None and self.wake in ready:
            try:
                os.read(self.wake, 4096)
            except OSError:
                pass
            if self.fd not in ready:
                return "resize"
        chunk = os.read(self.fd, 4096)
        if not chunk:
            return "eof"
        self.last_byte = time.monotonic()
        if b"\x1b[200~" in chunk:
            self.swallow_paste(chunk)
            return "burst"
        key = parse_key(chunk)
        if key is None:
            self.finish_line()
            self.discard_burst()
            return "burst"
        return key


class Screen:
    """Alternate-screen painter. One write per frame; only changed lines are sent."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.previous = None
        self.lock = threading.Lock()

    def size(self):
        try:
            size = os.get_terminal_size(self.stream.fileno())
            return max(20, size.columns), max(6, size.lines)
        except (OSError, ValueError, AttributeError):
            return 80, 24

    def write(self, data):
        with self.lock:
            self.stream.write(data)
            self.stream.flush()

    def enter(self):
        self.previous = None
        self.write("\033[?1049h\033[?25l\033[H\033[2J")

    def leave(self):
        try:
            self.write("\033[0m\033[?25h\033[?1049l")
        except OSError:
            pass                                       # the PTY may already be gone

    def invalidate(self):
        self.previous = None

    def paint(self, lines):
        _, rows = self.size()
        lines = lines[:rows]
        parts = []
        if self.previous is None:
            parts.append("\033[H\033[2J")
            parts += [f"\033[{row};1H{line}" for row, line in enumerate(lines, 1) if line]
        else:
            for row, line in enumerate(lines, 1):
                if row > len(self.previous) or self.previous[row-1] != line:
                    parts.append(f"\033[{row};1H{line}\033[0m\033[K")
            if len(lines) < len(self.previous):
                parts.append(f"\033[{len(lines)+1};1H\033[J")
        if parts:
            self.write("".join(parts))
        self.previous = list(lines)

    def last_row(self):
        return max(1, len(self.previous or [""]))


class Menu:
    def __init__(self, app, reader, screen):
        self.app, self.reader, self.screen = app, reader, screen
        self.ui = Ui(autologin=bool(os.environ.get("HUBI_AUTOLOGIN")))
        self.theme = Theme(app.c.ui_color and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb")
        self.glyphs = UNICODE if app.c.ui_icons else ASCII
        self.github = GitHub(app)
        self.catalogue = None
        self.snapshot = None
        self.view = None
        self.repos = []
        self.home_error = ""
        self.touched = time.monotonic()

    # ---- presentation

    def say(self, text, level="muted"):
        self.ui.message, self.ui.level = text, level

    def name(self, item):
        return f"{self.glyphs.icons[item.kind]} {item.name} ({item.repo})"

    def refresh(self):
        self.snapshot = self.app.snapshot()
        try:
            home, self.home_error = self.app.git.home(), ""
        except HubiError as e:
            home, self.home_error = None, str(e)
        self.view = build_view(self.snapshot, self.app.tmux.titles(), home=home)

    def home(self):
        if self.view.home is None:
            raise HubiError(self.home_error or "Home workspace is unavailable")
        return self.view.home.ref

    def draw(self):
        width, height = self.screen.size()
        width -= 1                                    # never write the last column: no autowrap anywhere
        ui, view = self.ui, self.view
        if ui.screen == "detail":
            lines = frame_detail(view, ui, width, height, self.glyphs)
            if lines is None:
                ui.screen = "main"
                self.say("Sesja została zatrzymana gdzie indziej", "warn")
        if ui.screen == "repo" and ui.repo is None:
            ui.screen = "main"
        if ui.screen == "main":
            lines = frame_main(view, ui, width, height, self.glyphs)
        elif ui.screen == "pick":
            lines = frame_pick(view, ui, width, height, self.glyphs)
        elif ui.screen == "repo":
            lines = frame_repo(view, ui, width, height, self.glyphs)
        elif ui.screen == "github":
            self.repos = self.github.view(self.catalogue) if self.catalogue else []
            lines = frame_github(view, ui, width, height, self.glyphs, self.catalogue, self.repos)
        elif ui.screen == "help":
            lines = frame_help(view, ui, width, height, self.glyphs)
        self.screen.paint(render(lines, self.theme, width))

    @contextmanager
    def busy(self, text):
        """Animate one status line while the main thread performs the operation.

        The operation itself stays on the main thread, so signals interrupt it
        exactly as before; the helper thread only writes spinner frames.
        """
        row, done = self.screen.last_row(), threading.Event()
        width = self.screen.size()[0]-1

        def spin():
            frame = 0
            while not done.is_set():
                glyph = self.glyphs.spinner[frame % len(self.glyphs.spinner)]
                line = paint_line([(f" {glyph} {text}", "busy")], self.theme, width)
                try:
                    self.screen.write(f"\033[{row};1H{line}\033[0m\033[K")
                except OSError:
                    return
                frame += 1
                done.wait(.09)

        worker = threading.Thread(target=spin, daemon=True)
        worker.start()
        try:
            yield
        finally:
            done.set()
            worker.join(1)
            self.screen.invalidate()

    def confirm(self, question):
        """A deliberate single `q` on a silent, flushed terminal."""
        self.reader.settle()
        self.say(f"{question}   q = TAK   inny klawisz = nie", "confirm")
        self.draw()
        key = self.reader.key()
        self.say("")
        return key in ("q", "Q")

    def ask_line(self, text, *, destructive=False):
        self.screen.write(f"\033[{self.screen.last_row()};1H\033[2K\033[?25h")
        try:
            return self.reader.prompt(text, destructive=destructive)
        finally:
            self.screen.write("\033[?25l")
            self.screen.invalidate()

    # ---- actions

    def attach(self, ident, mode="normal"):
        live = [i for i in self.view.items if i.state in (State.RUNNING, State.ATTACHED) and i.id != ident]
        self.reader.settle()                          # nothing typed earlier may reach the agent
        self.screen.leave()
        try:
            code = self.app.attach(ident, mode)
        finally:
            # tmux leaves "[detached (from session <generation hash>)]" on the
            # normal screen; do not let those pile up behind the menu.
            self.screen.write("\033[H\033[2J")
            self.screen.enter()
            self.reader.rearm()
        self.ui.cursor = -1
        # Ctrl-b ) / ( / L can move the client to another session, so every
        # session that was alive when we went in is checked, not only this one.
        done = [w for w in [self.app.reap_clean_exit(ident)]+[self.app.reap_clean_exit(i.id) for i in live] if w]
        if done:
            names = ", ".join(f"{w.record.kind}/{name_pl(w.record.instance)}" for w in done)
            self.say(f"{names}: koniec pracy, posprzątane", "ok")
            if self.ui.screen == "detail" and any(w.record.id == ident for w in done):
                self.ui.screen = "main"
        if any(w.record.id == ident for w in done):
            return
        try:
            after = self.app.find(ident, health_check=False)
        except HubiError:
            return
        if after.state == State.EXITED:
            self.ui.screen, self.ui.selected = "detail", ident
            status = after.session.exit_status if after.session else None
            self.say(f"Sesja zakończyła się błędem (kod {status}); v = podgląd wyjścia, d = usuń", "bad")
        elif code:
            self.say(f"Nie udało się wejść do sesji (kod {code})", "bad")

    def enter(self, item):
        if item.state not in (State.RUNNING, State.ATTACHED):
            self.ui.screen, self.ui.selected = "detail", item.id
            return
        mode = "normal"
        if item.clients:
            self.reader.settle()
            others = plural(item.clients, "klienta", "klientów", "klientów")
            self.say(f"{item.name} ma już {item.clients} {others}:  s = dołącz obok   o = przejmij   v = podgląd", "confirm")
            self.draw()
            mode = {"s": "shared", "o": "takeover", "v": "view"}.get((self.reader.key() or "").lower())
            self.say("")
            if not mode:
                self.say("Anulowano", "muted")
                return
        self.attach(item.id, mode)

    def stop(self, item, *, repair=False):
        live = item.state in (State.RUNNING, State.ATTACHED, State.STARTING)
        verb = "Naprawić" if repair else ("Zatrzymać" if live else "Usunąć")
        if not self.confirm(f"{verb} {self.name(item)}?"):
            self.say("Anulowano", "muted")
            return
        with self.busy(("Naprawiam " if repair else "Zatrzymuję ")+item.name+self.glyphs.more):
            self.app.stop(item.id, expected=item.generation, repair=repair)
        self.say(f"{item.name}: "+("naprawiono" if repair else "zatrzymano" if live else "usunięto"), "ok")
        if self.ui.screen == "detail" and not repair:
            self.ui.screen = "main"

    def launch(self, kind, ref):
        """l/c/t always means a new session; a running one is entered with its number.

        Without a typed name the session takes the first free automatic name, so
        "resume" resumes even where that agent already runs. Only a name the user
        typed can address an existing session.
        """
        named, resume = self.ui.instance, self.ui.resume and kind != "terminal"
        self.refresh()                                # the name is chosen from the current inventory
        instance = named or free_name(self.view.items, kind, ref.path)
        label = f"{kind}/{name_pl(instance)}"
        for item in self.view.items if named else ():
            if (item.kind, item.path, item.instance) != (kind, ref.path, instance):
                continue
            if item.state in (State.RUNNING, State.ATTACHED):
                self.say(f"{label} już działa", "ok")
                self.ui.instance, self.ui.resume = "", False
                return self.enter(item)
            if item.state == State.EXITED:
                if not self.confirm(f"Poprzednia sesja {label} jest zakończona. Usunąć ją i uruchomić nową?"):
                    self.say("Anulowano", "muted")
                    return
                with self.busy("Usuwam poprzednią sesję"+self.glyphs.more):
                    self.app.stop(item.id, expected=item.generation)
        try:
            with self.busy(f"Uruchamiam {label}"+self.glyphs.more):
                record = self.app.start(kind, ref.path, instance, resume)
        except HubiError:
            # A start that died immediately stays inspectable: show it.
            self.refresh()
            failed = [i for i in self.view.items if (i.kind, i.path, i.instance) == (kind, ref.path, instance)]
            if failed:
                self.ui.screen, self.ui.selected = "detail", failed[0].id
            raise
        self.ui.instance, self.ui.resume = "", False
        self.attach(record.id)

    def open_repo(self, ref):
        self.ui.screen, self.ui.repo, self.ui.page, self.ui.cursor, self.ui.git = "repo", ref, 0, -1, ""
        if ref.plain:
            return
        try:
            self.ui.git = self.app.git.summary(ref)
        except HubiError as e:
            self.say(pl(e), "bad")

    def open_github(self):
        self.ui.screen, self.ui.page, self.ui.cursor = "github", 0, -1
        if self.catalogue is None:
            self.catalogue = self.github.cached()
            if self.catalogue is None:
                with self.busy("Pobieram listę z GitHuba"+self.glyphs.more):
                    self.catalogue = self.github.list()
        if self.catalogue.stale:
            self.draw()                               # show the saved list first, then update it
            self.refresh_github()

    def refresh_github(self):
        with self.busy("Odświeżam listę z GitHuba"+self.glyphs.more):
            self.catalogue = self.github.list(refresh=True)
        self.say(pl(self.catalogue.error) if self.catalogue.error else "Lista odświeżona",
                 "warn" if self.catalogue.error else "ok")

    def back(self):
        ui = self.ui
        ui.screen = {"pick": "main", "repo": "main", "detail": "main", "github": "main", "help": "main"}.get(ui.screen, "main")
        ui.verb, ui.page, ui.cursor, ui.instance, ui.resume = "", 0, -1, "", False

    def name_instance(self):
        answer = self.ask_line("Nazwa sesji [automatyczna]: ")
        if answer is None:
            return False
        answer = answer.strip()
        if answer:                                    # empty keeps the automatic name
            answer = instance_of(answer)
            from .core.model import validate_instance
            validate_instance(answer)
        self.ui.instance = answer
        return True

    def toggle_resume(self):
        self.ui.resume = not self.ui.resume
        self.say("Wznów: po wyborze repo otworzy się lista rozmów" if self.ui.resume else "Nowa rozmowa", "ok")

    # ---- key dispatch

    def move(self, count, delta):
        if count:
            self.ui.cursor = (self.ui.cursor+delta) % count if self.ui.cursor >= 0 else (0 if delta > 0 else count-1)

    def on_main(self, key):
        ui, view = self.ui, self.view
        pages = max(1, (len(view.items)+PAGE-1)//PAGE)
        page = min(ui.page, pages-1)
        shown = view.items[page*PAGE:page*PAGE+PAGE]
        verb, ui.verb = ui.verb, ""
        if key in "123456789":
            if int(key) > len(shown):
                return self.say(f"Nie ma sesji numer {key}", "warn")
            item = shown[int(key)-1]
            if verb == "stop":
                return self.stop(item)
            if verb == "info":
                ui.screen, ui.selected = "detail", item.id
                return
            return self.enter(item)
        if verb:
            return self.say("Anulowano", "muted")
        if key in KINDS:
            ui.screen, ui.kind, ui.page, ui.cursor = "pick", KINDS[key], 0, -1
        elif key == "r":
            ui.screen, ui.kind, ui.page, ui.cursor = "pick", "", 0, -1
        elif key == HOME_KEY:
            self.open_repo(self.home())
        elif key in ("d", "i"):
            if not view.items:
                return self.say("Nie ma żadnej sesji", "warn")
            ui.verb = "stop" if key == "d" else "info"
        elif key == "g":
            self.open_github()
        elif key == "?":
            ui.screen = "help"
        elif key == " ":
            ui.page, ui.cursor = (page+1) % pages, -1
        elif key in ("down", "j", "up", "k"):
            self.move(len(shown)+len(places(view)), 1 if key in ("down", "j") else -1)
        elif key == "enter" and ui.cursor >= 0:
            if ui.cursor < len(shown):
                return self.enter(shown[ui.cursor])
            if ui.cursor-len(shown) < len(places(view)):
                self.open_repo(places(view)[ui.cursor-len(shown)].ref)
        else:
            return False

    def on_pick(self, key):
        ui, repos = self.ui, self.view.repos
        pages = max(1, (len(repos)+PAGE-1)//PAGE)
        page = min(ui.page, pages-1)
        shown = repos[page*PAGE:page*PAGE+PAGE]
        chosen = None
        if key in "123456789":
            if int(key) > len(shown):
                return self.say(f"Nie ma repozytorium numer {key}", "warn")
            chosen = shown[int(key)-1]
        elif key == "enter" and 0 <= ui.cursor < len(shown):
            chosen = shown[ui.cursor]
        elif key == HOME_KEY:
            ref = self.home()
            if not ui.kind:
                return self.open_repo(ref)
            ui.screen = "main"
            return self.launch(ui.kind, ref)
        if chosen:
            if not ui.kind:
                return self.open_repo(chosen.ref)
            ui.screen = "main"
            return self.launch(ui.kind, chosen.ref)
        if key == "n" and ui.kind:
            self.name_instance()
        elif key == "r" and ui.kind and ui.kind != "terminal":
            self.toggle_resume()
        elif key == " ":
            ui.page, ui.cursor = (page+1) % pages, -1
        elif key in ("down", "j", "up", "k"):
            self.move(len(shown), 1 if key in ("down", "j") else -1)
        elif key == "g" and not repos:
            self.open_github()
        else:
            return False

    def on_repo(self, key):
        ui = self.ui
        row = next((r for r in places(self.view) if r.ref.path == ui.repo.path), None)
        items = row.items[:PAGE] if row else []
        if key in "123456789":
            if int(key) > len(items):
                return self.say(f"Nie ma sesji numer {key}", "warn")
            return self.enter(items[int(key)-1])
        if key in KINDS:
            return self.launch(KINDS[key], ui.repo)
        if key == "n":
            self.name_instance()
        elif key == "r":
            self.toggle_resume()
        elif key == "s":
            if ui.repo.plain:
                return self.say("Katalog domowy nie jest repozytorium Git", "muted")
            ui.git = self.app.git.summary(ui.repo)
            self.say("Status Gita odświeżony", "ok")
        else:
            return False

    def on_detail(self, key):
        item = next((i for i in self.view.items if i.id == self.ui.selected), None)
        if item is None:
            return self.back()
        allowed = actions(item.state)
        live = item.state in (State.RUNNING, State.ATTACHED)
        if key == "a" and live:
            return self.enter(item)
        if key in ("v", "s", "o") and "view" in allowed and (live or key == "v"):
            return self.attach(item.id, {"v": "view", "s": "shared", "o": "takeover"}[key])
        if key == "d" and "stop" in allowed:
            return self.stop(item)
        if key == "f" and "reconcile" in allowed:
            return self.stop(item, repair=True)
        return False

    def on_github(self, key):
        ui, repos = self.ui, self.repos
        pages = max(1, (len(repos)+PAGE-1)//PAGE)
        page = min(ui.page, pages-1)
        shown = repos[page*PAGE:page*PAGE+PAGE]
        if key in "123456789":
            if int(key) > len(shown):
                return self.say(f"Nie ma pozycji numer {key}", "warn")
            picked = shown[int(key)-1]
            if picked["local"]:
                return self.open_repo(self.app.git.resolve(picked["local_paths"][0]))
            name = picked["nameWithOwner"]
            target = self.app.c.repos/name.split("/")[1]
            answer = self.ask_line(f"Sklonować {name} do {target}? Wpisz tak + Enter: ", destructive=True)
            if answer is None:
                return 0
            if answer.lower() != "tak":
                return self.say("Klonowanie anulowane", "muted")
            with self.busy(f"Klonuję {name}"+self.glyphs.more):
                ref = self.github.clone(name)
            self.say(f"Sklonowano {name}", "ok")
            ui.screen, ui.repo, ui.git = "repo", ref, ""
        elif key == "r":
            self.refresh_github()
        elif key == " ":
            ui.page = (page+1) % pages
        else:
            return False

    def handle(self, key):
        """Returns an exit code to leave the menu, otherwise None."""
        ui = self.ui
        if key == "eof":
            return 0
        if key == "resize":
            self.screen.invalidate()
            return None
        if key == "burst":
            self.say("Zignorowano serię znaków (wklejony tekst?). Naciskaj po jednym klawiszu.", "warn")
            return None
        self.touched = time.monotonic()
        self.say("")
        if len(key) == 1:
            key = key.lower()
        if key == "q":
            return 98
        if key == "x" and ui.autologin:
            return 99
        if not key:
            return None
        if key in ("0", "esc", "back") and ui.screen != "main":
            self.back()
            return None
        handler = {"main": self.on_main, "pick": self.on_pick, "repo": self.on_repo, "detail": self.on_detail,
                   "github": self.on_github, "help": lambda _key: False}[ui.screen]
        try:
            result = handler(key)
        except HubiError as e:
            self.say(pl(e), "bad")
            return None
        if result is False:
            shown = key if len(key) == 1 else {"enter": "Enter", "tab": "Tab"}.get(key, key)
            self.say(f"Klawisz „{display(shown)}” nic tu nie robi; ? = pomoc" if ui.screen != "help" else "0 = wróć", "muted")
            return None
        return result if isinstance(result, int) and not isinstance(result, bool) else None

    def loop(self):
        while True:
            self.refresh()
            self.draw()
            idle = time.monotonic()-self.touched
            key = self.reader.key(self.app.c.ui_refresh if idle < 60 else max(10, self.app.c.ui_refresh))
            if key is None:
                continue
            code = self.handle(key)
            if code is not None:
                return code


def run(app):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise HubiError("Menu requires a TTY; use list --json, start, stop or doctor")
    if os.environ.get("HUBI6_ACTIVE"):
        raise HubiError("Hubi is already active")
    with terminal(paste=True):
        read_end, write_end = os.pipe()
        os.set_blocking(write_end, False)
        previous = signal.getsignal(signal.SIGWINCH)

        def resized(signum, frame):
            try:
                os.write(write_end, b"r")
            except OSError:
                pass

        signal.signal(signal.SIGWINCH, resized)
        reader = Keys(sys.stdin.fileno(), wake=read_end)
        screen = Screen()
        screen.enter()
        try:
            reader.arm()
            return Menu(app, reader, screen).loop()
        finally:
            screen.leave()
            signal.signal(signal.SIGWINCH, previous)
            os.close(read_end)
            os.close(write_end)
