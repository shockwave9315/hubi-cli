# Hubi v6 — niezależny final deep review

## 1. Executive verdict

**FIX SMALL SET THEN ACCEPT**

Rdzeń runtime jest solidny: brak P1, fail-closed potwierdzony w każdym scenariuszu, żaden workload nie zginął bez jawnego polecenia. Deklaracja „brak znanych P2" jest jednak obalona. Znalazłem 3 P2 (każde z małą, lokalną poprawką) i 12 P3. HEAD `36fa688` nie powinien iść do cutover prep bez poprawienia P2.

## 2. Exact baseline

|                   |                                                           |
| ----------------- | --------------------------------------------------------- |
| Branch            | `feat/hubi-v6-python`                                     |
| HEAD              | `36fa688953cc796a7e41cb4e43cce80bfebc3bbf`                |
| Stan              | czysty przed i po; brak nowych plików, także ignorowanych |
| Diff od `def0c84` | 47 plików, +4916, 4 commity; pliki v5 bez zmian           |

Kodu nie zmieniałem, nic nie commitowałem ani nie instalowałem ownerowi.

## 3. Zgodność raportu autora z rzeczywistością

**Potwierdzone:**
- 64/64 testów (u mnie 100,6 s) i 100/100 przerwanych startów.
- 2 subprocessy na snapshot — policzone niezależnie shimami na PATH.
- Liczby wydajności (u mnie 289 ms przy 1000/50, autor 272 ms).
- Doctor jest read-only.
- Uczciwa informacja, że private clone był tylko na mocku.

**Niezgodne:**
- „Brak znanych P2" — są trzy.
- „Wszystkie 19 kontraktów zachowane" — kontrakt 13 jest złamany.
- „Blokuje domyślne bindings" — tylko do pierwszego `tmux source-file ~/.tmux.conf`.
- „UNKNOWN nigdy nie jest utożsamiane…" — prawda dla systemd, nie dla awarii odczytu tmux (R2).
- Komentarz w `hubi6/worker.py:19` twierdzi, że config jedzie w kopercie. Nie jedzie (R1).

## 4. Architecture review

