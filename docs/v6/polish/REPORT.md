# Hubi v6 — final polish PR #8

## 1. Executive status

**FIX-SET COMPLETE. READY FOR FINAL CLAUDE CHECK.**

Zakres: ostatni lokalny polish, bez zmiany topologii runtime, Basha v5, migracji
lub owner cutoveru. Źródło findings i rozbieżność dostępności komentarza targeted
re-review są opisane w [REVIEW_INPUT.md](REVIEW_INPUT.md).

## 2. Baseline, final HEAD, commity

Baseline HEAD: `ba22947b86537e9fc42a6edc3e96c0998f8f93e6`.
Branch: `feat/hubi-v6-python`; istniejący PR #8, base `main`.

Testowany czysty HEAD: `bade59c1c86699d6c4f78ef104f825655b608b90`.
Końcowy commit dodaje wyłącznie raport i evidence. Jego pełny final HEAD jest
podany w handoffie i opisie PR; hash raportu nie jest wpisywany do własnego pliku.
Fresh bundle po publikacji ma aktualny REVISION i ten sam production source
digest: `66d6571b63db9f57f023ad1182cbdde8bf7b261eb519d2a294a0c8390687d062`.

Commity po baseline:

1. `0f1d56c86bfcdfaa9a88e63a02f59f763a870115` — `fix(v6): finish targeted polish and deterministic regression guards`.
2. `bade59c1c86699d6c4f78ef104f825655b608b90` — `test(v6): restrict new PTY signal regressions to disposable UID`.
3. Commit raportu/evidence — `docs(v6): record final polish validation and review handoff`.

## 3. Changed files

- `hubi6/tui.py`: odroczona kwarantanna, natychmiastowy return akcji, reset granicy
  input po detach, bezpieczny backspace i incremental UTF-8 echo.
- `hubi6/adapters/git.py`, `inventory.py`: ordinary directories, priorytet issues.
- `hubi6/services.py`: pane-repair receipt fallback i startup exec/ack window.
- `hubi6/adapters/tmux.py`, `adapters/systemd.py`, `doctor.py`: informacyjne
  liczenie poprawnych obiektów innych namespace bez oferowania lifecycle.
- `tools/install_v6.py`: manifest, hash verification i wznawialny uninstall.
- `tests_v6/test_polish.py`, `test_fixset.py`, `test_install.py`: nowe regresje,
  kontrolowany Git lock test, fresh install i interrupted uninstall.
- `tests_v6/mutations.py`, `benchmark.py`, `tools/test_v6.py`: dwa realne mutation
  guards, baseline/current PTY oraz snapshot measurements, pełny lab flow.
- `docs/v6/README.md`, `CUTOVER.md`, ten katalog: kontrakty, retry runbook, raport
  i evidence. V5 executable/config/unit/autologin/legacy tests mają pusty diff.

## 4. Mapping targeted findings

Nazwy testów pomijają prefiks `test_`; nowe testy są w `tests_v6/test_polish.py`,
chyba że wskazano inaczej.

