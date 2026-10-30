# Audyt HLC (HomeLab Control) — Etap 0

Stan repozytorium: wersja **1.20.0** (agent 1.6.0), katalog `old/`. Audyt z 30.10.2026, bez zmian w kodzie HLC.
Ścieżki w tym dokumencie są względne do `old/`.

Metoda: przeczytany kod serwera, agenta, skryptów wdrożeniowych i dokumentacji; zestaw testów uruchomiony na kopii repo
(poza `old/`). Na Twojej prawdziwej infrastrukturze niczego nie sprawdzałem — nie mam do niej dostępu, a w workspace nie ma
bazy produkcyjnej.

---

## 0. Podsumowanie

- **Rdzeń HLC jest dobry i trafia w cel nowego projektu**: dokumentacja urządzeń, graf zależności z „co staje, gdy X
  padnie”, alerty z deduplikacją i tłumieniem skutków, „co się zmieniło przed awarią”, runbooki, log problemów,
  procedury awaryjne. To jest dokładnie „unikalna wartość” z punktu 3 zadania.
- **Projekt rozlał się w ~10 kierunków naraz.** Od 1.0.0 (30.09) do 1.20.0 (25.10) wyszło 20 wersji w 25 dni; około
  połowy dotyczyła rzeczy spoza celu (panel Start dla domowników z pogodą, kalendarzem i listą zadań, trzy motywy,
  ikona na iPhone'a, asystent AI, Home Assistant, samoaktualizacja z gita z wyborem Forgejo/Gitei).
  Szacunkowo **40–50% kodu nie służy celowi** (szczegóły w rozdz. 3).
- **Architektura stoi w poprzek nowych zasad:**
  - *jedna instancja = jedna lokalizacja* (osobny LXC dla domu i sklepu), a cel to **jeden panel** do wszystkiego;
  - *agent wszędzie i własne metryki* (historia 2 lata, prognozy), a zasada 4 mówi: nie budujemy własnej bazy metryk;
  - *konsola shell jako root* na hostach Proxmoxa i we wszystkich LXC/VM, a nowa zasada mówi: żadnego dowolnego shella.
- **Bezpieczeństwo: dobre fundamenty, ale kilka poważnych dziur** (rozdz. 4). Najgroźniejsze: shell z kluczem roota
  do całej infrastruktury, sesje bez możliwości unieważnienia, brak limitu prób dla kodu TOTP, domyślnie HTTP
  i instalacja agenta przez `curl http://… | sudo sh`.
- **Brakuje integracji, które są w nowym celu**: Komodo, OpenWrt (DHCP/ARP/Wi-Fi), API PBS. Nie ma ich wcale.
- **Testy**: 187 przechodzi, 7 nie przechodzi wyłącznie przez Windowsa (brak modułu `pwd`, brak `python3` w PATH),
  1 pominięty. Według `docs/STATUS.md` **żadna integracja nie była testowana na prawdziwym Proxmoxie/TrueNAS** —
  tylko na atrapach. Co realnie działa u Ciebie, musisz potwierdzić (pytania w rozdz. 7).

---

## 1. Stack, struktura, dane, sekrety

### Stack

| Warstwa | Technologia |
|---|---|
| Serwer | Python 3.12+, **Flask 3.1** + Jinja, serwer **waitress** (wątki), **APScheduler** w procesie |
| Baza | **SQLite** (WAL), `sqlite3` bez ORM, własne migracje (`MIGRATIONS` w `hlc/db.py`, 14 wersji, 45 tabel) |
| Frontend | vanilla JS (bez frameworka i bez kroku budowania), 9 plików, własny renderer Markdown, CSP z nonce |
| Agent | jeden plik Pythona, tylko biblioteka standardowa, usługa systemd jako **root** |
| Zależności | cryptography (Fernet), argon2-cffi, anthropic, paramiko (SSH), websocket-client (TrueNAS) |
| Wdrożenie | `deploy/create-lxc.sh` (na hoście Proxmoxa) → `deploy/install.sh` (venv + systemd w LXC); alternatywnie Docker |

### Rozmiar

| Część | Linie |
|---|---|
| Serwer (`hlc/**/*.py`) | 13 376 |
| Frontend (JS 3 471 + CSS 971 + szablony 433) | 4 875 |
| Agent | 1 313 |
| Testy | 4 136 |
| Wdrożenie (skrypty, unity systemd, `update.py`) | 739 |
| Dokumentacja (README, CHANGELOG, docs) | 1 446 |
| Około 180 endpointów HTTP w 8 blueprintach (`api.py` … `api4.py` — podział po kolejności powstawania, nie po domenie) | |

Same funkcje spoza celu zajmują: panel Start i widżety ~1 560 linii, asystent AI ~1 300, tryb demo ~510,
shell i zdalna instalacja agenta ~360, samoaktualizacja ~525.

### Struktura

```
hlc/
  __init__.py, config.py, __main__.py   start aplikacji, .env, CLI (serve, genkey, reset-password, backup…)
  db.py, security.py, masking.py         baza + migracje; Fernet, argon2, CSRF, TOTP, role, audyt; maskowanie sekretów
  inventory.py, alerts.py, notify.py     urządzenia + graf zależności; 41 reguł alertów + tłumienie; kanały powiadomień
  ingest.py, metrics.py, timeline.py     raport agenta → stan, metryki, oś czasu
  checks.py, uplink.py, netscan.py       sprawdzenia HTTP/TCP/ping/TLS/DNS, „brak internetu”, skan ARP
  collectors/{proxmox,truenas,registry}  integracje (odczyt) + digest obrazów Dockera
  jobs.py, pve_manage.py, hostrestart.py akcje, aktualizacja ze snapshotem, zarządzanie Proxmoxem, restart hosta
  shell.py, agent_remote.py              konsola SSH, zdalna instalacja agenta
  assistant.py, ai_write.py              asystent AI (czytanie + zapis dokumentacji + propozycje akcji)
  apps.py, widgets.py, web/start.py      panel Start dla domowników
  … oraz cloudflare, ups, restore, reports, availability, ipam, configs, ha, selfupdate, legacy, demo
agent/agent.py                           agent
deploy/                                  instalacja LXC, systemd, update.py (aktualizacja z gita jako root)
```

### Jak działa agent

- **Push** co 30 s: `POST /api/agent/v1/report` z tokenem Bearer (w bazie tylko hash SHA-256 tokena).
- Zbiera: system, dyski i i-węzły, Docker przez socket (stan, health, restarty, digest obrazów, rozmiar warstwy
  zapisywalnej, logi psujących się kontenerów, pliki core), systemd, NFS (zamontowany, zapisywalny, opóźnienie, **ukryte
  dane pod punktem montowania**), apt, SMART, ZFS, UPS (NUT), porty, wybrane pliki konfiguracyjne (z maskowaniem).
- **Akcje**: serwer odsyła w odpowiedzi listę zadań; agent wykonuje tylko te z **lokalnej** białej listy
  (`allow_actions`, `compose_dirs`, `allow_units`, `custom_actions` w `/etc/hlc-agent/config.json`). Serwer nie może
  tej listy zmienić przez raport — zmienia ją dopiero instalator uruchomiony jako root. To dobry wzorzec.
- Odporność: każdy kolektor ma timeout, zawieszony NFS nie blokuje reszty, raporty buforują się na dysku, gdy serwer
  nie odpowiada.
- Instalacja: jednorazowy kod → `curl http://HLC/agent/install/<kod> | sudo sh` albo „jednym kliknięciem” przez SSH /
  `pct exec` / `qm guest exec` (wymaga włączonego shella).

### Jak przechowywane są dane

- Wszystko w jednym pliku **`/var/lib/hlc/hlc.db`** (SQLite) + `uploads/` (załączniki, ikony) + `configs/` (repo git
  z kopiami konfiguracji) + `backups/` (dzienne ZIP-y, 14 sztuk).
- Pola złożone jako **JSON w kolumnach TEXT** (np. `sites.panic`, `devices.ports`, `devices.meta`).
- Identyfikatory: 12 znaków hex z UUID4 — przy łączeniu baz z dwóch lokalizacji kolizje są praktycznie niemożliwe.

### Jak przechowywane są sekrety

- `.env` (`/opt/hlc/.env`, 0600): `HLC_SECRET_KEY` (podpis ciasteczek sesji) i `HLC_FERNET_KEY`.
- W bazie szyfrowane Fernetem: tokeny Proxmoxa i TrueNAS, konfiguracja kanałów powiadomień, sekretne ustawienia
  (klucz API Anthropic, token HA, klucz prywatny SSH HLC, sekret OIDC…), **skarbiec haseł do urządzeń** (`secrets`), sekret TOTP.
- Hasła kont: argon2 (stare hashe werkzeug przehashowywane przy logowaniu).
- Słabość układu: klucz Fernet leży na tej samej maszynie co baza. Kto przejmie serwer HLC, ma wszystko w jawnej
  postaci — łącznie z kluczem SSH do roota na hostach Proxmoxa (rozdz. 4, B1).

---

## 2. Funkcje i ich stan

Legenda stanu:
**✔ kompletne** — kod gotowy, pokryty testami (na atrapach; według `STATUS.md` nie na Twojej infrastrukturze),
**◐ połowiczne** — działa częściowo albo ma znane luki,
**✗ porzucone** — martwy kod albo funkcja wycofana.

Kolumna „Decyzja” to moja propozycja dla nowego projektu (do Twojej akceptacji).

### Rdzeń „wiem, gdzie szukać” (unikalna wartość)

| Funkcja | Stan | Uwagi | Decyzja |
|---|---|---|---|
| Lokalizacje (dom, sklep), podsieci, Wi-Fi, „kartka awaryjna” (procedury per lokalizacja) | ✔ | Procedury jako JSON `[{t, steps[]}]` w `sites.panic` | **Przenieść** (model + dane) |
| Urządzenia: typ, rola („do czego służy”), krytyczność P1–P3, IP, miejsce, numer seryjny, porty, terminy (gwarancja, domena), notatki | ✔ | 20 typów, reguły zagnieżdżania VM/kontener/usługa w hoście | **Przenieść** |
| Zależności twarde/miękkie z typem (zasilanie, sieć, hosting, dane, usługa) i notatką | ✔ | Blokada pętli | **Przenieść** |
| Kable z gniazdami (`links`) | ◐ | Zapisywane, ale **nie biorą udziału w analizie przyczyny**; brak wpinania w gniazdo klikaniem | Przenieść dane; kable jako krawędzie grafu |
| Mapa (płótno, przeciąganie, zagnieżdżanie) | ◐ | Brak automatycznego układu; historia wersji (`doc_history`) bez UI przywracania | Przemyśleć w Etapie 1 |
| Runbook per urządzenie (Markdown) | ✔ | Jedno pole tekstowe na urządzenie | **Przenieść** |
| Log problemów (`issues`: data, tytuł, opis, status, jak naprawiono) | ✔ | Może być powiązany z alertem | **Przenieść** |
| Graf wpływu awarii + tłumienie skutków („TrueNAS leży → dotknięte: Immich, Jellyfin”) | ◐ | Działa po zagnieżdżeniu i zależnościach. Przy kilku awariach naraz przyczyna wybierana dowolnie (nie najgłębsza); kable pomijane | **Przenieść i rozbudować** — to serce Etapu 3 |
| „Co się zmieniło przed awarią” (zmiany z 24 h na urządzeniu i jego zależnościach) | ✔ | Bez AI, z osi czasu | **Przenieść** — odpowiada na „dlaczego” |
| Oś czasu zdarzeń | ✔ | | Przenieść (krótka historia, zgodnie z zasadą 4) |
| Eksport Markdown z maskowaniem sekretów | ✔ | | Podstawa dla eksportu statycznego (HTML/PDF) |
| Pliki/załączniki per urządzenie | ✔ | Serwowane jako załącznik (bez XSS) | Przenieść |
| Skarbiec haseł per urządzenie | ✔ | Fernet, odsłanianie po ponownym haśle + audyt | **Do decyzji** (pytanie 5) |

### Monitoring i integracje

| Funkcja | Stan | Uwagi | Decyzja |
|---|---|---|---|
| Autoodkrywanie z Proxmoxa (węzły, VM, LXC, IP, zasoby), zatwierdzanie nowych, blokada ręcznie wpisanych pól | ✔ | Dobry klient API | **Przenieść** (adapter Proxmox) |
| Kopie: vzdump/PBS widziane przez Proxmoxa (historia zadań vzdump) | ◐ | **Brak API PBS**; token PVEAuditor nie widzi plików kopii, więc kopie rozpoznawane z logów zadań | Zastąpić adapterem PBS |
| Klaster, replikacja, HA (1.20.0) | ✔ | Przygotowane pod drugi serwer | Przenieść do adaptera Proxmox |
| TrueNAS 25.x przez JSON-RPC/WebSocket (pule, datasety, SMART, snapshoty, replikacja, NFS, alerty TrueNAS) | ✔ | Testy tylko na atrapie protokołu | **Przenieść** (adapter TrueNAS) |
| Agent (system, Docker, NFS, apt, SMART, ZFS, UPS…) | ✔ | Bardzo szeroki zakres; część dubluje Proxmox/Komodo/TrueNAS | Odchudzić: apt, reboot-required, NFS (unikalne) |
| Alerty: 41 reguł, progi globalne i per urządzenie, deduplikacja, eskalacja, wyciszenia, okna serwisowe | ✔ | | Przenieść silnik; reguły tylko te, które dotyczą nowych źródeł |
| Powiadomienia: ntfy, Telegram, Discord, e-mail; czekanie na przyczynę przed wysłaniem skutków | ✔ | | **Przenieść** |
| Sprawdzenia HTTP/TCP/ping/TLS/DNS; „z internetu” przez check-host.net albo agenta na VPS | ✔ | | Przenieść (Etap 3); „z internetu” z VPS-a |
| Wykrywanie braku internetu + znak życia (healthchecks.io) | ✔ | | **Przenieść** — „kto pilnuje pilnującego” |
| Wykrywanie nowych urządzeń (ping + ARP z serwera HLC, producent z MAC) | ◐ | Widzi tylko segment L2, w którym stoi LXC | Zastąpić OpenWrt (DHCP/ARP/Wi-Fi); zachować bazę producentów MAC |
| Rejestr IP i portów, konflikty, import nmap | ✔ | | Później (częściowo w autodiscovery) |
| Obserwacja nazw DNS („nazwa prowadzi gdzie indziej”) | ✔ | | Później |
| Historia metryk (48 h / 45 dni / 2 lata), wykresy, prognoza zapełnienia dysku | ✔ | **Sprzeczne z zasadą 4** | **Wyciąć** — metryki z Proxmoxa (RRD) i TrueNAS |
| Zbiorczy widok kontenerów Docker | ✔ | Dubluje Komodo | Zastąpić adapterem Komodo |
| Kopie konfiguracji przez agenta (git + diff) | ✔ | | Później; OpenWrt `sysupgrade -b` to nowa funkcja |
| Cloudflare DNS + diagnoza łańcucha DNS → VPS → tunel → LAN | ✔ | Zależy od tego, czy domena jest w Cloudflare | Później (pytanie 7) |
| UPS przez NUT (alerty) | ✔ | Niesprawdzone na prawdziwym NUT | Później (UPS-y w sklepie: najpierw ping/SNMP) |

### Aktualizacje i akcje

| Funkcja | Stan | Uwagi | Decyzja |
|---|---|---|---|
| Aktualizacje: apt (agent), obrazy Docker (digest vs rejestr), Proxmox (API), release notes z GitHuba | ◐ | **Brak Komodo i OpenWrt** | Etap 4: Komodo zamiast własnego sprawdzania digestów |
| „Aktualizuj bezpiecznie”: snapshot → backup bazy → apt / compose → test zdrowia → „Cofnij do snapshotu” | ✔ | Maszyna stanów w `jobs.py` — dobry wzorzec | Przenieść wzorzec; compose przez Komodo |
| Cofnięcie jednej aplikacji (tagi `hlc-rollback/…`) | ✔ | Dubluje Komodo | Wyciąć |
| Zarządzanie Proxmoxem: start/stop/restart/snapshot/rollback, śledzenie zadań | ✔ | Delegowane do API — zgodne z nowymi zasadami | **Przenieść** (Etap 5) |
| Panel Akcje: naprawy (core dumpy, NFS remount, restart usługi, journald, apt clean), własne przyciski | ✔ | Przez lokalną białą listę agenta | Część później |
| Trzy zamki akcji (globalny przełącznik + per urządzenie + lokalna lista agenta) + audyt | ✔ | | **Przenieść koncepcję** |
| Bezpieczny restart hosta Proxmox z kolejnością startu z zależności | ✔ | Niesprawdzony na żywo | Później |
| Test odtworzenia backupu (tymczasowa VM bez sieci) | ✔ | Niesprawdzony na żywo | Później |
| Automatyczne wyłączanie VM przy kończącej się baterii UPS | ✔ | Dubluje NUT/upsmon | Wyciąć |
| **Konsola shell przez SSH** (host Proxmoxa → `pct exec` / `qm guest exec`) | ✔ | **Sprzeczne z zasadą „żadnego dowolnego shella”** | **Wyciąć** |
| Instalacja/aktualizacja agenta „jednym kliknięciem” przez shell | ◐ | Wymaga shella | Wyciąć; agent instalowany ręcznie albo przez Komodo/Ansible |

### Konta, dostęp, system

| Funkcja | Stan | Uwagi | Decyzja |
|---|---|---|---|
| Logowanie hasłem (argon2), CSRF, role admin / tylko odczyt / domownik | ✔ | Słabe sesje (rozdz. 4) | Przepisać sesje; role admin + podgląd |
| TOTP | ◐ | Brak kodu QR, brak limitu prób, brak ochrony przed ponownym użyciem kodu | Przepisać |
| SSO / OIDC (Authentik, Pocket ID…), PKCE, role z grup | ✔ | Starannie napisane | **Przenieść**, jeśli wybierzemy OIDC |
| Pierwsze uruchomienie z jednorazowym kodem | ✔ | | Przenieść |
| `reset-password` z konsoli + `docs/ODZYSKIWANIE.md` | ✔ | | Przenieść |
| Kopia aplikacji (ZIP co dobę, 14 sztuk) | ✔ | | Przenieść + sync poza serwer |
| Samoaktualizacja z gita (GitHub/Forgejo/Gitea), usługa root, wybór źródła w panelu | ✔ | Duża złożoność, ryzyko eskalacji (rozdz. 4) | **Wyciąć** — obraz Dockera albo `git pull` + restart |
| Wiele lokalizacji w jednej instancji | ◐ | Kod obsługuje, ale wdrożenie zakłada osobną instancję na lokalizację | **Jedna instancja dla wszystkiego** |
| Dokumentacja serwera zapasowego (klaster + replikacja + HA + QDevice) | ✔ | | Przenieść treść do README |

### Poza celem

| Funkcja | Stan | Decyzja |
|---|---|---|
| Panel Start dla domowników: zegar, pogoda, kalendarz ICS, wyszukiwarka, lista zadań, „Zgłoś problem”, widżet Home Assistant, ikona iPhone, publiczny dostęp przez Pangolina | ✔ | **Wyciąć**; rola „podgląd” zamiast domownika. „Zgłoś problem” → `POMYSLY.md` |
| Asystent AI (Claude): diagnoza, zapis dokumentacji z „Cofnij”, import notatek .md, propozycje akcji i poleceń shell, licznik kosztów | ✔ | **Wyciąć** → `POMYSLY.md` (ewentualnie później i tylko do odczytu) |
| Integracja z Home Assistant (REST + MQTT discovery) | ✔ | `POMYSLY.md` |
| Podsumowanie tygodnia, raport dostępności | ✔ | Później |
| Trzy motywy, własny kolor główny | ✔ | Jeden motyw (ciemny/jasny z systemu) |
| Tryb demo z symulatorem | ✔ | Wyciąć; zastąpić danymi testowymi |
| Migracja ze starej Mapy Serwerowni + konwersja starego skarbca | ✗ | Martwy kod (zdecydowałeś: bez importu) |
| Widok „dla taty” | ✗ | Usunięty w HLC |
| Audyt UX #13 (pierwsze kroki), #14 (objaśnienia reguł) | ◐ | Niezrobione; lekcje z audytu UX warto przenieść |
| Zrzuty ekranu w `docs/screenshots` | ◐ | Nieaktualne (stara nawigacja sprzed 1.12) |

---

## 3. Co dubluje Proxmoxa, Komodo i gotowe narzędzia

| Co robi HLC | Kto to już robi | Propozycja |
|---|---|---|
| Lista kontenerów, stan, restart, `compose pull/up`, wykrywanie nowszych obrazów (digest z rejestru), cofnięcie aplikacji | **Komodo** (stacki, deploy, sprawdzanie aktualizacji obrazów, auto-update, terminal) | Adapter Komodo: odczyt + delegowane akcje. Wyciąć `registry.py`, akcje compose w agencie, rollback tagami |
| Historia CPU/RAM/dysków, wykresy, prognoza | **Proxmox RRD** (VM/LXC/węzły), TrueNAS (pule, dyski) | Wyciąć `metrics.py`; pokazywać bieżące wartości z API; prognozę zapełnienia → `POMYSLY.md` |
| Start/stop/snapshot/rollback VM | **API Proxmoxa** (HLC już deleguje) | Zostaje, tylko jako cienki adapter |
| Konsola shell | **Proxmox** (xterm.js), **Komodo** (terminal) | Wyciąć; link „otwórz w Proxmoxie/Komodo” |
| Kolejność startu maszyn | **Proxmox** (Start/Shutdown order) | Zostawić Proxmoxowi; ewentualnie podpowiedź z grafu zależności |
| Kopie: wiek ostatniej kopii z logów vzdump | **PBS API** (snapshoty per grupa, weryfikacja) | Adapter PBS: „ostatni udany backup zasobu” |
| Test odtworzenia | PBS verify (sprawdza integralność, nie start maszyny) | Później |
| Wyłączanie przy UPS | **NUT / upsmon**, Proxmox | Wyciąć |
| Skan ARP z serwera HLC | **OpenWrt** (DHCP leases, ARP, klienci Wi-Fi) | Adapter OpenWrt |
| Sprawdzenia HTTP/TCP/TLS, „z internetu” | Uptime Kuma / Gatus | **Zostaje w panelu** (wymóg Etapu 3), ale prosto; dodatkowy runner na VPS |
| Samoaktualizacja aplikacji | Komodo / Docker / Watchtower | Wyciąć |
| Skarbiec haseł | Bitwarden/Vaultwarden | Do decyzji (pytanie 5) |
| Kopie konfiguracji plików | Git na maszynie, Komodo (compose w repo) | Później; OpenWrt `sysupgrade -b` jako osobna akcja |
| Agent: system, dyski, Docker, SMART, ZFS | Proxmox, Komodo (Periphery), TrueNAS | Agent tylko tam, gdzie API nie wystarcza: **apt, reboot-required, NFS** |

---

## 4. Bezpieczeństwo

Najpierw to, co jest **dobrze zrobione** i warto przenieść: CSRF na każdym zapisie, CSP z nonce, argon2, Fernet
dla sekretów w bazie, hashe tokenów agentów, jednorazowy kod pierwszego uruchomienia, OIDC z PKCE i bez łączenia kont
po nazwie, lokalna biała lista akcji w agencie, trzy zamki dla akcji, ponowne podanie hasła przy zmianie uprawnień,
audit log, klient Proxmoxa z weryfikacją TLS i bez przekierowań, pliki serwowane jako załącznik.

### Problemy

Waga: **W** wysoka, **Ś** średnia, **N** niska.

| # | Waga | Problem | Gdzie | Skutek |
|---|---|---|---|---|
| B0 | **W** | **Panel jest dziś dostępny z internetu pod `hlc.serwispowers.com`** (odpowiedź z 30.10). Jeśli to pełny HLC, a nie tylko panel Start, to wszystkie punkty poniżej (TOTP bez limitu, shell z rootem, sesje 14 dni) są wystawione publicznie | konfiguracja Pangolina | Atak z internetu na panel z kluczami do całej infrastruktury |
| B1 | **W** | **Dowolny shell jako root** na hostach Proxmoxa, a przez `pct exec` / `qm guest exec` w każdym LXC i VM. Klucz prywatny SSH leży w bazie, a klucz do jej odszyfrowania w `.env` na tym samym serwerze. „Niebezpieczne polecenia” to czarna lista wyrażeń regularnych — łatwa do obejścia (np. `base64 -d \| sh`). Wykonanie polecenia **nie wymaga ponownego hasła**, wystarczy sesja admina i raz włączony przełącznik | `hlc/shell.py:29-43`, `hlc/web/api4.py:186-197` | Przejęcie HLC albo skradzione ciasteczko = root na całej infrastrukturze |
| B2 | **W** | **Sesje nie do unieważnienia**: podpisane ciasteczko Flask (stan po stronie klienta), ważne 14 dni. Nie ma listy sesji, „wyloguj wszędzie” jest niemożliwe, zmiana hasła i wyłączenie TOTP nie unieważniają istniejących sesji | `hlc/__init__.py:59-62`, `hlc/web/api2.py:143-156` | Skradzione ciasteczko działa do 14 dni mimo zmiany hasła |
| B3 | **W** | **Domyślnie HTTP**: `HLC_SECURE_COOKIES=0`, `HLC_PUBLIC_URL=http://…:8088`. Hasło, ciasteczko sesji i tokeny agentów lecą w LAN otwartym tekstem | `hlc/config.py:34`, `.env.example` | Podsłuch w sieci sklepu/domu = przejęcie sesji |
| B4 | **W** | **Instalacja agenta `curl http://… \| sudo sh`**; skrypt i kod agenta pobierane po HTTP. Agent działa jako root | `hlc/web/agent_api.py:52-139`, `hlc/web/api4.py:317` | Atak MITM w LAN przy instalacji = root na maszynie |
| B5 | **W** | **Brak limitu prób kodu TOTP**: nieudana próba trafia do licznika, ale `/login/totp` go nie sprawdza. Brak ochrony przed ponownym użyciem kodu | `hlc/web/auth.py:109-123` | Po poznaniu hasła 2FA da się złamać zgadywaniem |
| B6 | **W** | **Asystent AI domyślnie w trybie `full`**: czyta niezaufane dane (logi kontenerów, importowane notatki, wyniki poleceń), a mimo to sam zmienia dokumentację i zleca akcje agenta w granicach przełączników | `hlc/security.py:275`, `hlc/ai_write.py` | Wstrzyknięcie poleceń przez log/notatkę → zmiany i akcje bez Twojej wiedzy |
| B7 | Ś | **Łańcuch do roota przez nazwę urządzenia**: nazwa (także nadana przez AI, bez filtrowania nowej linii) trafia do komentarza w skrypcie instalacyjnym uruchamianym jako root | `hlc/web/agent_api.py:74`, `hlc/ai_write.py:131-139` | Przy B6 + kliknięciu „zainstaluj agenta” → wykonanie kodu jako root |
| B8 | Ś | **Eskalacja `hlc` → root przy samoaktualizacji**: `install.sh` oddaje cały `/opt/hlc` (razem z `deploy/update.py`, `install.sh` i `.git/config`) użytkownikowi `hlc`, a `hlc-update.service` uruchamia ten kod jako root. Ochronę zapewnia wyłącznie sandbox systemd (`ProtectSystem=strict`) | `deploy/install.sh:55`, `deploy/hlc-update.service:8` | Wyjście procesu poza sandbox albo polecenie uruchomione jako `hlc` = root w kontenerze |
| B9 | Ś | **Limit prób logowania w pamięci procesu, per IP**. Brak obsługi `X-Forwarded-For`, więc za reverse proxy wszyscy mają IP proxy: limit staje się globalny (łatwa blokada logowania), a audyt zapisuje IP proxy. Brak limitu na `/api/reauth` | `hlc/web/auth.py:13-20, 145-155` | Blokada logowania, bezużyteczne IP w audycie |
| B10 | Ś | **SSRF przez agenta**: serwer może zlecić agentowi zapytanie HTTP lub połączenie TCP pod dowolny adres i dostaje wynik | `agent/agent.py:827-856` | Przejęty serwer skanuje sieci, w których stoją agenci (także z VPS-a) |
| B11 | Ś | Instalator domyślnie wpisuje agentowi `apt_upgrade`, `compose_update`, `compose_rollback` do lokalnej listy dozwolonych | `hlc/security.py:290` | Lokalna lista przestaje być świadomą decyzją właściciela maszyny |
| B12 | Ś | **Publiczny panel Start przez Pangolina**: blokada reszty panelu opiera się na nagłówkach `Host`/`X-Forwarded-Host` | `hlc/security.py:181-221` | Sprzeczne z nowym wymaganiem; ryzyko przy błędnej konfiguracji proxy |
| B13 | Ś | **Skarbiec haseł do urządzeń w panelu** — kolejny cel ataku; klucz obok bazy | tabela `secrets` | Wyciek wszystkich haseł przy przejęciu serwera |
| B14 | N | Nazwa snapshotu Proxmoxa w ścieżce URL przez `quote()` bez `safe=''` (`/` i `..` przechodzą) | `hlc/pve_manage.py:73-75` | Ryzyko teoretyczne, tylko dla admina |
| B15 | N | Różny czas odpowiedzi dla istniejącego i nieistniejącego loginu (argon2 liczony tylko dla istniejącego) | `hlc/web/auth.py:72-73` | Ujawnienie, czy dany login istnieje |

### Wnioski dla nowego projektu

1. Żadnego shella ani SSH z panelu. Akcje tylko przez API (Proxmox, Komodo, OpenWrt) z tokenami o minimalnych uprawnieniach.
2. Sesje po stronie serwera (tabela sesji): unieważnianie przy zmianie hasła/2FA, „wyloguj wszędzie”, krótszy czas życia.
3. HTTPS od pierwszego dnia (reverse proxy z certyfikatem z wewnętrznego CA albo DNS-01 na `serwispowers.com`) i `Secure` na ciasteczkach.
4. TOTP z limitem prób, ochroną przed ponownym użyciem kodu i kodem QR; limit prób po koncie i po IP (z poprawnym IP za proxy).
5. Agent instalowany z pliku z sumą kontrolną albo przez Komodo/Ansible; komunikacja tylko po HTTPS; bez zlecania dowolnych sprawdzeń.
6. Bez samoaktualizacji z uprawnieniami roota.
7. Brak AI z prawem zapisu.

---

## 5. Co warto przenieść

### Kod (do przeniesienia z adaptacją, nie 1:1)

| Element | Plik | Dlaczego |
|---|---|---|
| Klient API Proxmoxa (TLS, brak przekierowań, limit rozmiaru, zapasowe węzły, brak powtórki zapisu po timeoucie) + odkrywanie gości i IP + odczyt klastra/replikacji/HA | `hlc/collectors/proxmox.py` | Solidny, ostrożny, gotowy na drugi serwer |
| Klient TrueNAS JSON-RPC po WebSocket | `hlc/collectors/truenas.py` | Właściwe API dla 25.x |
| Graf zależności: `impact()`, `upstream()`, blokada pętli, reguły zagnieżdżania | `hlc/inventory.py` | Podstawa analizy przyczyny |
| Silnik alertów: odcisk (deduplikacja), `reconcile`, tłumienie, wyciszenia, okna serwisowe | `hlc/alerts.py` | Do rozbudowy o najgłębszą przyczynę i kable |
| Powiadomienia z czekaniem na przyczynę (`group_wait`) | `hlc/notify.py` | Mały, działa |
| „Co się zmieniło przed awarią” | `hlc/changes.py` | Odpowiada na „dlaczego” |
| Sprawdzenia HTTP/TCP/ping/TLS/DNS, wykrywanie braku internetu, znak życia | `hlc/checks.py`, `hlc/uplink.py` | Etap 3 |
| Maskowanie sekretów | `hlc/masking.py` | Eksport statyczny |
| Maszyna stanów zadań (krok po kroku, log, audyt) | `hlc/jobs.py` | Wzorzec dla akcji z Etapu 5 |
| OIDC z PKCE, role z grup | `hlc/sso.py` | Jeśli wybierzemy OIDC |
| argon2, CSRF, nagłówki bezpieczeństwa z CSP | `hlc/security.py`, `hlc/__init__.py` | Fundament |
| Agent: szkielet (timeouty, `guarded`, bufor, lokalna biała lista), kolektory apt / reboot-required / NFS z ukrytymi danymi | `agent/agent.py` | Tylko te części |
| Producent po MAC (baza IEEE) | `hlc/netscan.py` | Do autodiscovery z OpenWrt |
| Odmiana liczebników po polsku | `hlc/textutil.py` | Drobiazg, ale przydatny |

### Modele danych

Lokalizacja, urządzenie (z rolą, krytycznością, zagnieżdżeniem), zależność (twarda/miękka + typ), kabel z gniazdami,
log problemów, procedury awaryjne per lokalizacja, alert z `suppressed_by`, wyciszenie, zdarzenie osi czasu, zadanie
(job), audit log. W Etapie 1 zaproponuję poprawki: runbook jako osobna encja (wiele runbooków, powiązanie z wieloma
urządzeniami), procedury awaryjne jako tabela zamiast JSON w kolumnie, incydent łączący alert(y) z wpisem w logu problemów.

### UI

- Tokeny projektu (`hlc/static/css/tokens.css`): statusy z kolorem **i kształtem** (kółko, trójkąt, ośmiokąt, pusty
  pierścień), kontrast min. 4,5:1, jeden krój pisma, mono dla IP i ścieżek.
- Wnioski z `docs/AUDYT-UX.md`: na pulpicie najpierw jedno zdanie o stanie, potem „Wymaga uwagi” zgrupowane po urządzeniu,
  liczby na końcu; najwyżej 5 działów nawigacji; przycisk naprawy przy problemie, a nie na osobnej stronie.
- Układ karty urządzenia (przegląd, zależności, runbook, log problemów).
- Mały renderer Markdown z escapowaniem (`hlc/static/js/core.js:179`).

### Dokumentacja

`docs/ODZYSKIWANIE.md` i `docs/ZAPASOWY-SERWER.md` — treść do README nowego projektu (dostosowana do VM zamiast LXC).

---

## 6. Dane do migracji

### Gdzie są dane

Bazy produkcyjnej **nie ma w workspace**. Według dokumentacji każda lokalizacja ma **własną instancję**, więc
prawdopodobnie są **dwie bazy** (dom i sklep), każda w osobnym LXC:

- `/var/lib/hlc/hlc.db` (SQLite; przed kopiowaniem zatrzymać usługę albo użyć `python -m hlc backup`, który robi spójną kopię),
- `/var/lib/hlc/uploads/` (załączniki),
- `/opt/hlc/.env` → `HLC_FERNET_KEY` (bez niego nie odszyfrujemy skarbca, Wi-Fi ani tokenów).

Dodatkowo może istnieć stara **Mapa Serwerowni** (`mapa.db`: 28 urządzeń, 18 kabli, 3 zgłoszenia według `docs/PLAN.md`),
której nie importowałeś do HLC. Jeśli czegoś stamtąd nie przepisałeś ręcznie, można to też zmigrować — `hlc/legacy.py` zna jej format.

### Co i w jakim formacie

| Dane | Tabela HLC | Format | Migrować? |
|---|---|---|---|
| Lokalizacje | `sites` | `name`, `call` (do kogo dzwonić), `subnets` JSON, `wifi` JSON (hasła jako `{"hlc_enc": "<Fernet>"}`) | **Tak** |
| Procedury awaryjne | `sites.panic` | JSON `[{"t": "tytuł", "steps": ["krok", …]}]` | **Tak** → osobna tabela |
| Urządzenia | `devices` | Kolumny dokumentacji: `name, kind, ip, crit (1–3), doc_status, purpose, access, backup, restore_tested, place, serial, notes, parent_id, x/y/w`; JSON: `ports [{port, proto, desc}]`, `expiry [{date, label}]`, `cost`, `panel` (gniazda), `meta` | **Tak** (pola monitoringu `pve_*`, `live_*` odtworzy autodiscovery) |
| Runbooki | `devices.runbook` | Markdown, jeden na urządzenie | **Tak** → osobna encja |
| Zależności | `deps` | `device_id → parent_id`, `type` (zasilanie/siec/hosting/dane/usluga), `hard` 0/1, `note` | **Tak** |
| Kable | `links` | `a`, `ap` (gniazdo), `b`, `bp` | **Tak** |
| Log problemów | `issues` | `date, title, desc, status (open/closed), fix, closed, source (manual/alert/ai), alert_id` | **Tak** |
| Załączniki | `files` + `uploads/` | metadane + pliki | **Tak** |
| Skarbiec haseł | `secrets` | `label, desc, blob_enc` (Fernet) | Do decyzji (pytanie 5) |
| Nazwy hostów urządzeń | `dns_names` | `name → device_id` | Tak (jako aliasy) |
| Adresy MAC | `net_hosts` | `mac, ip, hostname, vendor, device_id` | Tak (przypisanie MAC do urządzeń pomoże autodiscovery z OpenWrt) |
| Sprawdzenia | `checks` | `type, target, scope, interval, params` | Tak (definicje, bez stanu) |
| Kanały powiadomień | `channels` | `type`, `config_enc` (Fernet) | Opcjonalnie (łatwo wpisać ponownie) |
| Kafelki aplikacji | `apps` | `name, url, icon, device_id, container` | Opcjonalnie (adresy aplikacji przydadzą się w katalogu aplikacji) |
| Historia alertów i zdarzeń | `alerts`, `events` | | Opcjonalnie (do osi czasu incydentów) |
| Historia dokumentacji | `doc_history` | migawki stanu lokalizacji (JSON) | Nie (archiwalnie zostaje w kopii) |
| Konta | `users` | | **Nie** — nowe konta z 2FA |
| Metryki, stan agentów, tokeny, shell, AI, panel Start, raporty | wiele tabel | | **Nie** |

---

## 7. Odpowiedzi i decyzje (30.10.2026)

| # | Odpowiedź | Co z tego wynika |
|---|---|---|
| 1 | Jest **jedna instancja** HLC | Migracja z jednej bazy `hlc.db` (+ `uploads/`, `HLC_FERNET_KEY`). Kopię będę potrzebował dopiero w Etapie 2 |
| 2 | Wszystko z Mapy Serwerowni jest już w HLC | `mapa.db` pomijamy; `hlc/legacy.py` nie jest potrzebny |
| 3 | „Był taki chaos, że ciężko powiedzieć”. Panel Start z pogodą miał być pulpitem. **Przy awarii nie było wiadomo, jak tego szukać** | To najważniejszy wniosek z audytu i główne kryterium nowego projektu: ekran awarii prowadzi od objawu do przyczyny, runbooka i historii, bez szukania po zakładkach. Pulpit nowego panelu to jeden ekran startowy (stan całości, co wymaga uwagi, linki do aplikacji), a nie osobny panel dla domowników |
| 4 | Hasła zostają w panelu („w razie co”) | Skarbiec przenosimy: szyfrowany, odsłanianie po ponownym haśle/2FA, każde odsłonięcie w audycie, dostępny tylko dla admina (nie dla roli „podgląd”) |
| 5 | Asystent AI i panel Start idą do `POMYSLY.md` (na razie) | Nie wchodzą do nowego projektu. Pogoda i kalendarz też idą do `POMYSLY.md` |
| 6 | DNS jest w Cloudflare; panel działa pod `hlc.serwispowers.com` | Nowy panel tylko z LAN + VPN. Obecne wystawienie HLC: patrz B0 |
| 7 | Pytanie o stack niejasne | Propozycja z uzasadnieniem w Etapie 1 |
| — | Sklepu nie robimy w tym panelu. Jeśli sklep dostanie panel, to jako osobno postawiona instancja, z lokalizacją wybraną w ustawieniach | Jedna instancja = jedna lokalizacja (ustawienie). Ten panel obejmuje dom + VPS. Model danych nie potrzebuje wielu lokalizacji naraz |

## 8. Pytania z pierwszej wersji audytu

1. **Skąd wziąć dane?** Ile masz działających instancji HLC (dom, sklep — dwie?) i czy możesz skopiować z każdej
   `hlc.db` + `uploads/` + `HLC_FERNET_KEY` do katalogu poza repo (np. `new/_migracja/`, dodany do `.gitignore`)?
2. **Mapa Serwerowni**: czy stara `mapa.db` jeszcze istnieje i czy wszystko z niej (28 urządzeń, 18 kabli) jest już w HLC?
3. **Co działało naprawdę?** Które funkcje HLC używałeś na co dzień i działały na Twoim Proxmoxie/TrueNAS? (Szczególnie:
   autodiscovery z Proxmoxa, TrueNAS, alerty z tłumieniem, powiadomienia — który kanał, agent na których maszynach.)
4. **Co Cię najbardziej irytowało** w HLC poza tym, że „rozjechał się w wielu kierunkach”? (To pomoże ustawić priorytety UI.)
5. **Skarbiec haseł**: przenosimy hasła do urządzeń do nowego panelu, czy wolisz je trzymać w menedżerze haseł
   (np. Vaultwarden/Bitwarden), a w panelu tylko odnośnik „gdzie jest hasło”? Ja polecam to drugie (mniejszy cel ataku).
6. **Asystent AI i panel Start**: potwierdzasz, że oba idą do `POMYSLY.md`, a nie do nowego projektu?
7. **Cloudflare**: czy `serwispowers.com` ma DNS w Cloudflare? (Decyduje o tym, czy diagnoza łańcucha DNS → VPS ma sens.)
8. **Stack**: czy masz preferencje (zostać przy Pythonie, czy coś innego, np. Go/TypeScript)? Jeśli nie, zaproponuję i uzasadnię w Etapie 1.