- **Topologia** zgodna z planem: `hubi6-tmux.service` plus scope per workload.
- **Lifecycle** jest jeden dla wszystkich typów (`App.start/stop/attach`); różnice to tylko flagi resume i HUP zamiast INT.
- **Snapshot** jest jeden na render, UI nie odpytuje systemu per wiersz.
- **Subprocessy**: jedno miejsce `Popen` (`hubi6/adapters/process.py:24`), zawsze argv, bez `shell=True`/`eval`, z timeoutem i `communicate`. Wyjątek od granicy adapterów: `hubi6/github.py` sam buduje `systemd-run` i `systemctl stop`.
- **Granice** są płytsze niż w planie: `core/` to tylko `model.py`, a `inventory.py` i `services.py` importują konkretne adaptery. Lifecycle nie ma żadnego testu na fałszywych adapterach.
- **Sprzężenie**: każdy snapshot skanuje też repo root, a `find/stop/attach` idą przez snapshot. To źródło P2-1 i skalowania stopu z liczbą repo.
- **Tożsamość** jest deterministyczna, ale runtime ID zależy od `XDG_STATE_HOME` (P2-3).
- **Stan trwały**: wbrew planowi („brak trwałego stanu runtime") journal leży w `~/.local/state` i przeżywa reboot (P3-5).

## 5. Findings

### P1
Brak.

### P2

**P2-1 — Jeden zły wpis w repo root wyłącza całe Hubi, łącznie ze stopem**
- Reproducer: `ln -s loop ~/repos/loop` albo `ln -s /root/x ~/repos/x`, potem `hubi list`.
- Expected: wpis zgłoszony jako issue; inventory i stop działają dalej.
- Actual: `list`, `doctor`, `inspect`, `stop`, `reconcile` i menu kończą się `rc=1` z „Hubi: OSError; operation failed". Działającego workloadu nie da się zatrzymać przez Hubi.
- Root cause: `entry.is_dir()` stoi poza `try` w `hubi6/adapters/git.py:151`; `build()` woła `discover()` w każdym snapshocie (`hubi6/inventory.py:43`).
- Remediation: łapać `OSError` per wpis; awaria discovery ma być issue; `find/stop/attach` nie powinny wymagać skanu repo.
- Uwaga o wadze: trigger jest wąski (pętla symlinków, cel nieczytelny lub na zerwanym mouncie), u ownera dziś nie występuje. Daję P2 ze względu na całkowity zasięg; to klasa F5 w ostrzejszej postaci.

**P2-2 — Kontrakt 13 złamany: kwarantanna wklejania**
- Reproducer: menu → `w`, potem jeden burst `1⏎d⏎yes⏎` bez markerów bracketed paste.
- Expected: wklejka nie wywołuje akcji.
- Actual: workload zatrzymany.
- Drugi wariant: po attach z menu i detach ostatnią sekwencją na terminalu jest `ESC[?2004l` od tmuxa. Hubi nie włącza trybu ponownie, więc do końca sesji menu każda wklejka przychodzi surowa.
- Root cause: `ESC[?2004h` jest pisane raz (`hubi6/tui.py:40`), a `Input.line()` zwraca kolejne zbuforowane linie. v5 uzbrajał tryb przed każdym odczytem i przez 80 ms odrzucał resztę burstu.
- Remediation: uzbrajać przed każdym odczytem; po przyjętej linii odrzucać bajty z tego samego burstu; prompt potwierdzenia zaczynać od pustej kolejki; dodać test PTY także po attach.

**P2-3 (R1) — Niestandardowy `XDG_STATE_HOME`: każdy start pada, doctor mówi PASS**
- Reproducer: `XDG_STATE_HOME=~/custom hubi start terminal alpha`.
- Actual: `rc=2` „Workload startup failed", zostaje martwy workload EXITED; `doctor` w tym samym środowisku zwraca `ready=true`.
- Root cause: worker odtwarza `Config.load()` ze środowiska panelu, czyli serwera tmux, i przy niezgodności podmienia tylko `runtime_id` (`hubi6/worker.py:22-30`). Ścieżka journala wychodzi inna i worker odmawia.
- Skutek uboczny: runtime ID zawiera ścieżkę state (`hubi6/config.py:65`), więc dwa konteksty z różnym środowiskiem widzą dwa różne runtime.
- Remediation: worker ma brać katalog journala z `record_path` lub z koperty; dodać test.
- Uwaga: u ownera dziś uśpione (zmienna nieustawiona w powłoce i w managerze). README dokumentuje tę ścieżkę jako wspieraną.

### P3

| #          | Wada                                                                                                                                                                             | Dowód                                                                                                    | Miejsce                                                                             | Kierunek                                                     |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| P3-1 (R2)  | Awaria odczytu tmux przy żywym scope i aktywnej usłudze daje `ORPHANED` i „runtime DOWN" zamiast `UNKNOWN`. Stop, reconcile, start i attach są odmawiane, więc nic nie ginie.    | shim tmux `exit 1`; SIGSTOP serwera (list 10 s, odmowa stopu 30 s)                                       | `hubi6/inventory.py:69,93`                                                          | `ORPHANED` tylko gdy usługa inactive/failed                  |
| P3-2 (R4)  | Doctor zwraca PASS i `rc=0` bez codex i claude oraz bez `gh`. Brak `git`, `tmux`, `systemd-run`, `loginctl` daje FAIL.                                                           | PATH z symlinkami bez danej binarki                                                                      | `hubi6/doctor.py:39-41`                                                             | trójstan FAIL/WARN/PASS                                      |
| P3-3       | Dodatkowy panel w sesji to ślepy zaułek: `UNKNOWN`, a attach, stop i reconcile odmawiają. Pomaga tylko surowy `tmux kill-pane`.                                                  | po `source-file` z bindami z Twojego `~/.tmux.conf` wracają `prefix c` i `%`                             | `hubi6/core/model.py:218`, `hubi6/services.py:199,210`, `hubi6/adapters/tmux.py:61` | pozwolić na attach w tym stanie; ponawiać unbind w preflight |
| P3-4       | Okno łagodnego stopu to 0,2 s INT plus 0,4 s TERM, potem KILL (v5: 3 s i 2 s).                                                                                                   | agent potrzebujący 1,0 s zabity w trakcie; stop 0,78 s                                                   | `hubi6/services.py:169`                                                             | konfigurowalne, domyślnie bliżej v5                          |
| P3-5       | Po crashu serwera lub reboocie zostają wiersze `STOPPED` z journala. Znikają dopiero po stopie po ID albo starcie tego samego klucza.                                            | symulowany reboot: 4 takie wiersze, doctor PASS                                                          | `hubi6/services.py:107`, `hubi6/storage.py`                                         | automatyczne czyszczenie po zmianie generacji usługi         |
| P3-6       | `start` zwraca `rc=0` i to samo ID dla martwego workloadu.                                                                                                                       | agent padający przy exec: `rc=0`, EXITED; ponowienie po nieudanym starcie: najpierw `rc=2`, potem `rc=0` | `hubi6/services.py:89`                                                              | EXITED to kod ≠ 0 albo jawny restart                         |
| P3-7       | `hubi stop ""` zatrzymuje jedyny workload.                                                                                                                                       | `rc=0`, workload zniknął                                                                                 | `hubi6/services.py:66`                                                              | odrzucać pusty i za krótki prefiks                           |
| P3-8       | Hubi uruchomione wewnątrz terminala v6 może przypiąć sesję do samej siebie.                                                                                                      | `attached_clients=1`, serwer tmux ok. 98% rdzenia                                                        | `hubi6/tui.py:126` (`HUBI6_ACTIVE` nikt nie ustawia)                                | marker workloadu w env, odmowa self-attach                   |
| P3-9       | Clone: powód błędu jest gubiony; twardy limit 60 s; unit ma PATH managera; w menu cyfra klonuje od razu, bez potwierdzenia i postępu.                                            | „systemd-run failed (exit 1)" zarówno dla błędu auth, jak i dla limitu                                   | `hubi6/github.py:177-182`, `hubi6/tui.py:209-213`                                   | pokazać stderr `gh`, timeout w configu, potwierdzenie        |
| P3-10      | Środowisko workloadu jest węższe niż w v5: brak `SSH_AUTH_SOCK` i `SHELL`, `LANG=C.UTF-8`, `TERM=screen-256color`; terminal to `bash --noprofile --norc`.                        | zrzut env z agenta i terminala                                                                           | `hubi6/worker.py:67-70`, `hubi6/config.py:66`                                       | Twoja decyzja przed cutoverem                                |
| P3-11      | Snapshot 289 ms przy 1000 repo i 50 workloadach (cel audytu ≤100 ms); stop rośnie z 0,19 s do 0,96 s.                                                                            | sekcja 12                                                                                                | `hubi6/inventory.py:43`                                                             | oddzielić skan repo od inventory runtime                     |
| P3-12 (R3) | Builder zostawia stare pliki w `dist`, a `REVISION` pokazuje czysty HEAD także przy brudnym drzewie. Poza gitem zostaje półbundle. Brak instalatora i skryptów cutover/rollback. | usunięty moduł nadal w `dist` i importowalny                                                             | `tools/build_v6.py:10,18`                                                           | czyścić `dist`, oznaczać dirty, dodać install/uninstall      |

### P4

- **Utrata wszystkich trzech receipts** (residual autora): patrz sekcja 7. To samo dla obcego pliku w katalogu journala.
- **Prawdziwy hangup** (zamknięty pty) kończy launcher kodem 120 zamiast 129.
- **Backspace** jest wyświetlany jako dosłowne `\u007f`; linia parsuje się poprawnie.
- **Diagnostyka**: nieoczekiwany wyjątek zostawia tylko nazwę typu, bez tracebacku nawet z `--debug`. Zły typ w configu daje „TypeError; operation failed". Nieistniejący `--config` i nieznane klucze są po cichu ignorowane.
- **Widok**: kod wyjścia EXITED nie trafia do JSON. Menu główne nie pokazuje zdrowia repo, więc podmieniony checkout wygląda jak `codex:RUNNING`.
- **Przeniesione repo** nie jest rozpoznawane jako to samo; da się uruchomić drugiego agenta w tym samym drzewie (jak w v5).
- **Pozostałości**: `hubi6-workspace-id` w `.git` każdego repo (zostaje po deinstalacji), wpisy `[credential] helper` w klonach, pliki blokad, pliki `.hubi6-*` w journalu po SIGKILL.
- **Higiena kodu**: 6 nieużywanych importów, nieużywane `Config.timeout` i `RepoRef.git_ctime_ns`.
- **Origin**: `www.github.com`, `ssh.github.com:443` i aliasy `insteadOf` nie są rozpoznawane jako GitHub.
- **CLI**: `list --json` zwraca 0 przy runtime DOWN/UNKNOWN; zamknięty stdin daje `rc=1`.

## 6. 19 kontraktów

| #   | Wynik                | Evidence                                                                                        |
| --- | -------------------- | ----------------------------------------------------------------------------------------------- |
| 1   | PASS                 | PID serwera = MainPID, cgroup usługi; mutacje odmawiane, gdy tmux nieznany                      |
| 2   | PASS                 | usługa zatrzymana → start odmówiony, socket nie powstaje                                        |
| 3   | PASS                 | test SSH autora (u mnie zielony); zniszczenie unitu launchera; SIGKILL klienta; hangup          |
| 4   | PASS                 | własny scope; potomek po `setsid` ignorujący TERM martwy po stopie (uwaga P3-4)                 |
| 5   | PASS                 | jedno miejsce sygnału (`hubi6/adapters/systemd.py:96`), dokładne nazwy, brak globów w mutacjach |
| 6   | PASS                 | zły `--generation` odmówiony; stare ID nie zatrzyma następcy                                    |
| 7   | PASS z zastrzeżeniem | systemd: UNKNOWN i odmowa; tmux: etykieta ORPHANED (P3-1), akcje nadal odmawiane                |
| 8   | PASS                 | brak `loginctl` → doctor FAIL; linger i capability sprawdziłem tylko przez kod i test autora    |
| 9   | PASS                 | 12 par równoległych stopów i startów; blokada nie jest trzymana przy promptach ani attach       |
| 10  | PASS                 | 7 punktów przerwania × 3 tryby odzyskania; 20 losowych Ctrl+C; 100/100 autora                   |
| 11  | PASS                 | stop przy 5 równoczesnych takeover: 0 klientów-duchów, terminale przywrócone                    |
| 12  | PASS                 | REPLACED odmawia startu; worker przypina katalog przez fd                                       |
| 13  | **FAIL**             | P2-2                                                                                            |
| 14  | PASS                 | środowisko od zera, allowlista, brak TMUX i markerów (zmiana względem v5: P3-10)                |
| 15  | PASS                 | normal / view / shared / takeover                                                               |
| 16  | PASS                 | argv dosłowne, panel to sam agent, bez powłoki                                                  |
| 17  | PASS z odchyleniem   | INT 130, HUP 129, TERM 143, q 98, x 99, EOF 0; prawdziwy hangup 120                             |
| 18  | PASS                 | czyste konto: `doctor` i `list` nie tworzą żadnego katalogu                                     |
| 19  | PASS                 | EXITED z ostatnim wyjściem, także po globalnym `remain-on-exit off`                             |

## 7. F1–F15

|     | Wynik                       | Evidence                                                                                                                      |
| --- | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| F1  | PASS                        | 16 wrogich nazw: 15 startuje z dokładnym cwd, tab odrzucony, nic nie wykonane                                                 |
| F2  | PASS                        | brak TMUX/TMUX_PANE w agencie i terminalu; crash serwera → nowy serwer (residual P3-8)                                        |
| F3  | PASS                        | `exit-empty off` po `source-file` z Twoim configiem (residual P3-3)                                                           |
| F4  | PASS                        | drugi runtime nie widzi i nie potrafi zabić cudzego; scope bez receipt nigdy nie jest oferowany                               |
| F5  | PASS dla scenariuszy audytu | rename, delete, brak `.git`, zepsuty HEAD: widoczny i zatrzymywalny. Nowa regresja tej klasy: P2-1                            |
| F6  | PASS                        | REPLACED, start odmówiony, po stopie nowa generacja                                                                           |
| F7  | PASS                        | ciągłe próbkowanie `/proc/*/cmdline` testowego UID: sekretu brak; koperta 0600 usuwana; brak w tmux, systemd, journalu i logu |
| F8  | PASS behawioralnie          | 0/120 nieudanych commitów przy statusie Hubi; kontrola zwykłym `git status`: 76/120                                           |
| F9  | PASS                        | katalogi 0700, brak fallbacku do `/tmp`                                                                                       |
| F10 | PASS                        | regex ASCII pod `pl_PL`; kolejność identyczna w 3 locale                                                                      |
| F11 | PASS                        | reconcile działa dla uszkodzonych receipts (residual: P4, P3-3)                                                               |
| F12 | PASS                        | stop terminala 0,15 s, start 0,24 s (skutek uboczny P3-4)                                                                     |
| F13 | PASS                        | osobny UID, sondy kernela, cleanup                                                                                            |
| F14 | PASS                        | CLI i JSON; bez TTY błąd zamiast pętli                                                                                        |
| F15 | PASS                        | znaki sterujące odrzucane lub escapowane; ESC z opisu repo na GitHubie nie trafia na ekran                                    |

## 8. Adversarial / races

Wszystko poniżej kończyło się czystym stanem: 0 sierot, 0 duplikatów, 0 klientów-duchów.

- Dwa równoległe stopy ×12: jeden `rc=0`, drugi `rc=2`.
- Dwa równoległe starty tego samego klucza ×12: zawsze jeden workload, to samo ID.
- Start kontra stop ×15: albo nowa generacja, albo nic. Nigdy stan pośredni.
- Reconcile kontra start i kontra stop, po 10 rund.
- Stop workloadu A przy podpiętym kliencie B: B nietknięty.
- 30 szybkich cykli start/stop: 0 błędów, 0 jednostek failed.
- 75 sygnałów w losowych momentach menu: poprawny kod wyjścia i terminal w trybie cooked za każdym razem.

Receipts: skrzyżowane metadane (receipt A wpisany do B) — reconcile zabija tylko B.

## 9. Ownership receipts i unified inventory

**Fail-closed potwierdzony.** W żadnym z 9 scenariuszy utraty lub korupcji nic nie zostało zabite automatycznie.

- Utrata jednego lub dwóch receipts: workload widoczny, zarządzalny przez `stop` lub `reconcile`.
- Utrata wszystkich trzech (journal usunięty, opcja tmux usunięta, scope zakończony): martwa sesja zostaje. Nie ma jej na liście workloadów, jest tylko linia issue „reconcile using its receipt", której nie da się wykonać. Doctor jest trwale not-ready do ręcznego `tmux kill-session`.
- Przy żywym procesie wszystkich trzech nie da się stracić, bo opis scope'a żyje tak długo jak proces.
- Zgadzam się z P4; do poprawy jest sam komunikat i brak akcji.

**Inventory**: jeden widok dla Codex, Claude i terminali z pełnym schematem (typ, repo, zdrowie, instancja, sesja, scope, generacja, stan, klienci, wiek, cwd, akcje). Sortowanie repo → typ → instancja, niezależne od locale. Wybór w menu odnosi się do snapshotu, który był na ekranie. Braki: P3-5 i dwa punkty P4 o widoku.

## 10. GitHub browser

- **Token**: Hubi go nie czyta ani nie zapisuje; cache 0600 w katalogu 0700 zawiera tylko katalog repo.
- **Paginacja**: 250 repo kompletnie (wywołania z limitem 100, 200, 400 plus `gh api`).
- **Cache**: TTL, `--refresh`, `--offline` (zero wywołań `gh`) działają. Uszkodzony cache, cache world-readable i symlink są ignorowane.
- **Błędy**: awaria auth z cache daje znacznik stale; bez cache `rc=2`. Wiszący `gh` jest ucinany po 30 s i zabijany. Komunikat nie odróżnia auth od sieci.
- **LOCAL**: po originie, niezależnie od nazwy katalogu i formy URL. Poświadczenia wbudowane w origin nie trafiają do JSON.
- **Clone**: staging, origin check, `renameat2 NOREPLACE`. INT, TERM i HUP sprzątają od razu. Po SIGKILL staging i unit zostają do następnego clone, który je usuwa.
- **Kolizja nazw** między ownerami: odmowa, bez propozycji innej nazwy.

**REAL PRIVATE CLONE: NOT VERIFIED end-to-end.** Ścieżka kodu v6 wymagałaby uruchomienia v6 jako uid 1000 w Twoim managerze, więc jej nie wykonałem. Zrobiłem najbliższy bezpieczny odpowiednik:
- Dokładnie to polecenie, które buduje `hubi6/github.py:177-182`, w środowisku równoważnym unitowi, jako `dev` z Twoim istniejącym auth.
- Cel: `shockwave9315/pompa` (najmniejsze prywatne), do scratchpada.
- Wynik: `rc=0` w 2,1 s, origin przechodzi walidację v6, tokenu brak w `.git/config`.
- Kontrola bez poświadczeń: `rc=128`.
- Klon usunięty; `~/repos` i `/run/user/1000` bez zmian. Użyłem też read-only `gh repo list`.

## 11. CLI / TUI / JSON

- `--version` zwraca „Hubi 6.0.0" (bez rewizji).
- `list --json` daje poprawny JSON bez ANSI przy pustym runtime, braku tmux, awarii systemd, zatrzymanej usłudze i uszkodzonym journalu. `--debug` pisze na stderr.
- Zawsze `rc=0`, także przy DOWN/UNKNOWN. Błąd configu daje tekst na stderr, bez JSON.
- TUI: Ctrl+C, EOF, SIGTERM, resize, Unicode, strzałki, linia 70 kB — terminal zawsze przywrócony.
- Rozłączenie w trakcie promptu „Type yes": workload nie zostaje zatrzymany.
- Zatrzymanie usługi przy otwartym menu: następny render pokazuje „runtime: DOWN", bez crasha.

## 12. Performance

| Kształt                          | Snapshot (mediana) | Cały proces `list --json` | Exec na snapshot |
| -------------------------------- | ------------------ | ------------------------- | ---------------- |
| 7 repo / 6 workloadów / 1 klient | 28,9 ms            | 77,5 ms                   | 2                |
| 200 repo / 6 workloadów          | 57,0 ms            | 106 ms                    | 2                |
| 1000 repo / 50 workloadów        | 288,6 ms           | 341 ms                    | 2                |

- Liczba exec na akcję jest identyczna w obu skalach: doctor 5, start 20, stop 15, każdy ekran menu 2 (status gita 3). Brak N+1.
- Opóźnienie klawisz → prompt na kształcie zbliżonym do Twojego: ok. 60 ms. Mój pomiar ma ziarno 50 ms, więc realnie 30–60 ms.
- Jedyna regresja architektoniczna to sprzężenie ze skanem repo (P3-11).

## 13. Fresh-install validation

- Bundle zbudowany z czystego klonu i rozpakowany na dziewiczym koncie.
- Przed instalacją unitu: doctor FAIL, runtime DOWN, żadnych utworzonych katalogów.
- Po ręcznych krokach z README: doctor PASS, pierwszy workload startuje.
- Pusty repo root daje czytelny błąd.
- Ręczna deinstalacja schodzi do zera. `disable --now` przy działających workloadach zostawia agenty odporne na HUP jako sieroty — kolejność w runbooku rollbacku ma znaczenie.
- Instalatora nie ma (P3-12).

## 14. Test quality

64 testy są prawdziwe, ale nie wystarczają:

- **Mutacje** na kopii w labie: 7 z 11 wykrytych. Przeżyły: wyłączenie `GIT_OPTIONAL_LOCKS` (test F8 sprawdza tylko słownik), klasyfikacja R2, nieuzbrojenie bracketed paste, usunięcie sprawdzenia tożsamości w workerze.
- **Bez testu**: zły wpis w repo root, `XDG_STATE_HOME`, wklejka po attach, prawdziwy hangup, wyjście z dodatkowego panelu, puste ID, start na EXITED, wiersze po reboocie.
- `make test` poza labem uruchamia tylko 33 testy jednostkowe.
- Testy runtime idą in-process z drzewa roboczego, nie ze zbudowanego bundla.

## 15. Test isolation

- Użytkownik `hubi6test` (uid 1001) z własnym `user@1001`, busem, socketem i limitami, utworzony narzędziem autora po przeczytaniu go w całości.
- Wszystkie procesy testowe szły jako transient units w managerze 1001.
- Sondy kernela: 28 PID-ów ownera odmawia sygnałów; socket, bus i cgroupy niedostępne.
- Jako `dev` wykonałem tylko: zapytania read-only, build w klonie w scratchpadzie oraz `gh`/`git` z sekcji 10.
- Kilka FAIL-i po drodze okazało się błędami mojego harnessu. Odsiałem je i nie ma ich w findingach.

## 16. Cleanup verification

- Przed teardownem: scopes 0, sesje 0, klienci 0, unity clone 0, failed 0; socket znika po zatrzymaniu usługi.
- Po teardownie: brak usera, grupy, HOME, `/run/user/1001`, linger, slice'a, override'ów, labowego sshd i procesów uid 1001.
- 0 plików uid 1001 w `/tmp`, `/var/tmp`, `/dev/shm`, `/run`.
- W Twoim managerze 0 jednostek `hubi6*`; brak katalogów v6 na Twoim koncie.
- Kopie builda w scratchpadzie usunięte.

## 17. Live v5 fingerprint before/after

**Identyczny.** Jedyna różnica w pełnym diffie to lista PID-ów potomnych mojej własnej sesji (`hubi-claude-83de655a3d27.scope`).

Bez zmian: InvocationID usługi `4553644e…`, MainPID 172, NRestarts 0, InvocationID 9 scope'ów, id i czasy 9 sesji, PID-y paneli, inode socketu, SHA zainstalowanych plików.

W trakcie (13:41) pojawił się klient na `pts/3` w sesji `hubi-claude-praca`. Łańcuch rodziców to `sshd-session: dev@pts/3 → -bash → hubi (v5) → tmux attach`, czyli Twoje własne podpięcie. Na końcu klientów było 0.

## 18. Rekomendowany fix-set

Przed akceptacją:
1. P2-1: `OSError` per wpis w `discover()`; stop i inspect bez skanu repo.
2. P2-2: uzbrajanie bracketed paste przed każdym odczytem plus odrzucanie burstu; test PTY po attach.
3. P2-3: worker bierze katalog journala z receipt; test z `XDG_STATE_HOME`.
4. P3-7: odrzucić puste i za krótkie ID.
5. P3-6: `start` na EXITED nie może zwracać sukcesu.
6. P3-1: UNKNOWN przy awarii tmux z aktywną usługą.

W tym samym podejściu, bo tanie: P3-2 (doctor), P3-3 (wyjście z dodatkowego panelu), P3-8 (marker workloadu), P3-12 (czyszczenie `dist`).

Decyzje dla Ciebie: okno stopu (P3-4) i polityka środowiska oraz plików rc terminala (P3-10).

## 19. Final recommendation

v6 **nie** powinno przejść do owner smoke ani cutover prep na `36fa688`. Po punktach 1–6 z testami regresyjnymi i ponownym przebiegu suite pod osobnym UID może przejść.

W owner smoke warto sprawdzić dwie rzeczy, których nie zweryfikowałem:
- prawdziwy private clone przez samo v6,
- czy prawdziwy Codex i Claude po `hubi stop` zostawiają sesję do wznowienia przy obecnym oknie 0,6 s.

Skrypty testowe i logi są w scratchpadzie sesji (`…/scratchpad/review/`, tmpfs). Mogę zapisać ten raport jako plik albo stronę, jeśli chcesz go przekazać autorowi.

STOP.