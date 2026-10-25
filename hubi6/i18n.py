"""Polish presentation layer for human-facing text.

Raised errors and diagnostics keep their stable English wording: logs, tests and
the JSON interface depend on it. Only what a person reads in the menu or on the
CLI is translated here, at the moment of display. An untranslated message is
shown unchanged, never hidden.
"""
import os
import pwd
import re

from .core.model import DEFAULT_INSTANCE, DEFAULT_NAME, LEGACY_DEFAULT_NAME, RepoHealth, State, display_name


STATE_PL = {
    State.STOPPED: "zatrzymany", State.STARTING: "startuje", State.RUNNING: "działa",
    State.ATTACHED: "podpięty", State.EXITED: "zakończony", State.ORPHANED: "osierocony",
    State.STALE: "nieaktualny", State.UNKNOWN: "nieznany",
}
HEALTH_PL = {
    RepoHealth.UNKNOWN: "nieznane", RepoHealth.OK: "OK", RepoHealth.MISSING: "brak repo",
    RepoHealth.REPLACED: "repo podmienione", RepoHealth.INVALID: "repo uszkodzone",
}
RUNTIME_PL = {"OK": "OK", "DOWN": "nie działa", "DEGRADED": "niepełny", "UNKNOWN": "nieznany"}
KIND_PL = {"claude": "Claude", "codex": "Codex", "terminal": "terminal"}

