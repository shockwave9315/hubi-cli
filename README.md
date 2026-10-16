# Hubi

Menu po SSH do pracy z agentami w repozytoriach. Uruchamia Claude Code, Codexa
i zwykłe terminale tak, żeby przeżywały rozłączenie: każda sesja działa we
własnym scope `systemd --user` i własnej sesji tmux, a SSH i samo menu można w
każdej chwili zamknąć.

```text
 HUBI v6  ● runtime OK                                      00:24

 AGENCI I TERMINALE  3
 1  ✳  claude/główna     hubi-cli        pracuje ◐         18 min
       Poprawka menu
 2  >_ codex/cp5         praca           czeka             2 h 05
 3  $  terminal/główna   praca           działa             6 min

 REPOZYTORIA  7
 ·  hubi-cli              main             ✳ główna
 ·  praca                 cp5-timer-trip   >_ cp5  $ główna
 ───────────────────────────────────────────────────────────────
 1-9 wejdź  l Claude  c Codex  t terminal  d zatrzymaj
 i szczegóły  r repo  g GitHub  ? pomoc  q shell  x rozłącz
```

Aktualna wersja to **v6**: Python 3.11+, sama biblioteka standardowa, pakiet
`hubi6`. Skrypt `hubi` w katalogu głównym to poprzednia wersja v5 w Bashu,
zostawiona na wypadek wycofania zmian ([opis v5](docs/v5/README.md)).

## Co potrafi

- Sesje przeżywają zerwanie SSH, zamknięcie menu i odpięcie klienta tmux.
- Jeden klawisz na akcję, bez Entera. Mieści się na ekranie telefonu (65 kolumn).
- Lista pokazuje, który agent pracuje, a który czeka, razem z tematem rozmowy.
- Zatrzymanie sesji kończy cały jej cgroup, więc nie zostają procesy potomne.
- Po `exit` albo `/exit` wracasz do menu. Czyste wyjście sprząta się samo,
  wyjście z błędem zostaje do obejrzenia.
- Lista repozytoriów z GitHuba z klonowaniem.
- Terminal albo agent poza repozytorium, w katalogu domowym (`h`): trwała
  powłoka do zwykłej pracy na devboksie, do której wracasz jak do każdej sesji.

Restart maszyny kończy sesje. Rozmowę z agentem wznowisz potem klawiszem `r`
przy starcie.

## Wymagania

- Linux z `systemd --user` i włączonym linger: `loginctl enable-linger $USER`
- Python 3.11 lub nowszy, tmux, git
- `claude` lub `codex` w PATH
- `gh` z zalogowanym kontem, jeśli chcesz listę z GitHuba

## Instalacja

Jako zwykły użytkownik, bez sudo:

```console
git clone https://github.com/shockwave9315/hubi-cli ~/repos/hubi-cli
cd ~/repos/hubi-cli
make build
python3 dist/hubi6/install.py install --as-hubi
hubi doctor
```

`hubi doctor` ma pokazać `PASS`. Instalator zakłada:

| Co | Gdzie |
|---|---|
| polecenia `hubi` i `hubi6` | `~/.local/bin` |
| pakiet | `~/.local/lib/hubi6` |
| konfiguracja tmux | `~/.config/hubi6/tmux.conf` |
| usługa | `hubi6-tmux.service` (użytkownika) |

Bez `--as-hubi` powstaje tylko polecenie `hubi6`. Instalator odmawia, gdy bundle
zbudowano z niezacommitowanych zmian, gdy działa jeszcze v5 albo gdy istnieje
jakakolwiek sesja v6.

### Aktualizacja

Zatrzymaj wszystkie sesje (`d` w menu), potem:

```console
cd ~/repos/hubi-cli
git pull
make build
python3 dist/hubi6/install.py install --as-hubi
hubi doctor
```

### Odinstalowanie

```console
python3 dist/hubi6/install.py uninstall
```

Też wymaga zatrzymanych sesji. Usuwa pakiet, usługę, konfigurację i stan Hubi,
a jeśli instalator zastąpił wcześniejsze `hubi`, przywraca je. Repozytoriów nie
rusza.

### Menu od razu po zalogowaniu przez SSH

Dopisz zawartość [`config/hubi6-autologin.sh`](config/hubi6-autologin.sh) na
**końcu `~/.profile`**. Nie do `~/.bashrc`, bo sesje dostałyby niepełny PATH.
Wtedy `q` wychodzi do powłoki, a `x` kończy połączenie.

Ominięcie menu: `ssh -t HOST 'HUBI_NOAUTO=1 bash -il'`.

## Menu

