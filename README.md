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
- Strażnik limitu Codexa widoczny i sterowany bez agenta (`s`, `hubi guard`):
  stan, CP, cel, progi, wersja, log; zmiana CP, celu, progów i wersji jednym
  poleceniem. Sam strażnik jest w repozytorium (`guard/`, instalacja
  `bash guard/install.sh`).

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
| `s` | strażnik limitu Codexa (tylko gdy jest skonfigurowany) |
| `?` | pomoc |
| `q` / `x` | powłoka / rozłącz (`x` tylko po autologinie) |
| `0`, Esc | wróć |

`l`, `c` i `t` zawsze startują nową sesję. Do działającej wchodzisz cyfrą.
Sesje danego typu w repo dostają numery: `codex/1`, `codex/2`, `codex/3`.
Numer nie oznacza ważności. Sesji z rolą daj nazwę (`n`), np. `claude/rev`.

Każda nazwa sesji Claude i Codexa ma swoją rozmowę, a „wznów” wraca dokładnie
do niej, nie do innej. Co pracowało w `claude/rev`, po restarcie wraca do
`claude/rev`, a `codex/1` do swojej rozmowy z `codex/1`. Gdy w repo jest kilka
zatrzymanych sesji z rozmowami, menu pyta, którą wznowić (po nazwie, bez
domyślnej). Lista rozmów agenta pojawia się tylko dla nazwy, która nie ma jeszcze
zapisanej rozmowy.

Starszą rozmowę wybierasz w samym agencie (`/resume` w Claude albo w Codexie) albo
pod nową nazwą (`hubi claude REPO stara --resume` pokaże listę). Po `/clear`,
`/new` czy wyborze z listy Hubi przepina nazwę na bieżącą rozmowę, gdy wychodzisz
z sesji do menu (`Ctrl-b d`), gdy ją zatrzymujesz i gdy narzędzie wysyła do niej
przez `hubi send`. Po zakończeniu agenta przez `/exit` Hubi tej zmiany nie zdąży
odczytać. Jedna rozmowa należy do jednej nazwy. Nowa rozmowa od razu zastępuje
zapisaną. Claude dostaje jej identyfikator przy starcie, Codex dopiero przy
pierwszej wiadomości; jeśli Codex skończy się, zanim Hubi go odczyta, „wznów”
pokaże listę, nigdy starszej rozmowy.

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
hubi guard status             # strażnik limitu Codexa (szczegóły niżej)
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

### Strażnik limitu Codexa

Strażnik pilnuje limitów Codexa: przy niskim limicie każe mu zrobić pauzę, po
odnowieniu wysyła „wznów”. To skrypt w bashu, bez AI. Jego pliki są w katalogu
[`guard/`](guard/README.md): skrypt i jego wersje, odczyt limitów, testy i pełny
opis działania. Instalacja do katalogu strażnika
(`~/.cache/praca/codex-watchdog`, jednostka `praca-codex-watchdog`):

```console
bash guard/install.sh     # kopiuje tylko brakujące pliki, inny plik o tej samej nazwie zatrzymuje instalację
hubi guard start CP1 codex/<repo> claude/<repo>
```

Hubi pokazuje strażnika i nim steruje, a sam skrypt zostaje bez zmian. Hubi
czyta jego pliki i jednostkę systemd, a uruchamia go tak, jak opisuje
`guard/README.md`.

Na ekranie głównym jest wiersz `STRAŻNIK`: czy działa, CP, stan (`RUNNING`,
`PAUSED_5H`…), godzina wznowienia w pauzie, wersja, cel i kontakt. Klawisz `s`
otwiera szczegóły z ostatnimi wpisami logu i klawiszami: `u` uruchom, `z`
zatrzymaj, `p` nowy CP, `w` Codex i kontakt, `g` progi, `v` wersja, `l` limity.
Godziny są w czasie polskim.

```console
hubi guard status [--limits]          # stan; --limits czyta też limity (5–20 s, najwyżej co 2 min)
hubi guard log [-n 20]                # ostatnie wpisy logu, czas polski
hubi guard cp CP21                    # ten sam Codex i kontakt, nowy CP
hubi guard who codex/praca claude/praca   # inny Codex i kontakt
hubi guard stop
hubi guard start [CP CODEX KONTAKT]   # bez argumentów: ostatnie ustawienie
hubi guard limits [7 1] [--idle 720] [--max-hours 48]   # progi; z liczbami: zmień i uruchom ponownie
hubi guard version [v4.8]             # lista wersji; z argumentem: przełącz i uruchom ponownie
```

