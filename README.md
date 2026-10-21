# Hubi

Menu po SSH do pracy z agentami w repozytoriach. Uruchamia Claude Code, Codexa
i zwykłe terminale tak, żeby przeżywały rozłączenie: każda sesja działa we
własnym scope `systemd --user` i własnej sesji tmux, a SSH i samo menu można w
każdej chwili zamknąć.

```text
 HUBI v6  ● runtime OK                                      00:24

 AGENCI I TERMINALE  3
 1  ✳  claude/1          hubi-cli        pracuje ◐         18 min
       Poprawka menu
 2  >_ codex/cp5         praca           czeka             2 h 05
 3  $  terminal/1        praca           działa             6 min

 REPOZYTORIA  7
 ·  hubi-cli              main             ✳ 1
 ·  praca                 cp5-timer-trip   >_ cp5  $ 1
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
- Pliki tworzone w sesji dostają takie uprawnienia jak w shellu, z którego ją
  uruchomiono (ten sam `umask`).
- Zatrzymanie sesji kończy cały jej cgroup, więc nie zostają procesy potomne.
- Po `exit` albo `/exit` wracasz do menu. Terminal po `exit` sprząta się sam,
  z dowolnym kodem wyjścia (menu podaje kod, jeśli nie był 0). Agent zakończony
  błędem zostaje do obejrzenia, tak samo jak terminal zabity sygnałem albo taki,
  który padł już przy starcie.
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
Sesje danego typu w repo dostają numery: `codex/1`, `codex/2`, `codex/3`.
Numer nie oznacza ważności. Sesji z rolą daj nazwę (`n`), np. `claude/rev`.

Każda nazwa sesji Claude ma swoją rozmowę. Nowa rozmowa dostaje stały
identyfikator, a „wznów” wraca dokładnie do niej, nie do innej. Co pracowało w
`claude/rev`, po restarcie wraca do `claude/rev`. Gdy w repo jest kilka
zatrzymanych sesji z rozmowami, menu pyta, którą wznowić (po nazwie, bez
domyślnej). Po `/clear` w Claude albo wyborze innej rozmowy z jego listy Hubi
przepina nazwę na bieżącą rozmowę, gdy wychodzisz z sesji i gdy ją zatrzymujesz.
Lista rozmów Claude pojawia się tylko dla nazwy, która nie ma jeszcze zapisanej
rozmowy. Codex na razie zawsze pokazuje swoją listę.

Przy wyborze repo:

| Klawisz | Działanie |
|---|---|
| `r` | przełącza między nową rozmową a wznowieniem rozmowy tej sesji |
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
hubi send TYP REPO [NAZWA] -m TEKST  # wyślij wiadomość do sesji po nazwie
hubi pane TYP REPO [NAZWA]    # panel tmux działającej sesji, np. %14
hubi github list              # repozytoria na GitHubie
hubi doctor                   # diagnostyka
hubi --classic                # starsze menu liniowe, awaryjne
```

`REPO` to nazwa katalogu w `~/repos` albo ścieżka; `~` oznacza katalog domowy.
Brak `REPO` oznacza katalog domowy tylko w `hubi terminal`: agent (`claude`,
`codex`) działa tam na całym katalogu domowym, więc wymaga jawnego `~`, a puste
`$REPO` w skrypcie kończy się błędem. Opcje każdego polecenia
pokazuje `hubi POLECENIE --help`.

### Sesje rozmawiają ze sobą po nazwie

Sesję wskazuje się nazwą: rodzaj, repozytorium i nazwa sesji, np. `claude praca`
albo `claude hubi-cli 2`. Numer panelu tmux (`%14`) nie jest adresem: zmienia
się przy każdym zatrzymaniu i starcie, a po restarcie serwera tmux numeracja
zaczyna się od nowa, więc stary numer może wskazać już inną sesję.

```console
hubi send claude praca -m 'CP17 pauza: co zrobione, co dalej'
echo "$raport" | hubi send codex praca      # treść ze standardowego wejścia
```

`hubi send` za każdym razem sam ustala, gdzie sesja jest teraz, wkleja tekst
jako jedną wiadomość i naciska Enter (`--no-enter` tylko wkleja). Restart
odbiorcy niczego więc nie psuje. Kod wyjścia 4 znaczy, że tej sesji teraz nie
ma albo nie działa (można ponowić później), a 2 to każdy inny błąd.

Każda sesja zna swoją nazwę ze zmiennych `HUBI6_TYPE`, `HUBI6_REPO` i
`HUBI6_NAME`, więc agent może napisać innym, jak mu odpowiedzieć:
`hubi send $HUBI6_TYPE $HUBI6_REPO $HUBI6_NAME`. Agent widzi tę samą nazwę co
menu: domyślna sesja to `1` (w JSON pole `name`; pole `instance` ma stałe
`primary`).

Nazwij sesję tak, jak jej rola, np. `hubi claude praca rev` dla review. Inne
sesje zobaczą ją w `hubi list` jako `claude/rev` i napiszą do niej
`hubi send claude praca rev -m '…'`. Co ma robić, mówisz jej w pierwszej
wiadomości.

Skrypt, który musi zrobić z panelem coś więcej (np. odczytać ekran), pyta o
niego tuż przed użyciem, nigdy go nie zapamiętuje:

```console
pane=$(hubi pane codex praca) || exit 1
tmux -N -S /run/user/$(id -u)/hubi6/tmux.sock capture-pane -p -t "$pane"
```

`hubi pane` niczego nie zmienia i odmawia, gdy sesja nie działa (np. jest
zakończona albo osierocona). `hubi pane ... --json` podaje też gniazdo tmux, a
`hubi list --json` pole `key` (stałe dla rodzaju, repo i nazwy) oraz `pane`,
które jest puste, gdy sesja nie działa.

### Procesy pomocnicze

Każda sesja ma własny cgroup systemd. `hubi stop` zabija go w całości, razem
ze wszystkim, co agent uruchomił, także przez `setsid`, `nohup` albo `disown`.
To zamierzone: zatrzymana sesja nie zostawia procesów. Taki pomocnik trzyma też
sesję przy życiu po wyjściu agenta (np. `/exit`), więc zamiast zniknąć jest
ona osierocona (ORPHANED) do czasu `hubi stop`.

Proces, który ma przeżyć agenta, uruchom jako osobną jednostkę użytkownika:

```console
systemd-run --user --same-dir --collect --setenv=PATH="$PATH" --unit=praca-straznik -- ./watchdog.sh
systemctl --user status praca-straznik    # stan
systemctl --user stop praca-straznik      # zatrzymanie
```

Taka jednostka dostaje środowisko menedżera systemd, a nie agenta. Bez
`--setenv=PATH="$PATH"` nie znajdzie `hubi` w `~/.local/bin`; inne potrzebne
zmienne też przekaż przez `--setenv=`.

## Konfiguracja

Plik `~/.config/hubi6/config.toml` jest opcjonalny. Wzór z opisem:
[`config/hubi6.example.toml`](config/hubi6.example.toml).

| Klucz | Domyślnie | Znaczenie |
|---|---|---|
| `repos_root` | `~/repos` | gdzie szukać repozytoriów |
| `home_workspace` | `~` | katalog dla sesji poza repozytorium, poza `repos_root`; `""` wyłącza |
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
