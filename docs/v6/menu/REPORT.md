# Hubi v6 — polskie menu jednoklawiszowe i poprawki użyteczności

## 1. Status

**GOTOWE DO PRZEGLĄDU. PR pozostaje DRAFT.** Bez instalacji u ownera: instalator
odmawia przy żywych sesjach, a na koncie ownera pracował w tym czasie agent.

Zakres: nowe domyślne menu, czytelność sesji w tmux, zachowanie po zakończeniu
procesu, polskie komunikaty oraz usterki znalezione w teście ownera na terminalu
65×25. Topologia runtime, model własności i ścieżki sygnałów bez zmian.

## 2. Źródło: test na koncie ownera (v6 z `main`, 68dd12a)

Test na żywym runtime, w osobnym terminalu 65×26, bez dotykania sesji ownera.

| # | Znalezisko | Rozwiązanie |
|---|---|---|
| 1 | Ctrl-Z w menu po autologinie SSH zamraża sesję: `hubi` w stanie `T`, powłoka logowania czeka bez job control; Ctrl-C, Ctrl-\ i Ctrl-D nie pomagają | Menu wyłącza znak suspend i obsługuje SIGTSTP; serwer tmux odpina `suspend-client` |
| 2 | Sesja startowana z menu po autologinie dostaje PATH sprzed dopisków `~/.profile` (systemowy Node 20 zamiast 22, brak `~/bin` i `~/ai-harness/bin`) | Blok autologinu na końcu `~/.profile` (`config/hubi6-autologin.sh`) |
| 3 | Po `exit` w terminalu albo zakończeniu agenta klient zostaje na „Pane is dead”; potem ręczne `d`, Enter, `yes`, Enter | Hak `pane-died` odpina klientów; czyste wyjście sprząta się samo |
| 4 | Wejście do 80 ms po Enterze jest po cichu odrzucane; brak reakcji na nieznany klawisz, wielką literę, strzałkę | Pojedyncze klawisze bez kwarantanny między nimi; każda odrzucona seria i każdy nieznany klawisz mają komunikat |
| 5 | `inspect` wypycha menu poza 25 wierszy, JSON ucięty | Ekran szczegółów mieszczący się w oknie; pełny JSON zostaje w `hubi inspect` |
| 6 | Przy 65 kolumnach zawijają się nagłówek, lista i pasek akcji; brak nazw instancji | Kolumny przycinane do szerokości, nic się nie zawija od 24 kolumn |
| 7 | Pasek tmux pokazuje `[hubi6-46…` zamiast repo i instancji | Etykieta `typ/instancja · repo`, kolor paska zależny od typu |
| 8 | `[x] exit SSH` przy ręcznym `hubi` tylko wraca do shella | `x` oferowane tylko pod autologinem (`HUBI_AUTOLOGIN`) |
| 9 | Numery na liście przesuwają się po dodaniu sesji | Numeracja w kolejności utworzenia |
| 10 | Odświeżenie GitHuba zamraża ekran bez komunikatu | Spinner; zapisana lista pokazywana od razu |
| 11 | Większy klient przy tej samej sesji zostawia telefonowi wycinek okna | `window-size latest` |
| 12 | Brak polecenia „wystartuj i wejdź” (v5 miało `hubi claude REPO`) | `hubi claude\|codex\|terminal REPO [INSTANCJA]`; start z menu wchodzi sam |

Pomiar wyjściowy na koncie ownera: start menu 69 ms, przejście między ekranami
20 ms, attach 53 ms, powrót 23 ms. Sam rendering nie był problemem; czas
zabierały Enter po każdym klawiszu, gubione wciśnięcia i liczba kroków.

## 3. Decyzje projektowe

- **Dwa menu.** `hubi6/tui.py` (liniowe, angielskie, po przeglądzie) zostaje bez
  zmian jako zapas: `hubi --classic`, `HUBI6_UI=classic` i automatycznie, gdy
  nowe menu rzuci nieoczekiwany wyjątek. Hubi jest punktem wejścia po SSH, więc
  awaria nowego menu nie może zostawić użytkownika bez menu. Dotychczasowe
  regresje PTY nadal testują menu liniowe.
- **Bezpieczeństwo wejścia.** Odczyt, który nie jest dokładnie jednym klawiszem,
  jest odrzucany w całości i wymaga 80 ms ciszy; bracketed paste jest połykany do
  znacznika końca. Przed pytaniem o zatrzymanie i przed każdym attach terminal
  musi być cichy przez 80 ms i jest opróżniany. Wklejka nie może więc ani
  potwierdzić zatrzymania, ani trafić do agenta. Osobne naciśnięcia nie są
  dławione.