Progi: pierwsza liczba to pauza 5 h (LIMIT-5, gdy limitu 5 h zostało tyle % albo
mniej), druga to stop tygodniowy (`WEEKLY_STOP` przy tylu % tygodnia). Wpisujesz
liczby całkowite 0–100 bez `%`. Zwykle stop tygodniowy jest niższy niż pauza 5 h;
odwrotne ustawienie przejdzie, ale z ostrzeżeniem. `--idle` to minuty
bezczynności Codexa do końca pracy strażnika (1–10080), `--max-hours` najdłuższy
czas jego pracy (1–720). Status pokazuje obecne progi. W menu: `g`, potem np.
`5 1` albo `5 1 720 48`.

Bezpieczniki:

- CP albo Codexa zmienisz tylko w stanie `RUNNING`. W pauzie strażnik trzyma
  stan pod nazwą CP, więc zmiana by go zgubiła i Codex nie dostałby „wznów”.
  Ponowny start z tym samym CP i Codexem, także z innym kontaktem, działa zawsze.
- Restart zachowuje `PATH`, wszystkie `WD_…` i opcje restartu z działającej
  jednostki. Bez `codex` w `PATH` start jest odrzucany.
- Nazwy sesji jak w Hubi (`codex/praca`, `claude/praca`, `codex/praca/temp-rev`);
  numery paneli (`%N`) nie przechodzą. `codex/praca/1` to to samo co `codex/praca`.
- Przy blokadzie tygodniowej (`WEEKLY_STOP`) start jest odrzucany z instrukcją
  ręcznego wznowienia. Hubi nie usuwa pliku stanu sam.
- `version` podmienia `watchdog.sh` na kopię `watchdog.vXX.sh` (nowy plik, nie
  nadpisanie w miejscu) i restartuje działającego strażnika. Odmawia, gdy
  `watchdog.sh` nie odpowiada żadnej zachowanej wersji, bo zmiana by przepadła.
- Gdy nowa wersja, CP albo cel nie wystartuje (np. v4.8 nie przyjmuje Codexa jako
  kontaktu), Hubi wraca do wersji i ustawienia, które przed chwilą działały, i mówi
  dlaczego. Strażnik nie zostaje wyłączony.
- Gdy Hubi nie zna poprzedniego ustawienia, start z nowym CP albo Codexem
  sprawdza wszystkie pliki `state.codex-*`: żaden Codex w pauzie nie zostanie bez „wznów”.
- Nowe progi: strażnik startuje ponownie z tym samym CP i celem, także w pauzie
  (stan wraca z pliku). W blokadzie tygodniowej zmiana jest odrzucana. Pozostałe
  `WD_…` i opcje restartu zostają. Zła liczba jest odrzucana, zanim strażnik się
  zatrzyma; gdy strażnik i tak odrzuci próg, wraca z poprzednimi progami. Gdy
  strażnik nie działa, progi zapisują się na następny start.
- W menu każdy restart (`p`, `w`, `g`, `v`) i zatrzymanie (`z`) potwierdzasz klawiszem `q`.
- Hubi nigdy nie tworzy `reset.armed`, nie uruchamia `reset_credit.py` i nie pisze
  do Codexa.

## Konfiguracja

Plik `~/.config/hubi6/config.toml` jest opcjonalny. Wzór z opisem:
[`config/hubi6.example.toml`](config/hubi6.example.toml).

| Klucz | Domyślnie | Znaczenie |
|---|---|---|
| `repos_root` | `~/repos` | gdzie szukać repozytoriów |
| `home_workspace` | `~` | katalog dla sesji poza repozytorium, poza `repos_root`; `""` wyłącza |
| `[agents] claude` | `["claude", "--permission-mode", "bypassPermissions"]` | polecenie Claude |
| `[agents] codex` | `["codex", "--no-daemon", "-c", "check_for_update_on_startup=false"]` | polecenie Codexa |
| `[ui] refresh` | `2` | odświeżanie listy w sekundach |
| `[ui] icons` | `true` | `false` = sam ASCII |
| `[guard] dir` | `~/.cache/praca/codex-watchdog` | katalog strażnika limitu Codexa; `""` ukrywa strażnika |
| `[guard] unit` | `praca-codex-watchdog` | jego jednostka systemd użytkownika (bez `.service`) |

Codex startuje bez okna aktualizacji (`check_for_update_on_startup=false`). W tym
oknie domyślne jest „Update now”, więc każdy Enter, także z `hubi send` albo od
strażnika, instalował aktualizację i zamykał Codexa razem z sesją. Aktualizujesz
ręcznie: `npm install -g @openai/codex`, gdy żaden Codex nie pracuje.

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