EXACT = {
    # Git adapter
    "Invalid Git HEAD": "Nieprawidłowy HEAD w repozytorium Git",
    "Malformed Hubi workspace identity": "Uszkodzony identyfikator workspace Hubi",
    "Repository changed while pinning workspace identity": "Repozytorium zmieniło się podczas nadawania identyfikatora workspace",
    "Not a Git working directory": "To nie jest katalog roboczy Git",
    "Missing Git HEAD": "Brak pliku HEAD w repozytorium Git",
    "Repository missing, invalid, replaced or outside repo root": "Repozytorium nie istnieje, jest uszkodzone, podmienione albo leży poza katalogiem repozytoriów",
    "Select the Git repository root": "Wskaż katalog główny repozytorium Git",
    "Invalid .git file": "Nieprawidłowy plik .git",
    "Repository is unavailable": "Repozytorium jest niedostępne",
    "Repository escapes repo root": "Repozytorium wychodzi poza katalog repozytoriów",
    # home workspace (work outside any repository)
    "Home workspace is disabled in the configuration": "Katalog domowy (bez repo) jest wyłączony w konfiguracji: home_workspace",
    "Home workspace is unavailable": "Katalog domowy (bez repo) jest niedostępny",
    "Home workspace is not a directory": "Katalog domowy (bez repo) nie jest katalogiem",
    "Home workspace changed or is not the configured one": "Katalog domowy zmienił się albo nie jest tym z konfiguracji",
    "The home workspace is not a Git repository": "Katalog domowy nie jest repozytorium Git",
    "home_workspace must be a path string": "home_workspace musi być ścieżką zapisaną jako tekst",
    "home_workspace must be outside repos_root": "home_workspace musi leżeć poza katalogiem repozytoriów (repos_root)",
    "Repository name is empty": "Pusta nazwa repozytorium (katalog domowy podaj jawnie jako ~)",
    "An agent needs an explicit repository or ~": "Agent wymaga jawnie podanego repozytorium albo ~ (katalog domowy)",
    # the Codex limit guard (hubi guard)
    "Guard sessions are Hubi names like codex/praca, not pane numbers": "Strażnik przyjmuje nazwy Hubi, np. codex/praca, a nie numery paneli",
    "The contact must be a Claude session or another named Codex session": "Kontakt to sesja Claude albo inna sesja Codexa z własną nazwą, np. codex/praca/temp-rev",
    "Unusual guard restart options; change the guard by hand": "Nietypowe opcje restartu strażnika; zmień go ręcznie",
    "Saved guard setup is unreadable": "Zapisane ustawienie strażnika jest nieczytelne",
    "The guard unit was started some other way; change it by hand": "Strażnik został uruchomiony inaczej niż zwykle; zmień go ręcznie",
    "Another guard change is in progress; try again": "Trwa inna zmiana strażnika; spróbuj za chwilę",
    "Guard is not configured: no watchdog.sh in the guard directory": "Strażnik nie jest skonfigurowany: brak watchdog.sh w katalogu strażnika",
    "Invalid guard unit name in the configuration": "Nieprawidłowa nazwa jednostki strażnika w konfiguracji",
    "The guard is already running; use cp, who or version to change it": "Strażnik już działa; zmienisz go poleceniem cp, who albo version",
    "No earlier guard setup known; give CP, Codex and contact": "Nie znam wcześniejszego ustawienia strażnika; podaj CP, Codexa i kontakt",
    "The guard PATH has no codex; start Hubi from a login shell": "W PATH strażnika nie ma codex; uruchom Hubi z powłoki logowania",
    "watchdog.sh matches no kept watchdog.v*.sh; save it as a version first": "watchdog.sh nie odpowiada żadnej zachowanej watchdog.v*.sh; najpierw zapisz go jako wersję",
    "Give the 5-hour pause and the weekly stop, e.g. 7 1": "Podaj pauzę 5 h i stop tygodniowy w %, np. 7 1",
    "Give the thresholds to change": "Podaj progi do zmiany",
    "Give CP, Codex and contact, or nothing": "Podaj CP, Codexa i kontakt albo nic (wtedy ostatnie ustawienie)",
    "Give Codex and contact": "Podaj Codexa i kontakt, np. codex/praca claude/praca",
    "Unknown guard configuration keys": "Nieznane klucze w sekcji guard konfiguracji",
    "Invalid guard configuration": "Nieprawidłowa sekcja guard konfiguracji",
    # systemd adapter
    "Malformed systemd inventory: missing or duplicate unit identity": "Nieprawidłowa lista jednostek systemd: brak lub duplikat identyfikatora",
    "Refusing foreign systemd unit": "Odmowa: obca jednostka systemd",
    "Systemd state UNKNOWN; operation refused": "Stan systemd nieznany; operacja odrzucona",
    "Scope state UNKNOWN": "Stan scope nieznany",
    "Unsupported signal": "Nieobsługiwany sygnał",
    "Scope generation/ownership changed; operation refused": "Generacja lub własność scope zmieniła się; operacja odrzucona",
    "Scope cgroup identity mismatch": "Niezgodna tożsamość cgroup scope",
    # tmux adapter
    "tmux returned an invalid pane identity": "tmux zwrócił nieprawidłowy identyfikator panelu",
    "Cannot verify tmux session for removal": "Nie można zweryfikować sesji tmux do usunięcia",
    "Cannot verify primary pane/session for repair": "Nie można zweryfikować głównego panelu/sesji do naprawy",
    "Session ownership mismatch; use reconcile": "Niezgodna własność sesji; użyj naprawy (reconcile)",
    "Unknown attach mode": "Nieznany tryb podpięcia",
    "Malformed tmux inventory row": "Nieprawidłowy wiersz listy tmux",
    "Invalid tmux server identity": "Nieprawidłowa tożsamość serwera tmux",
    "Malformed tmux pane inventory": "Nieprawidłowa lista paneli tmux",
    "tmux unavailable": "tmux niedostępny",
    "tmux inventory unavailable": "Lista tmux niedostępna",
    "systemd inventory unavailable": "Lista systemd niedostępna",
    # configuration
    "No usable interactive shell": "Brak działającej powłoki interaktywnej",
    "Worker journal must use a canonical absolute path": "Dziennik workera musi mieć kanoniczną ścieżkę bezwzględną",
    "Worker journal layout invalid": "Nieprawidłowy układ dziennika workera",
    "Worker paths do not match runtime identity": "Ścieżki workera nie zgadzają się z tożsamością runtime",
    "Unknown configuration keys": "Nieznane klucze konfiguracji",
    "repos_root must be a path string": "repos_root musi być ścieżką zapisaną jako tekst",
    "XDG_RUNTIME_DIR must identify this UID's /run/user directory": "XDG_RUNTIME_DIR musi wskazywać katalog /run/user tego użytkownika",
    "Unknown github configuration keys": "Nieznane klucze konfiguracji github",
    "Invalid GitHub configuration": "Nieprawidłowa konfiguracja GitHub",
    "Unknown stop configuration keys": "Nieznane klucze konfiguracji stop",
    "Unknown ui configuration keys": "Nieznane klucze konfiguracji ui",
    "Invalid ui configuration": "Nieprawidłowa konfiguracja ui",
    "agents must contain nonempty argv arrays for codex, claude or terminal": "agents musi zawierać niepuste tablice argv dla codex, claude lub terminal",
    # model
    "Instance must match [A-Za-z0-9][A-Za-z0-9_-]{0,31}": "Nazwa sesji: litery, cyfry, _ i -, bez spacji, najwyżej 32 znaki, na początku litera lub cyfra",
    "Paths containing control characters are unsupported": "Ścieżki ze znakami sterującymi nie są obsługiwane",
    "Unknown workload type": "Nieznany typ sesji",
    "Invalid runtime/workload identity": "Nieprawidłowa tożsamość runtime/sesji",
    "Malformed or foreign workload metadata": "Uszkodzone lub obce metadane sesji",
    "Malformed workload metadata": "Uszkodzone metadane sesji",
    # GitHub
    "Expected GitHub owner/repository": "Oczekiwano nazwy GitHub w postaci właściciel/repozytorium",
    "Atomic no-replace rename requires Linux renameat2": "Atomowa zmiana nazwy bez nadpisania wymaga renameat2 (Linux)",
    "GitHub returned invalid JSON": "GitHub zwrócił nieprawidłowy JSON",
    "No GitHub cache available offline": "Brak zapisanej listy GitHub do pracy offline",
    "Foreign clone unit refused": "Odmowa: obca jednostka klonowania",
    "Invalid repository metadata": "Nieprawidłowe metadane repozytorium",
    "GitHub discovery failed; check gh auth status or try offline cache": "Nie udało się pobrać listy z GitHuba; sprawdź `gh auth status` albo użyj zapisanej listy",
    "Repository is already LOCAL": "Repozytorium jest już sklonowane lokalnie",
    "Destination exists; nothing overwritten": "Katalog docelowy już istnieje; niczego nie nadpisano",
    "GitHub discovery limit/timeout reached; refusing a truncated catalogue": "Przekroczony limit lub czas pobierania listy z GitHuba; odrzucam niepełną listę",
    "Another clone is in progress": "Trwa już inne klonowanie",
    "Cloned origin does not match requested repository": "Origin sklonowanego repozytorium nie zgadza się z żądanym",
    "Invalid GitHub pagination response": "Nieprawidłowa odpowiedź stronicowania GitHub",
    "GitHub unavailable; showing saved catalogue": "GitHub niedostępny; pokazuję zapisaną listę",
    # inventory
    "Inconsistent metadata or generation; use reconcile": "Niespójne metadane lub generacja; użyj naprawy",
    "Session name/metadata mismatch": "Nazwa sesji nie zgadza się z metadanymi",
    "Scope identity mismatch": "Niezgodna tożsamość scope",
    "Extra panes: explicit reconcile repairs this exact session before stopping": "Dodatkowe panele: przed zatrzymaniem napraw tę sesję",
    # lifecycle
    "Runtime unavailable or unowned; run hubi doctor": "Runtime niedostępny albo nie należy do Hubi; uruchom `hubi doctor`",
    "tmux server cgroup/UID mismatch": "Niezgodny cgroup/UID serwera tmux",
    "Runtime generation changed": "Zmieniła się generacja runtime",
    "Full-cgroup kill support unavailable": "Brak obsługi zabijania całego cgroup",
    "Runtime service must use Restart=always": "Usługa runtime musi mieć Restart=always",
    "Workload ID requires at least 8 hexadecimal characters; use hubi list": "ID sesji wymaga co najmniej 8 znaków szesnastkowych; zobacz `hubi list`",
    "Workload ID missing or ambiguous; use hubi list": "Nie ma takiego ID sesji albo jest niejednoznaczne; zobacz `hubi list`",
    "Workload generation changed; stale confirmation refused": "Generacja sesji zmieniła się; nieaktualne potwierdzenie odrzucone",
    "Systemd state UNKNOWN; stop refused": "Stan systemd nieznany; zatrzymanie odrzucone",
    "Cannot confirm session cleanup; keeping receipt": "Nie można potwierdzić usunięcia sesji; zostawiam zapis",
    "Scope reappeared; preserving receipt": "Scope pojawił się ponownie; zostawiam zapis",
    "Attach from inside a Hubi workload is refused; detach first": "Nie można podpinać się z wnętrza sesji Hubi; najpierw się odepnij (Ctrl-b d)",
    "Attach requires a terminal": "Podpięcie wymaga terminala",
    "Cannot verify tmux server ownership": "Nie można zweryfikować własności serwera tmux",
    "A terminal has no conversation to resume": "Terminal nie ma rozmowy do wznowienia",
    "tmux ownership UNKNOWN; stop refused": "Własność tmux nieznana; zatrzymanie odrzucone",
    "Cannot recover scope ownership": "Nie można odzyskać własności scope",
    "Scope did not stop; keeping metadata and session": "Scope się nie zatrzymał; zostawiam metadane i sesję",
    "Remove manually created extra panes before stopping this session": "Usuń ręcznie utworzone dodatkowe panele przed zatrzymaniem tej sesji",
    "State UNKNOWN; inspect/reconcile before stop": "Stan nieznany; przed zatrzymaniem obejrzyj lub napraw sesję",
    "Workload cannot be attached in its current state": "W obecnym stanie nie można wejść do tej sesji",
    "Workload already has clients; select --mode view/shared/takeover": "Sesja ma już podpiętego klienta; wybierz podgląd, dołączenie albo przejęcie (--mode view/shared/takeover)",
    "Previous checkout still has a workload; stop it by ID": "Poprzedni checkout wciąż ma sesję; zatrzymaj ją po ID",
    "Orphan requires explicit stop/reconcile by ID": "Osierocona sesja wymaga jawnego zatrzymania lub naprawy po ID",
    "Extra panes require explicit reconcile before stop": "Dodatkowe panele wymagają naprawy przed zatrzymaniem",
    "Scope receipt changed during pane repair": "Potwierdzenie scope zmieniło się podczas naprawy paneli",
    "Inconsistent workload; use reconcile by ID": "Niespójna sesja; użyj naprawy po ID",
    "Scope receipt does not match the exact repair generation": "Potwierdzenie scope nie zgadza się z naprawianą generacją",
    "Start in progress; retry shortly": "Start w toku; spróbuj za chwilę",
    "Cannot verify scope receipt for pane repair": "Nie można zweryfikować potwierdzenia scope do naprawy paneli",
    # storage
    "Unsafe metadata file": "Niebezpieczny plik metadanych",
    "Invalid lock identity": "Nieprawidłowy identyfikator blokady",
    "Unsafe lifecycle lock": "Niebezpieczna blokada cyklu życia",
    "Metadata filename disagrees with identity": "Nazwa pliku metadanych nie zgadza się z tożsamością",
    "Workload busy: lifecycle lock timed out": "Sesja zajęta: nie doczekano blokady cyklu życia",
    # menus and worker
    "Menu requires a TTY; use list --json, start, stop or doctor": "Menu wymaga terminala; użyj list --json, start, stop albo doctor",
    "Hubi is already active": "Hubi już działa w tej sesji; odepnij się (Ctrl-b d), żeby wrócić do menu",
    "Choose new or resume": "Wybierz new albo resume",
    "Invalid worker mode": "Nieprawidłowy tryb workera",
    "Worker scope ownership could not be verified": "Nie można zweryfikować własności scope workera",
    "Repository changed before scope creation": "Repozytorium zmieniło się przed utworzeniem scope",
    "Repository changed before exec": "Repozytorium zmieniło się przed uruchomieniem",
    "Repository was replaced during startup": "Repozytorium zostało podmienione w trakcie startu",
    "Launcher umask unavailable": "Nie można odczytać umask procesu uruchamiającego",
    "Invalid session umask": "Nieprawidłowy umask sesji",
    # pane resolver
    "Runtime state UNKNOWN; cannot resolve the pane": "Stan runtime nieznany; nie można ustalić panelu",
    "Several running generations; use hubi list": "Działa kilka generacji tej sesji; zobacz `hubi list`",
    "No such session; use hubi list": "Nie ma takiej sesji; zobacz `hubi list`",
    "Runtime is not running; run hubi doctor": "Runtime Hubi nie działa; uruchom `hubi doctor`",
    "Message is empty": "Pusta wiadomość",
    "Message is longer than 64 KiB": "Wiadomość jest dłuższa niż 64 KiB",
    "Message contains control characters": "Wiadomość zawiera znaki sterujące (dozwolone są tylko nowa linia i tabulator)",
    "Message missing: use -m TEXT or standard input": "Brak wiadomości: podaj -m TEKST albo przekaż ją na standardowe wejście",
    "Session is scrolled back (copy mode); retry shortly": "Sesja jest przewijana (tryb kopiowania); nic nie wysłano, ponów za chwilę",
    "Message pasted but not submitted: the session was scrolled back": "Wiadomość wklejona, ale niewysłana: sesję właśnie przewinięto",
    "Cannot read the pane mode": "Nie można odczytać trybu panelu tmux",
    "Saved conversation of this session is unreadable; start a new conversation":
        "Zapisana rozmowa tej sesji jest nieczytelna; uruchom nową rozmowę",
}