- **Potwierdzenie zatrzymania to `q`** (decyzja ownera), jedno, na opróżnionym
  terminalu. Klonowanie dalej wymaga wpisania `tak` i Entera.
- **Zakończone sesje.** `remain-on-exit` i stan EXITED zostają. Zmienia się tylko
  to, że klient wraca do menu, a wyjście z kodem 0, bez sygnału i bez klientów
  jest usuwane (`App.reap_clean_exit`). Niczego się nie sygnalizuje: scope już
  nie istnieje. Wyjście z błędem zostaje do obejrzenia.
- **Tytuły paneli** ustawia sam agent, więc czyta je osobne, pobłażliwe zapytanie
  (`Tmux.titles`). Nie trafiają do parsera inventory, weryfikacji własności ani
  wyznaczania stanu; cały tekst zewnętrzny przechodzi przez `display()`.
- **Tłumaczenia przy wyświetlaniu.** Komunikaty wyjątków, logi i JSON zachowują
  angielskie identyfikatory. `hubi6/i18n.py` tłumaczy to, co czyta człowiek; test
  skanuje AST pakietu i pada, gdy komunikat nie ma polskiej wersji.

## 4. Zmienione pliki

- `hubi6/menu.py` (nowy): modele widoku, czyste funkcje ramek, czytnik klawiszy,
  malarz ekranu, kontroler.
- `hubi6/i18n.py` (nowy): polskie brzmienie komunikatów, stanów i wieku.
- `hubi6/tui.py`: tryb terminala menu bez suspend (znak i sygnał).
- `hubi6/adapters/tmux.py`: etykieta i kolor paska, hak `pane-died` z warunkiem
  `session_attached`, `window-size latest`, odpięcie `C-z`, `titles()`.
- `hubi6/services.py`: `reap_clean_exit`, `open`.
- `hubi6/cli.py`: wybór menu z zapasem, polecenia `claude|codex|terminal`,
  polska pomoc i komunikaty.
- `hubi6/config.py`, `config/hubi6.example.toml`: sekcja `[ui]`.
- `config/hubi6-tmux.conf`: pasek stanu, lista sesji z etykietami, `C-z`.
- `config/hubi6-autologin.sh` (nowy), `docs/v6/README.md`, `docs/v6/CUTOVER.md`.
- `tests_v6/test_menu.py` (nowy); `tests_v6/test_runtime.py` (klient PTY z
  wyborem menu i rozmiaru); dwie asercje dostosowane do polskiego CLI.

## 5. Błąd złapany przez lab w trakcie pracy

Pierwsza wersja haka (`detach-client -s =SESJA`) kończyła się błędem „no current
client”, gdy panel kończył się bez podpiętego klienta, także przy podmianie
procesu zastępczego podczas startu. tmux zostawiał ten błąd w panelu jako
komunikat w trybie podglądu, który połykał klawisze następnego klienta. Hak ma
teraz warunek `if -F '#{session_attached}'`, a test sprawdza, że po starcie panel
nie jest w żadnym trybie.

## 6. Walidacja

Testy jednostkowe (dowolny UID): parser klawiszy, serie i wklejki, brak
dławienia osobnych klawiszy, opróżnienie przed potwierdzeniem, ramki wszystkich
ekranów dla szerokości 24–200 i wysokości 8–50, wrogi tekst w tytule, numeracja,
malowanie tylko zmienionych wierszy, komplet tłumaczeń, etykieta paska, `[ui]`,
warunki sprzątania, tryb terminala bez suspend. Kontroler menu na atrapach:
wejście cyfrą, dołączenie do zajętej sesji, zatrzymanie tylko po `q`, nazwa
instancji i wznowienie, zastąpienie zakończonej instancji, nieudany start,
klawisze wyjścia, powroty, widok repo i szczegółów, `tak` przed klonowaniem.

Testy runtime (wyłącznie jednorazowy UID): start z menu z wejściem i powrotem po
`exit`, wyjście z błędem zachowane i zgłoszone, zatrzymanie tylko po świadomym
`q` (seria `d1q` i `q` wpisane przed pytaniem nie działają), Ctrl-Z i SIGTSTP nie
zatrzymują menu, kody wyjścia i przywrócenie termios, opcje i haki tmux, jedno
polecenie start+wejście, sprzątanie po przeskoku między sesjami w tmux, przejście
na menu liniowe po awarii nowego.