| Klawisz | Działanie |
|---|---|
| `1`-`9` | wejdź do sesji |
| `l` `c` `t` | nowa sesja: Claude / Codex / terminal, potem numer repo albo `h` |
| `h` | katalog domowy `~`: sesje poza repozytorium |
| `d` | zatrzymaj: numer sesji, potem `q` potwierdza |
| `i` | szczegóły sesji: podgląd, współdzielenie, przejęcie, naprawa |
| `r` | widok repozytorium i `git status` |
| `g` | repozytoria na GitHubie, klonowanie |
| `?` | pomoc |
| `q` / `x` | powłoka / rozłącz (`x` tylko po autologinie) |
| `0`, Esc | wróć |

`l`, `c` i `t` zawsze startują nową sesję. Do działającej wchodzisz cyfrą.
Pierwsza sesja danego typu w repo nazywa się `główna`, kolejne dostają numer:
`codex/główna`, `codex/2`, `codex/3`.

Przy wyborze repo:

| Klawisz | Działanie |
|---|---|
| `r` | przełącza między nową rozmową a wznowieniem (lista wcześniejszych rozmów) |
| `n` | własna nazwa sesji zamiast automatycznej, np. `codex/cp5` |

W sesji:

| Klawisz | Działanie |
|---|---|
| `Ctrl-b d` | wróć do menu, sesja działa dalej |
| `Ctrl-b )` / `Ctrl-b (` | następna / poprzednia sesja |
| `Ctrl-b L` | ostatnio oglądana sesja |

## Polecenia

```console
hubi                          # menu
hubi claude REPO [NAZWA]      # uruchom w razie potrzeby i wejdź
hubi codex REPO --resume      # to samo dla Codexa, ze wznowieniem rozmowy
hubi terminal REPO            # terminal w repo
hubi terminal                 # trwały terminal w katalogu domowym, bez repo
hubi claude ~ [NAZWA]         # agent w katalogu domowym
hubi list                     # sesje i ich ID
hubi stop ID                  # zatrzymaj sesję
hubi inspect ID               # szczegóły (JSON)
hubi github list              # repozytoria na GitHubie
hubi doctor                   # diagnostyka
hubi --classic                # starsze menu liniowe, awaryjne
```

`REPO` to nazwa katalogu w `~/repos` albo ścieżka; `~` oznacza katalog domowy
(w `hubi claude|codex|terminal` także brak `REPO`). Opcje każdego polecenia
pokazuje `hubi POLECENIE --help`.

## Konfiguracja

Plik `~/.config/hubi6/config.toml` jest opcjonalny. Wzór z opisem:
[`config/hubi6.example.toml`](config/hubi6.example.toml).

| Klucz | Domyślnie | Znaczenie |
|---|---|---|
| `repos_root` | `~/repos` | gdzie szukać repozytoriów |
| `home_workspace` | `~` | katalog dla sesji poza repozytorium; `""` wyłącza |
| `[agents] claude` | `["claude", "--permission-mode", "bypassPermissions"]` | polecenie Claude |
| `[agents] codex` | `["codex", "--no-daemon"]` | polecenie Codexa |
| `[ui] refresh` | `2` | odświeżanie listy w sekundach |
| `[ui] icons` | `true` | `false` = sam ASCII |

Codex startuje z `--no-daemon` celowo. Bez tej flagi wszystkie sesje Codexa
dzielą jeden proces w tle, który siedzi w pierwszej z nich, i zatrzymanie jej
psuje pozostałe.

## Gdy coś nie działa

| Objaw | Co zrobić |
|---|---|
| nie wiadomo, czego brakuje | `hubi doctor` |
| menu się nie otwiera | `hubi --classic` |
| autologin blokuje logowanie | `ssh -t HOST 'HUBI_NOAUTO=1 bash -il'` |
| sesja w dziwnym stanie | `i` w menu i naprawa, albo `hubi reconcile ID` |
| potrzebny log | `~/.local/state/hubi6/hubi6.log` |

## Rozwój

```console
make test                           # testy jednostkowe
sudo -n python3 tools/test_v6.py    # pełny zestaw na jednorazowym użytkowniku
```

Testy runtime zakładają osobnego użytkownika systemowego i po sobie sprzątają.
Nie dotykają sesji na twoim koncie. Zasady przeglądu:
[`REVIEW_POLICY.md`](REVIEW_POLICY.md).

Więcej:

- [`docs/v6/README.md`](docs/v6/README.md): architektura, model własności,
  bezpieczeństwo wejścia
- [`docs/v6/CUTOVER.md`](docs/v6/CUTOVER.md): instalacja krok po kroku, przejście
  z v5, wycofanie