# English template -> Polish template. Named fields are copied verbatim.
TEMPLATES = (
    ("Invalid repository {name}: broken symlink", "Nieprawidłowe repozytorium {name}: zerwany symlink"),
    ("Invalid repository {name}: {why}", "Nieprawidłowe repozytorium {name}: {why}"),
    ("Cannot read repo root: {why}", "Nie można odczytać katalogu repozytoriów: {why}"),
    ("{program} failed (exit {code})", "{program}: błąd (kod {code})"),
    ("Require an owned directory with mode 0700: {path}", "Wymagany własny katalog z uprawnieniami 0700: {path}"),
    ("{name} must be a positive number up to 86400 seconds", "{name} musi być liczbą dodatnią do 86400 sekund"),
    ("{name} must be an absolute path", "{name} musi być ścieżką bezwzględną"),
    ("Private runtime directory unavailable: {path}", "Prywatny katalog runtime niedostępny: {path}"),
    ("Config file does not exist: {path}", "Plik konfiguracji nie istnieje: {path}"),
    ("Invalid config: {detail}", "Nieprawidłowa konfiguracja: {detail}"),
    ("Invalid {name} path", "Nieprawidłowa ścieżka {name}"),
    ("{name} must be a TOML table", "{name} musi być tabelą TOML"),
    ("Cannot create private directory: {path}", "Nie można utworzyć prywatnego katalogu: {path}"),
    ("Invalid lifecycle transition: {detail}", "Nieprawidłowe przejście cyklu życia: {detail}"),
    ("Atomic clone publish refused (errno {code}); nothing overwritten", "Publikacja klona odrzucona (errno {code}); niczego nie nadpisano"),
    ("GitHub clone failed (exit {code}): {detail}", "Klonowanie z GitHuba nie powiodło się (kod {code}): {detail}"),
    ("Clone helper unavailable: {detail}", "Pomocnik klonowania niedostępny: {detail}"),
    ("Clone timed out after {seconds} s", "Klonowanie przerwane po {seconds} s (limit czasu)"),
    ("Unverified session {name}; inspect its exact identity manually (no surviving receipt)",
     "Niezweryfikowana sesja tmux {name}; sprawdź ją ręcznie (brak potwierdzenia własności)"),
    ("Unverified scope {name}; no destructive action allowed", "Niezweryfikowany scope {name}; żadna destrukcyjna akcja nie jest dozwolona"),
    ("Creation requires Linger=yes; run loginctl enable-linger {user}", "Uruchamianie wymaga Linger=yes; wykonaj: loginctl enable-linger {user}"),
    ("Agent executable unavailable: {name}", "Brak programu agenta: {name}"),
    ("Start acknowledgement timed out; workload remains inspectable: {ident}", "Nie doczekano potwierdzenia startu; sesję można obejrzeć: {ident}"),
    ("Moved workspace already has a workload: {ident}; stop it before starting here", "Przeniesiony workspace ma już sesję: {ident}; zatrzymaj ją przed startem tutaj"),
    ("Workload EXITED: {ident}; stop/reconcile it before creating a new generation", "Sesja zakończona: {ident}; usuń ją przed uruchomieniem nowej"),
    ("Workload startup failed; inspect {ident}", "Start sesji nie powiódł się; obejrzyj {ident}"),
    ("Workload is {state}, not running: {ident}", "Sesja nie działa ({state}): {ident}"),
    ("Workload failed or EXITED during startup; inspect/stop {ident}", "Sesja zakończyła się w trakcie startu; obejrzyj lub usuń {ident}"),
    ("{name}: {error}; reconcile using a surviving runtime receipt, otherwise inspect/move this file manually",
     "{name}: {error}; napraw sesję na podstawie zachowanego potwierdzenia albo obejrzyj/przenieś ten plik ręcznie"),
    ("Invalid guard session name: {name}", "Nieprawidłowa nazwa sesji dla strażnika: {name} (np. codex/praca, claude/praca)"),
    ("Invalid guard version: {name}", "Nieprawidłowa wersja strażnika: {name} (np. v4.8)"),
    ("Invalid checkpoint name: {name}", "Nieprawidłowa nazwa CP: {name} (np. CP21)"),
    ("Unusual guard setting {name}; change the guard by hand", "Nietypowe ustawienie strażnika {name}; zmień go ręcznie"),
    ("The 5-hour pause threshold must be a whole number 0-100, without %: {value}",
     "Próg pauzy 5 h to liczba całkowita 0–100, bez %: {value}"),
    ("The weekly stop threshold must be a whole number 0-100, without %: {value}",
     "Próg stopu tygodniowego to liczba całkowita 0–100, bez %: {value}"),
    ("Idle minutes must be a whole number 1-10080: {value}", "Minuty bezczynności to liczba całkowita 1–10080: {value}"),
    ("Max hours must be a whole number 1-720: {value}", "Najdłuższy czas pracy to liczba godzin 1–720: {value}"),
    ("No such guard version: {name}", "Nie ma takiej wersji strażnika: {name}"),
    ("The guard did not stop; check systemctl --user status {unit}", "Strażnik się nie zatrzymał; sprawdź systemctl --user status {unit}"),
    ("The guard stopped right after start: {why}", "Strażnik zakończył się zaraz po starcie: {why}"),
    ("Weekly limit lock in state.{key}: when the limit is back, send Codex \"{cp} wznów\", delete that file and start the guard again",
     "Blokada tygodniowa w pliku state.{key}: gdy limit wróci, wpisz Codexowi „{cp} wznów”, usuń ten plik i włącz strażnika ponownie"),
    ("Guard is in {gstate} for {codex}: a new CP or Codex would lose the pause; wait for RUNNING or restart with the same CP and Codex",
     "Strażnik jest w stanie {gstate} dla {codex}: nowy CP albo inny Codex zgubiłby stan pauzy; poczekaj na RUNNING albo uruchom ponownie z tym samym CP i celem"),
    ("Guard stopped; the new start failed: {cause}; going back failed too: {cause_back}. Previous setup: hubi guard start {words}",
     "Strażnik zatrzymany: nowy start się nie udał ({cause}), powrót też nie ({cause_back}). Poprzednie ustawienie: hubi guard start {words}"),
    ("The new guard setup did not start: {cause}; the guard runs again with {words}",
     "Nowe ustawienie strażnika nie wystartowało: {cause}; strażnik działa znowu z {words}"),
    ("Could not replace watchdog.sh ({error}); the guard runs as before",
     "Nie udało się podmienić watchdog.sh ({error}); strażnik działa jak wcześniej"),
    ("Could not replace watchdog.sh ({error})", "Nie udało się podmienić watchdog.sh ({error})"),
    ("Guard version {name} did not start: {cause}; back to {previous}, the guard runs",
     "Wersja {name} nie przyjęła tego ustawienia: {cause}; wrócono do {previous}, strażnik działa"),
    ("{program}: TimeoutExpired", "{program}: przekroczony limit czasu"),
    ("{program}: FileNotFoundError", "{program}: nie znaleziono programu"),
    ("{program}: PermissionError", "{program}: brak uprawnień"),
)