| Finding | Status | Implementacja | Test |
|---|---|---|---|
| P3-A stałe +80 ms | FIXED | `Input.finish_line()` czyści bufor i ustawia deadline od ostatniego odebranego bajtu, po czym natychmiast zwraca akcję. `line()` odrzuca dalsze bajty przed kolejnym odczytem, wydłużając deadline od każdego chunku. Render zużywa czas okna. Destructive prompt drenuje przed pokazaniem tekstu. Po detach rearm i nowa granica bez blokowania renderu. | `accepted_line_returns_before_quarantine_wait`; PTY `line_quarantine_blocks_split_burst_without_confirmation`, `last_byte_extends_quarantine_and_manual_fast_line_works`; `whole_main_burst_stays_on_workloads_screen`; dotychczasowe raw paste/detach tests; mutant removal wykrywany. |
| P3-B ordinary dirs / issue priority | FIXED | `Git.discover()` pomija tylko brak `.git`; filesystem errors i uszkodzone `.git` pozostają w per-entry issue. Error handler nie wykonuje dodatkowego stat. Inventory dopisuje repo diagnostics po runtime/receipt issues. | `plain_directories_skipped_but_damaged_git_reported`; `runtime_issue_precedes_repo_diagnostics_in_menu`: 3 zwykłe i 3 uszkodzone katalogi oraz rzeczywista unverified session; runtime issue widoczny. Wcześniejsze ELOOP/permission/broken-link tests nadal przechodzą. |
| Pane repair P4 | FIXED | Exact scope read, decode description receipt dla właściwego RID/UID, zgodność id/session/scope/description, następnie fresh `verify_scope` z odzyskanym InvocationID i porównanie description. Brak name-only adoption. | `pane_repair_missing_journal_and_session_invocation`: utrata journala, session receipt z pustym invocation, extra pane, poprawny live scope; primary/rodzeństwo zachowane, repair→stop i repair/stop race. Wcześniejszy crossed-receipt guard nadal działa. |
| Resumable uninstall P4 | FIXED | Private manifest pozostaje do końca, zawiera uninstalling i hashes własnych plików/backup. Zero-state sprawdzane na każdej próbie. Disable/stop rc nie blokuje retry, jeśli fresh read dowodzi inactive/failed z PID 0. Absence jest normalne; changed files/unknown registry są zachowane. Fallback przywracany atomic replace i rozpoznawany po hash przy retry. | `test_install.uninstall_resumes_after_partial_failure_and_is_idempotent`: cache 0755, połowa cleanupu, poprawa, retry przy usuniętym unicie, trzecia próba, identyczny v5 fallback z zachowanym inode/mtime/mode. `uninstall_preserves_changed_unit_and_can_retry`; fresh bundle workflow. |
| Immediate-exit first start P4 | FIXED | Nowy start obserwuje wąskie okno scope acknowledgement/exec: 75 ms i końcowy runtime snapshot. EXITED lub inny niesprawny stan daje failure, receipt i dead pane pozostają do inspect/stop. Bez wielosekundowego opóźnienia i bez obietnicy późniejszej żywotności. | `first_start_immediate_exec_exit_fails_and_stays_inspectable`: 5 realnych CLI startów `exec /bin/sh -c 'exit 23'`, pierwszy rc≠0, list/inspect EXITED+23, explicit stop; także dwa równoczesne starty. Wcześniejszy delayed-exit/repeated-start test zachowany. |
| F8 test quality P4 | FIXED | Real Git writer z hookiem tworzącym jawny index.lock i barierę. Shim przechwytuje env i wymaga OPTIONAL_LOCKS=0 przed exec real Git. Nie ma probabilistycznego hard assertion contention. | `test_fixset.git_read_status_write_deterministic_F8`: 16 read-only status/rev-parse podczas locka, writer commit kończy się poprawnie; usunięta zmienna deterministycznie daje exit 49. Osobny mutation run usuwa ochronę z produkcyjnego kodu i wykrywa regresję. |
| Empty backspace / UTF-8 P4 | FIXED | Parser ignoruje backspace przy pustym buforze. Echo kasuje tylko istniejący znak; incremental UTF-8 decoder obsługuje split multibyte i uwzględnia szerokość znaku. | PTY `empty_backspace_and_split_utf8_echo`: pusty DEL/BS nie kasuje promptu, split ż i podwójny DEL, wynik ok, tylko jedno erase, brak replacement character. Dotychczasowe Unicode/backspace i termios tests zachowane. |
| XDG namespace UX | FIXED | README wyjaśnia oddzielne RID dla XDG_STATE_HOME. Doctor używa nadal batch snapshotu, z szerszym read-only scope query; liczy tylko poprawne receipt-bound obiekty innych RID. `other_runtime_objects: N` jest informacyjne i nie zmienia PASS. Inventory i mutacje pozostają ograniczone do własnego RID. | `xdg_namespace_diagnostic_never_adopts_other_objects`: dwa rzeczywiste namespace, foreign session+scope=2, PASS, własne inventory bez obcych workloads, find/stop obcego ID odmówione, invalid foreign session receipt nie jest liczony. |

Żaden punkt tego polish fix-setu nie pozostał RESIDUAL. Świadome szersze granice
są wyszczególnione w sekcji 9.

## 5. Test results

- **42/42 unit** poza labem; 61 testów wymagających UID pominiętych: [unit.log](evidence/unit.log).
- **103/103 pełny isolated suite**, bez skipów: 42 unit + 61 integration/regression/PTY/adversarial, 173,468 s. [final-validation.log](evidence/final-validation.log).
- **100/100** startów przerwanych SIGKILL odzyskanych; wszystkie lifecycle i nowe races przechodzą.
- **2/2 mutanty wykryte** na osobnych kopiach source w labie: usunięcie line quarantine i usunięcie GIT_OPTIONAL_LOCKS=0.
- Deterministyczne F8: 16 read-only wywołań prawdziwego Git podczas jawnego locka, writer kończy commit poprawnie; missing-env control odmawiany bez losowej race assertion.
- Fresh clean build/install: trzy workflow typów, doctor PASS, CLI/attach/stop, zero-state refusal, changed-file preservation, partial uninstall/retry/repeat i dokładny fallback: PASS.
- Dwa pełne przebiegi 103/103 zakończyły się powodzeniem; końcowe evidence dotyczy czystego HEAD bade59c (drugi przebieg po guardzie PTY).