## 7. Pomiary nowego menu (lab, PTY 65×26, 4 repo, 3 sesje)

| Operacja | Menu liniowe | Nowe menu |
|---|---|---|
| start do pierwszego ekranu | 69 ms | 79 ms |
| klawisz do przerysowania | 20 ms + Enter | 26 ms |
| drugi klawisz 0–80 ms po pierwszym | odrzucony | przyjęty |
| wejście do działającej sesji | 6 klawiszy | 1 |
| nowy agent w repo | 8 klawiszy | 2 |
| zatrzymanie sesji | 10 klawiszy | 3 |

Pierwsza kolumna czasów pochodzi z konta ownera, druga z labu. Snapshot nadal
używa 2 podprocesów; menu dokłada jedno zapytanie o tytuły. Bezczynne menu z
jednym pracującym agentem wysyła około 280 B/s w pierwszej minucie (zmienia się
kolumna wieku i spinner), potem odświeża co 10 s.

## 8. Wynik końcowy

Baseline: `68dd12ab438107766b56d3b6db3fdf5dc7c2a594` (`main`).
Gałąź: `feat/hubi-v6-menu-pl`.

Commity:

1. `8d7eb1160a81d5a92272cc779c26850536070ca2` — `feat(v6): Polish single-key menu, readable sessions and usability fixes`.
2. `0e30afdebbfecc6a40e1826dd89a34fedcf393e7` — `test(v6): cover dashboard controller flows with fakes` (tylko testy).
3. Commit raportu i dowodów.

Testowany czysty HEAD: `0e30afdebbfecc6a40e1826dd89a34fedcf393e7`, bundle z
`dirty: false` (`evidence/build-source.json`).

- **Jednostkowe, UID ownera: 153 testy, 83 wykonane, 70 pominiętych** (pominięte
  to testy runtime, które z założenia nie działają poza labem). `evidence/unit.log`.
- **Pełny zestaw w labie: 153/153, bez pominięć; 100/100 przerwanych startów
  odzyskanych.** Wcześniejszy przebieg na commicie 1: 137/137.
- **Oba mutanty wykryte:** kwarantanna linii oraz `GIT_OPTIONAL_LOCKS=0`.
- **Benchmark bez regresji** względem dowodów PR #8: snapshot 7 repo / 6 sesji
  28,04 ms (było 27,78), 200/6 54,51 ms (54,66), 1000/50 278,79 ms (277,55),
  nadal 2 podprocesy; attach 7 i stop 15 podprocesów jak poprzednio; menu
  liniowe Enter do promptu p50 37,64 ms (37,59). `evidence/performance.json`.
- **Lab:** przed zamknięciem scopes/sessions/clients/clone units = 0; po
  zamknięciu użytkownik, HOME, runtime i linger usunięte.
  `evidence/final-validation.log`.

Na koncie ownera w trakcie prac: dwie sesje ownera nietknięte (0 klientów z tej
pracy poza dwoma wejściami do `claude/rev` uzgodnionymi w teście, bez wpisywania).
Menu z tej gałęzi uruchomiono tam raz, tylko do odczytu: rozpoznało pracującego
Codexa wraz z tytułem zadania i czekającego Claude'a.

Blok autologinu na koncie ownera został przeniesiony z `~/.bashrc` na koniec
`~/.profile` (kopie zapasowe obu plików obok). Sprawdzone na emulowanym
logowaniu (`bash -l` z `SSH_TTY`): sesja z menu ma Node 22, `jc-impl` i `later`.

## 9. Świadome ograniczenia

- Menu liniowe zostaje po angielsku; to zapas, nie interfejs docelowy.
- `tools/install_v6.py` nadal wypisuje komunikaty po angielsku.
- Dwa klawisze dostarczone w jednym odczycie (zator na łączu) są odrzucane
  razem, z komunikatem. To cena ochrony przed wklejką bez bracketed paste.
- `Ctrl-b s` nie potrafi ukryć nazwy sesji, więc etykieta jest czytelna dopiero
  na szerokim ekranie. Na wąskim zostają `Ctrl-b )`, `(` i `L`.
- Aktywność agenta jest odczytywana z tytułu panelu (spinner na początku). Agent,
  który nie ustawia tytułu, jest pokazywany jako „czeka”.
- Nie uruchamiano prawdziwych agentów w nowym menu; lab używa atrap.
- Sesje utworzone starym kodem nie mają etykiety ani haka do czasu ponownego
  utworzenia.