def _compile(template):
    parts = re.split(r"\{(\w+)\}", template)
    pattern = "".join(re.escape(part) if index % 2 == 0 else f"(?P<{part}>.+?)" for index, part in enumerate(parts))
    return re.compile(pattern, re.S)


_COMPILED = tuple((_compile(source), target) for source, target in TEMPLATES)+(
    # Runner failures: "<helper>: <ExceptionName>". Narrow on purpose, so it can
    # never swallow an unrelated "a: b" message.
    (re.compile(r"(?P<program>[\w.+-]+): (?P<error>[A-Z]\w*)"), "{program}: nie udało się uruchomić ({error})"),
)


def pl(message):
    """Polish wording for a known message; unknown text is returned unchanged."""
    text = str(message)
    known = EXACT.get(text)
    if known is not None:
        return known
    for pattern, target in _COMPILED:
        match = pattern.fullmatch(text)
        if match:
            fields = match.groupdict()
            if "state" in fields and fields["state"] in State.__members__:
                fields["state"] = STATE_PL[State[fields["state"]]]   # the word `hubi list` shows
            for name in [n for n in fields if n.startswith("cause")]:
                fields[name] = pl(fields[name])                    # a nested error, itself translated
            return target.format(**fields)
    return text


def place(ref):
    """What a person calls a workspace: the repository name, or ~ for the home directory."""
    if ref.plain:
        try:
            if ref.path == os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir):
                return "~"
        except KeyError:
            pass
    return os.path.basename(ref.path.rstrip("/")) or ref.path


def name_pl(instance):
    """What a person sees for an instance name."""
    return display_name(instance)


def instance_of(name):
    """The instance a typed session name means; empty, "1" and the old "główna" map to the default."""
    text = str(name).strip()
    return DEFAULT_INSTANCE if not text or text.casefold() in (DEFAULT_NAME, LEGACY_DEFAULT_NAME) else text


def age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d}"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def plural(count, one, few, many):
    """Polish plural forms: 1 klient, 2 klienty, 5 klientów."""
    if count == 1:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many