Pełny suite zachowuje dotychczasowe adversarial/races: utratę SSH/clienta/launchera,
multiattach/takeover, reconnect, concurrent workloads, orphan/corrupt receipts,
stale generation, source-file, missing/replaced repo, child po setsid i full-cgroup
stop. Nowe zmiany lifecycle są dodatkowo pokryte repair/stop race i równoległymi
startami immediate-exit agenta.

## 6. Performance before/after

| PTY 7 repo / 9 workloads | Before p50 / p90 | After p50 / p90 |
|---|---|---|
| Enter → next prompt | 117.59 / 118.31 ms | 37.59 / 38.02 ms |
| d⏎ → destructive prompt | 160.64 / 161.34 ms | 80.69 / 80.90 ms |

Raw paste reproducer: PASS before i after; nowe split-burst i detach regresje również PASS.

| Snapshot | Before median | After median | Subprocesses |
|---|---|---|---|
| 7/6 | 28.23 ms | 27.78 ms | 2 → 2 |
| 200/6 | 53.08 ms | 54.66 ms | 2 → 2 |
| 1000/50 | 274.14 ms | 277.55 ms | 2 → 2 |

Snapshot pozostaje w dotychczasowym zakresie; mały dodatkowy filesystem check nie wprowadza N+1.

| Lifecycle after | Inspect | Attach | Stop | Calls inspect/attach/stop |
|---|---|---|---|---|
| 7 repo / 6 workloads | 28.48 ms | 188.42 ms | 139.39 ms | 2 / 7 / 15 |
| 7 repo / 50 workloads | 145.48 ms | 790.78 ms | 375.17 ms | 2 / 7 / 15 |
| 1000 repo / 50 workloads | 143.93 ms | 791.40 ms | 376.46 ms | 2 / 7 / 15 |

Przy stałych 50 workloadach stop/inspect/attach nie rosną liniowo z liczbą repo. Pełne wyniki i próbki: [performance.json](evidence/performance.json).

Pomiar before używa niezmienionego kodu baseline `ba22947` z `git archive`,
rozpakowanego tylko w HOME labowego UID. Before i after używają tych samych
syntetycznych repo, istniejących workloads i managera. PTY mierzy rzeczywisty
czas wysłania Enter do odebrania następnego promptu: 20 akcji menu, 12 potwierdzeń;
bez odejmowania kosztów i bez stałego polling tick. P50 to mediana, p90 to
uporządkowana próbka na pozycji floor((n−1)×0.9). Snapshot: 9 odczytów na kształt,
liczba subprocessów sprawdzana twardym assertion. Raw samples są w evidence.

Reprodukcja z czystego źródła, bez instalacji ownerowi:

```console
python3 tools/build_v6.py
git archive ba22947b86537e9fc42a6edc3e96c0998f8f93e6 hubi6 config systemd > dist/hubi6-polish-baseline.tar
sudo -n python3 tools/test_v6.py --require-clean-build
```

## 7. Paste safety

Akcja zaczyna się od razu; następny logical read nadal czeka na 80 ms ciszy od
ostatniego odebranego bajtu. Normalny ekran może być już widoczny podczas końcówki
tego okna. Cały burst `w⏎1⏎d⏎yes⏎` zatrzymuje się na workloads, a `1⏎d⏎yes⏎`
na detail. Split burst po pokazaniu następnego ekranu jest również odrzucany.
Ten ostatni test nie zawiera confirmation drain, więc nie maskuje usunięcia
line-level quarantine. Mutation guard rzeczywiście daje czerwony test po jej
usunięciu. Kolejne chunki przedłużają okno; po nim normalne szybkie wpisanie
pojedynczej odpowiedzi działa. Bracketed paste jest ignorowane; po attach/detach
tryb jest uzbrojony ponownie. Destructive prompt pokazuje się dopiero z czystą
kolejką. Termios przywracany przy Ctrl+C, SIGHUP, EOF, hangup i render exception.

## 8. Build/install/uninstall

Fresh clean bundle ma SOURCE.json z rzeczywistym HEAD, dirty=false, hashami plików
i powyższym source digest. Stale-file rebuild regression nadal przechodzi.
W labie zainstalowano bundle poza source checkout, sprawdzono doctor PASS,
Codex/Claude/terminal, CLI JSON, inspect, shared attach, stop i odmowę uninstallu
przy live scope. Usunięto instalację i workspace markers; fallback v5 był wyłącznie
syntetyczną fixture. Osobne testy sprawdzają unverified unit preservation oraz
częściowy uninstall, retry i powtórzenie zakończonego uninstallu.
Runbook wymaga zachowania zewnętrznego bundla na retry po usunięciu prefixu.
Żaden skrypt install/cutover/rollback nie został wykonany na ownerze.

## 9. Residual P1/P2/P3/P4

- **P1: 0 znanych. P2: 0 znanych.** Wszystkie osiem lokalnych punktów tego zadania
  ma implementację i regresję; końcowy niezależny check pozostaje osobnym krokiem.
- **P3:** pełny inventory 1000 repo/50 workloadów nadal kosztuje setki ms; brak
  N+1, lifecycle bez repo discovery. Pełny redesign/profil systemd poza zakresem.
- **P4:** równoczesny SIGKILL launchera i workera w najgorszym punkcie startu może
  pozostawić widoczny recoverable state wymagający explicit stop/reconcile.
- **P4:** historyczne receipts bez live objects są ukrywane; klucze nigdy więcej
  nieuruchomione mogą zachować plik. Brak automatycznego GC osłabiającego ownership.
- **P4:** utrata wszystkich receipts / obce pliki journala wymaga ręcznej
  diagnostyki. Nie zgadujemy ownership po nazwie i nie sweepujemy takich obiektów.
- **P4:** exotic Git insteadOf/SSH host aliases oraz UUID poza repo root lub w
  nieczytelnym workspace pozostają poza resolverem/automatycznym cleanupem.
- Granice zachowane: synchroniczny network refresh, SIGKILL clone staging recovery
  przy następnej operacji; authenticated real-agent resume i private clone przez
  samą aplikację wymagają późniejszego owner smoke. Brak owner credentials w labie.

## 10. Isolation / cleanup

Disposable `hubi6test`, UID **1001**, własne HOME, user@1001, bus, socket, runtime IDs, units, repo fixtures, loopback sshd i limity. Supervisor wykonuje operacje administracyjne wyłącznie po sprawdzeniu UID/HOME/markera labu. Testy lifecycle odmawiają UID 0/1000; kernel probe potwierdził EPERM dla 30 owner PIDs, odmowę dostępu do owner socketu/busa i brak zapisu do owner cgroups, CapEff=0.

Przed teardown: **scopes 0, sessions 0, clients 0, clone units 0**. Po teardown: **processes 0**, socket/HOME/runtime/user/group/linger/cgroup/slice overrides/test sshd usunięte. Zero plików test UID w /tmp, /var/tmp, /dev/shm i /run. Synthetic repos, tmp copies, baseline source i installed bundles były w usuniętym HOME. [cleanup.json](evidence/cleanup.json) oraz końcówka pełnego logu potwierdzają cleanup. Zachowany `dist/hubi6` jest nieinstalowanym build artifact, nie działającym runtime.

Wcześniejszy development lab został również usunięty przed finalnym przebiegiem.
Nowe PTY signal tests są teraz skipowane poza LAB; owner-side unit command
uruchamia 42 testy logiki/config/Git fixtures i pomija 61 przypadków labowych.
Pierwszy development unit run obejmował także Input-only PTY subprocesses na
owner UID (dokładnie własne dzieci, bez wywołań runtime/lifecycle); następnie
dodano guard, aby te nowe PTY testy z sygnałami również wymagały disposable UID.
Wszystkie rzeczywiste workload lifecycle/kill/cleanup/races wykonywano pod UID 1001.

## 11. Live v5 fingerprint

**UNCHANGED / identical=true**: [before](evidence/live-before.json), [after](evidence/live-after.json).

Dziewięć sesji i dziewięć workload scopes bez zmian. Service MainPID **172**, NRestarts **0**, InvocationID **4553644e7cf9456db5ce5bdefad4ca14**, socket inode **28**, installed file hashes bez zmian. Po drodze wykonano również identyczny kontrolny fingerprint.

Fingerprint obejmuje MainPID/InvocationID/NRestarts i cgroup usługi, exact unit
identities/InvocationIDs dziewięciu scopes, session/pane IDs i czasy/PID-y,
inode socketu i SHA256 trzech zainstalowanych plików. Nie porównuje zmiennej listy
potomków bieżącego agenta ani nie deklaruje danych o klientach, których nie zbiera.
Jedynymi operacjami runtime na UID ownera były te read-only odczyty; żadnego
start/stop/attach/reconcile ani cleanupu Hubi na UID 1000.

## 12. GitHub handoff

Gotowe commity zostają wypchnięte do tego samego `feat/hubi-v6-python`; opis istniejącego PR #8 wskazuje ten raport, final HEAD i evidence. Stan remote HEAD i DRAFT jest ponownie weryfikowany po push i podany w końcowym handoffie.

PR #8 pozostaje DRAFT. Bez owner install, cutoveru, migracji, merge i release.
Następny krok: osobny FINAL CLAUDE CHECK na gotowym fix-secie.
