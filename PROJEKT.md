# Projekt nowego panelu — Etap 1

Stan: propozycja do akceptacji, 30.10.2026. Podstawa: [AUDYT.md](AUDYT.md) i zaakceptowana makieta UI
(https://claude.ai/artifact/ETQboaZuDXtj8F1ZLVTGPS — pulpit w wersji **B**).

Cel, do którego odnosi się każda decyzja:

> Jeden panel do mojej infrastruktury: widzę w nim wszystkie urządzenia, aplikacje, VM-ki i ich aktualizacje, mogę nimi
> zarządzać, a gdy coś padnie albo się zmieni, w minutę wiem **co, dlaczego i gdzie szukać**.

---

## 0. Ustalenia, które już zapadły

| Temat | Decyzja |
|---|---|
| Zakres | Dom + VPS. Sklep nie wchodzi; jeśli kiedyś dostanie panel, to osobna instancja z lokalizacją w ustawieniach |
| Pulpit | Wersja B z makiety: pulpit jak ekran systemu — ikony aplikacji ze stanem, widżet „Gdzie jest problem” (warstwy), dok na dole |
| Tablet | Pulpit ma działać na tablecie na ścianie (obok Home Assistanta) — tryb kiosku, dotyk, poziomo i pionowo |
| Nawigacja | 5 działów: Pulpit · Infrastruktura (Mapa, Proxmox, TrueNAS, Docker, Sieć) · Incydenty · Aktualizacje · Procedury |
| Sieć | AX23 v1 → OpenWrt (router, pełny adapter); AX10 zostaje na fabrycznym oprogramowaniu (AP, adapter TP-Link tylko do odczytu) |
| Hasła do urządzeń | Zostają w panelu (skarbiec), tylko dla admina, odsłanianie po haśle/2FA, każde w audycie |
| Poza zakresem | Asystent AI, panel Start z pogodą/kalendarzem, widok domownika — w [POMYSLY.md](POMYSLY.md) |
| Shell | Żadnego. Akcje wyłącznie przez API (Proxmox, Komodo, OpenWrt) albo lokalną białą listę agenta |
| Powiadomienia | **Discord (webhook)** na start |
| Ustawienia urządzeń | **Minimum ustawień na pojedynczym urządzeniu** — zasada nadrzędna, patrz niżej (warunek akceptacji projektu) |

### Zasada nadrzędna: prawie nic do ustawiania na pojedynczym urządzeniu

HLC zawiódł, bo każde urządzenie wymagało osobnej konfiguracji (agent, sposób monitorowania, progi, akcje, aktualizacje,
test odtworzenia, sprawdzenia…). W Denali:

1. **Urządzenia przychodzą same.** Proxmox, Komodo, TrueNAS i router zgłaszają maszyny, stacki, udziały i klientów sieci.
   Nowe urządzenie zatwierdzasz jednym kliknięciem (albo hurtem) — bez formularza.
2. **Wymagane jest tylko zatwierdzenie.** Nazwa, typ, IP, adres MAC, „na czym działa” — z integracji. Rola, krytyczność,
   miejsce, runbook — opcjonalne, uzupełniasz, kiedy chcesz.
3. **Zależności wykrywane, nie wpisywane**, wszędzie gdzie się da: VM → host (Proxmox), stack → serwer (Komodo),
   montaż NFS → udział w TrueNAS (agent + TrueNAS), urządzenie → port routera / AP (OpenWrt, TP-Link).
   Ręcznie dopisujesz tylko to, czego nikt nie wie (np. „switch zasilany z UPS-a”).
4. **Monitorowanie bez decyzji**: każde urządzenie z IP dostaje ping, każda aplikacja z adresem — sprawdzenie HTTP,
   każdy obiekt z integracji — jej stan. Nie ma pola „sposób monitorowania”.
5. **Ustawienia na poziomie typu, nie urządzenia.** Progi alertów, dozwolone akcje i sposób aktualizacji ustawiasz
   raz dla **profilu** (np. „VM z Dockerem”, „stack Komodo”, „AP”, „VM krytyczna — bez akcji”). Urządzenie dziedziczy
   profil po typie; wyjątek na pojedynczym urządzeniu to świadoma decyzja, widoczna jako „inne niż profil”.
   (To realizuje wymóg „akcje włączane per urządzenie” — przez profil, z wyjątkami.)
6. **Agent tylko tam, gdzie API nie wystarcza**, instalowany jednym poleceniem bez pytań; konfiguracja agenta przychodzi
   z profilu.

**Kryterium:** nowa VM postawiona w Proxmoxie z Dockerem w Komodo pojawia się w Denali z hostem, stackami, sprawdzeniem
i profilem po **jednym kliknięciu „Zatwierdź”**, bez otwierania żadnego formularza.

---

## 1. Nazwa

**Denali** (decyzja z 30.10.2026). Adres: `denali.serwispowers.com` (tylko w LAN / przez Tailscale).
Nazwa repozytorium, pakietu Pythona i obrazu Dockera: `denali`.

---

## 2. Stack

**Zostajemy przy Pythonie**, ale zmieniamy prawie wszystko wokół niego.

| Warstwa | Wybór | Dlaczego |
|---|---|---|
| Język | **Python 3.12** | Przenosimy sprawdzony kod z HLC (klienci Proxmox i TrueNAS, graf zależności, alerty, maskowanie, OIDC, agent). Integracje to głównie wywołania HTTP — Python wystarcza z zapasem |
| Serwer HTTP | **FastAPI** + uvicorn (zamiast Flask + waitress) | Adaptery odpytują kilka systemów naraz — `async` + `httpx` robi to równolegle bez wątków. Walidacja danych z adapterów przez Pydantic. Wbudowany opis API (przydaje się do testów) |
| Baza | **SQLite (WAL)** + SQLAlchemy 2 (Core) + **Alembic** | Jeden plik = łatwy backup i restore (wymóg). Kilkadziesiąt urządzeń to dla SQLite nic. Alembic zamiast ręcznych migracji z HLC |
| Interfejs | Szablony **Jinja** renderowane na serwerze + **htmx** + odświeżanie przez **SSE** | Bez kroku budowania i bez frameworka JS. Tablet dostaje zmiany na żywo przez SSE. Te same szablony generują eksport statyczny procedur |
| Wygląd | Własne tokeny CSS z makiety (statusy kolorem i kształtem, kontrast ≥ 4,5:1), czcionka IBM Plex Sans/Mono hostowana lokalnie | Panel nie może zależeć od Google Fonts, gdy padnie internet |
| Zadania w tle | Osobny proces **worker** (asyncio, harmonogram w kodzie) | Odpytywanie integracji nie blokuje UI; restart workera nie wyłącza panelu |
| Eksport PDF | **WeasyPrint** (w kontenerze) | PDF procedur do druku i do telefonu |
| Agent | Jeden plik Pythona (stdlib), odchudzony agent z HLC | Tylko apt, reboot-required, NFS (z wykrywaniem ukrytych danych) |
| Testy | pytest + nagrane odpowiedzi API (fixture) dla każdego adaptera | Każdy adapter testowany na prawdziwych odpowiedziach, nie na wymyślonych |
| Wdrożenie | **Docker Compose** na osobnej VM: `app` + `worker`; HTTPS robi istniejący **Nginx Proxy Manager** | Obraz = powtarzalna instalacja; łatwe uruchomienie na zapasowym serwerze |

Odrzucone: **Go** (szybszy i jeden plik binarny, ale trzeba by pisać od zera klientów, które już mamy); **SPA w React/Svelte**
(krok budowania i więcej kodu, a htmx wystarcza dla paneli i tabletu); **Postgres** (dodatkowa usługa do backupu bez zysku przy tej skali).

---

## 3. Model danych

Zasady:
- **Urządzenie** to wszystko, co ma stan albo dokumentację: host, VM, LXC, stack Dockera, usługa, router, AP, NAS, VPS, UPS.
- Integracje nie tworzą własnych kopii urządzeń — obiekt z integracji jest **przypięty** do urządzenia przez `external_ref`.
- **Incydent = wpis w logu problemów.** Awaria wykryta automatycznie tworzy incydent; ręczny wpis też jest incydentem. Jedna historia zamiast dwóch.
- Dane z integracji to tylko **ostatni znany stan** + krótka historia zdarzeń (90 dni). Bez własnych szeregów czasowych.

### Encje

```
settings              klucz → wartość (nazwa lokalizacji, progi, kanały domyślne…)

-- dokumentacja ------------------------------------------------------------
device                id, name, kind, role (do czego służy), criticality P1–P3, parent_id (na czym działa),
                      place (gdzie fizycznie), serial, notes_md, icon, app_url (ikona na pulpicie),
                      pulpit_group (codzien | zarzadzanie | brak), profile_id (puste = profil po typie), created_at, updated_at
device_address        device_id, kind (ip | mac | hostname), value, source (manual | integracja), primary
dependency            id, device_id → depends_on_id, type (zasilanie | siec | hosting | dane | usluga),
                      hard (bool), note
cable                 id, a_device_id, a_port, b_device_id, b_port, label (np. „SW-ZAP, niebieski”);
                      strona A = bliżej routera, strona B = wpięte urządzenie (B zależy od A)
runbook               id, title, body_md, updated_at, updated_by
runbook_device        runbook_id, device_id          (jeden runbook może dotyczyć wielu urządzeń)
procedure             id, title, scope (infrastruktura | panel), intro_md, trigger (np. device_down:pve), last_tested_at
procedure_step        procedure_id, position, title, body_md
procedure_device      procedure_id, device_id
contact               id, name, phone, note
procedure_contact     procedure_id, contact_id
file                  id, device_id, name, mime, size, path
vault_secret          id, device_id, label, ciphertext, updated_at        (tylko admin; odsłonięcie = wpis w audycie)

-- incydenty i alerty ------------------------------------------------------
incident              id, title, root_device_id, status (trwa | potwierdzony | zamkniety), started_at, ended_at,
                      acked_by, cause_md, fix_md, source (auto | reczny | hlc), severity
incident_device       incident_id, device_id, role (przyczyna | skutek)
incident_event        incident_id, ts, kind, text                         (przebieg incydentu)
alert                 id, fingerprint, device_id, rule, severity, status, message, first_seen, last_seen,
                      incident_id, suppressed (bool)
silence               id, device_id | rule, starts_at, ends_at, reason, created_by
event                 id, ts, device_id, kind, severity, text, data_json   (oś czasu, retencja 90 dni)

-- integracje ----------------------------------------------------------------
integration           id, type (proxmox | pbs | truenas | komodo | openwrt | tplink | agent | checks),
                      name, config_json (bez sekretów), secret_ct (zaszyfrowane), enabled, poll_seconds,
                      last_ok_at, last_error
external_ref          integration_id, external_id (np. qemu/107, stack/immich, mac/aa:bb…), device_id, proposed_json,
                      status (nowy | zatwierdzony | ignorowany). Zatwierdzenie obejmuje też czekających przodków
                      i potomków (VM + jej stacki = jedno kliknięcie); obiekty z różnych integracji łączą się po adresie
observed_state        integration_id, external_id, state (ok | warn | down | unknown), summary,
                      data_json, observed_at                                (ostatni znany stan)
update_available      device_id, source, component, current, available, security, detected_at, notes_url
backup_status         device_id, source (pbs | zfs | openwrt), last_ok_at, last_status, details_json
network_client        mac, ip, hostname, vendor, via (router | ap: device_id), band, signal_dbm,
                      first_seen, last_seen, device_id, status (nowy | przypisany | ignorowany)
check                 id, device_id, type (http | tcp | ping | tls | dns), target, from (lan | vps),
                      interval, state, fails, last_ms, last_error, cert_expires_at
config_backup         device_id, taken_at, sha256, path, size                (kopie konfiguracji OpenWrt)

-- akcje i bezpieczeństwo ----------------------------------------------------
action_allowed        device_id, action_key                                (jawna lista per urządzenie)
action_run            id, user_id, device_id, action_key, params_json, status, started_at, finished_at,
                      result_text, incident_id
user                  id, login, pw_hash (argon2id), role (admin | podglad), totp_secret_ct,
                      totp_last_step, created_at, disabled
session               id (losowy, w ciasteczku), user_id, created_at, last_seen_at, expires_at, ip,
                      user_agent, kind (zwykla | kiosk)
login_attempt         login, ip, ts, ok
audit_log             id, ts, user_id, ip, action, target, params_json, result
notification_channel  id, type (discord; później ntfy | email | telegram), config_ct, min_severity, enabled
profile               id, name, applies_to_kinds, alert_overrides_json, allowed_actions_json, update_policy,
                      agent_config_json                                    (ustawienia na poziomie typu)
device_override       device_id, key, value_json                           (świadome wyjątki od profilu)
```

### Analiza przyczyny (jak panel wie „dlaczego”)

1. Graf z trzech źródeł: **hosting** (`device.parent_id`, także z Proxmoxa), **zależności** (`dependency`) i **kable** (`cable` — padnięty switch odcina to, co w niego wpięte).
2. Gdy kilka urządzeń leży naraz, przyczyną jest to **najgłębsze**: leży, a wszystko, od czego zależy, działa.
   (W HLC wybierany był dowolny „korzeń”).
3. Pozostałe niedziałające urządzenia, które zależą od przyczyny, to **skutki** — bez osobnych powiadomień.
4. Objawy czekają ~60–90 s na wykrycie przyczyny, zanim pójdzie powiadomienie (koncepcja `group_wait` z HLC).
5. Panel zapisuje **dowody** w incydencie: co nad przyczyną działa, co zgłosiły integracje i w jakiej kolejności,
   co się zmieniło w ostatnich 24 h (przeniesione z `changes.py` HLC). Z tego powstaje sekcja „Dlaczego panel tak uważa”.
6. Procedury z `trigger` pasującym do przyczyny pokazują się w incydencie automatycznie.

---

## 4. Interfejs adapterów

Każda integracja to klasa w `denali/adapters/<typ>.py`, rejestrowana w jednym miejscu. Rdzeń zna tylko ten interfejs.

```python
class Capability(StrEnum):
    DISCOVER = "discover"        # lista obiektów (VM, stacki, datasety, klienci sieci…)
    STATUS = "status"            # stan obiektów
    UPDATES = "updates"          # dostępne aktualizacje
    ACTIONS = "actions"          # akcje (restart, snapshot, kopia konfiguracji…)
    BACKUPS = "backups"          # kiedy ostatni udany backup
    NETWORK_CLIENTS = "clients"  # DHCP / ARP / Wi-Fi

class Adapter(Protocol):
    type: ClassVar[str]                      # "proxmox", "komodo", …
    capabilities: ClassVar[frozenset[Capability]]
    config_schema: ClassVar[type[BaseModel]] # pola w formularzu „+ integracja”, walidowane

    def __init__(self, config: BaseModel, secrets: Secrets, http: AsyncClient): ...

    async def test(self) -> TestResult                              # „Sprawdź połączenie”
    async def discover(self) -> list[DiscoveredObject]              # external_id, kind, name, addresses, parent_external_id
    async def status(self) -> list[ObservedState]                   # external_id, state, summary, data
    async def updates(self) -> list[UpdateInfo]
    async def backups(self) -> list[BackupInfo]
    async def clients(self) -> list[NetworkClient]
    def actions(self) -> list[ActionSpec]                           # key, label, target_kinds, dangerous, needs_reauth
    async def run_action(self, key: str, external_id: str, params: dict) -> ActionResult
```

Metody spoza `capabilities` nie są wołane. Rdzeń robi resztę: harmonogram odpytywania, zapis `observed_state`, przypięcie
do urządzeń, alerty, analiza przyczyny, audyt akcji. **Nowy typ urządzenia = nowa klasa, bez zmian w rdzeniu.**

### Adaptery w planie

| Adapter | Możliwości | Uprawnienia (osobne konto/token dla panelu) | Etap |
|---|---|---|---|
| Proxmox | discover, status, updates, backups (vzdump), actions | token **PVEAuditor** do odczytu + osobny token z rolą `PanelActions` (VM.PowerMgmt, VM.Snapshot) | 3 / 5 |
| PBS | backups | token z rolą **DatastoreAudit** | 3 |
| TrueNAS | discover (pule, datasety, udziały), status, alerty | klucz API konta z rolą tylko do odczytu | 3 |
| Komodo | discover (serwery, stacki), status, updates, actions | użytkownik Komodo z prawem odczytu + osobny klucz do Restart/Deploy na wybranych stackach | 3 / 5 |
| OpenWrt | clients, status (WAN, porty, Wi-Fi), updates (apk/opkg), actions: **tylko kopia konfiguracji** | użytkownik rpcd z ACL tylko na potrzebne obiekty ubus | 3 / 5 |
| TP-Link (fabryczny) | clients, status | konto admina routera (jedyne, jakie ma) — tylko odczyt po stronie panelu | 3 |
| Agent | status (NFS), updates (apt, reboot-required), actions: `apt_upgrade` z lokalnej białej listy | token per maszyna, tylko HTTPS | 4 / 5 |
| Sprawdzenia | status (http, tcp, ping, tls, dns) z LAN i z VPS | — | 3 |

Później, bez zmian w rdzeniu: Omada / UniFi (AP), UPS przez NUT, Home Assistant.

---

## 5. Logowanie, uprawnienia, dostęp

### Rekomendacja: własne logowanie jako podstawa, OIDC opcjonalnie

**Dlaczego nie samo OIDC (Pocket ID / Authelia / Authentik):** dostawca logowania stoi na tej samej infrastrukturze, którą
panel pilnuje. Gdy padnie Proxmox albo sieć, możesz nie zalogować się do panelu właśnie wtedy, kiedy jest najbardziej
potrzebny. Dlatego podstawą jest **własne logowanie**.

OIDC jest w [POMYSLY.md](POMYSLY.md) — chcesz najpierw ustandaryzować logowanie we wszystkich aplikacjach. Logowanie
w Denali piszemy tak, żeby OIDC dało się potem dołożyć jako drugi sposób (kod z HLC jest gotowy), a własne konto
zostało jako awaryjne.

Własne logowanie:
- hasła **argon2id**;
- **TOTP obowiązkowe dla admina**: kod QR przy włączaniu, limit prób, kod nie zadziała drugi raz (zapamiętany ostatni krok czasu), kody zapasowe;
- **sesje po stronie serwera** (tabela `session`), ciasteczko `HttpOnly; Secure; SameSite=Lax` (Lax, żeby link z powiadomienia na Discordzie nie wymagał ponownego logowania; zapisy chroni token CSRF), sesja wygasa po 12 h bezczynności i najpóźniej po 14 dniach;
- **wyloguj wszędzie**, a zmiana hasła lub 2FA unieważnia pozostałe sesje; lista aktywnych sesji w ustawieniach;
- **limit prób** po loginie i po IP, z prawdziwym IP klienta zza reverse proxy (ufamy tylko adresowi NPM); jednakowy czas odpowiedzi dla istniejącego i nieistniejącego loginu;
- ponowne podanie hasła i kodu (ważne 5 min) przed: akcją oznaczoną jako niebezpieczna, odsłonięciem hasła ze skarbca, zmianą uprawnień i integracji;
- CSRF, CSP z nonce, nagłówki bezpieczeństwa (przeniesione z HLC).

### Role

| Rola | Co widzi | Co może |
|---|---|---|
| **admin** | wszystko | dokumentacja, integracje, akcje z listy dozwolonych, skarbiec |
| **podgląd** | pulpit, infrastruktura, incydenty, procedury, runbooki | czytać; bez haseł, bez akcji, bez ustawień |

### Tablet na ścianie (tryb kiosku)

- Konto roli **podgląd** parowane z tabletem jednorazowym kodem (wpisujesz go raz na tablecie).
- Sesja typu `kiosk`: długa (90 dni), ale **tylko z adresu IP tabletu** i z możliwością unieważnienia jednym kliknięciem.
- Bez haseł, bez akcji, bez ustawień — nawet gdy ktoś weźmie tablet ze ściany.
- Widok pulpitu dostosowany do dotyku (ikony ≥ 72 px, dok na dole), odświeżanie na żywo przez SSE, ciemny ekran nocą.
- Home Assistant działa na tablecie obok (osobna aplikacja). Widżet HA na pulpicie — w [POMYSLY.md](POMYSLY.md).

### Dostęp sieciowy

- Panel słucha tylko w LAN. Z zewnątrz **wyłącznie przez Tailscale** (klient Tailscale na VM panelu albo subnet router
  w domu; dostęp do panelu ograniczony ACL-em Tailscale do Twoich urządzeń).
- **Nie wystawiamy przez Pangolina.** Obecny zasób `hlc.serwispowers.com` w Pangolinie trzeba usunąć (audyt, B0).
- HTTPS także w LAN: istniejący **Nginx Proxy Manager** z certyfikatem Let's Encrypt dla `denali.serwispowers.com` (wyzwanie DNS w Cloudflare po stronie NPM; decyzja z 30.10.2026 — bez własnego Caddy w Denali)
  (token Cloudflare w NPM tylko do edycji DNS tej strefy). Rekord wskazuje na adres w LAN albo istnieje tylko w lokalnym DNS — z internetu panel jest nieosiągalny.

### Sekrety

- `.env` (poza repo): `ENCRYPTION_KEY` (sesje są po stronie serwera, więc osobny klucz podpisu nie jest potrzebny). Kopia `.env` w menedżerze haseł i zaszyfrowana (age) poza serwerem.
- W bazie zaszyfrowane: tokeny integracji, sekrety TOTP, konfiguracja kanałów, skarbiec.
- Klucz do tokenów „akcyjnych” (Proxmox `PanelActions`, Komodo) przechowywany osobno od tokenów do odczytu — wyłączenie akcji = usunięcie jednego wpisu.

---

## 6. Akcje (Etap 5, ale model od początku)

- Jawna lista akcji w kodzie adapterów (`ActionSpec`). Akcja działa tylko, gdy jest w `action_allowed` dla danego urządzenia.
- Każda akcja: okno potwierdzenia z nazwą urządzenia i skutkiem → dla niebezpiecznych ponowne hasło + kod → wykonanie przez
  API → wynik w `action_run`, `audit_log` i w przebiegu incydentu.
- Na start: restart/start/stop stacka (Komodo), start/stop/restart/snapshot VM (Proxmox), kopia konfiguracji OpenWrt,
  aktualizacja przez Komodo, `apt upgrade` przez agenta (snapshot VM przed).
- Panel nie ma akcji na samym sobie ani na routerze (poza kopią konfiguracji).

---

## 7. Odporność: panel nie może paść razem z tym, co pilnuje

| Ryzyko | Zabezpieczenie |
|---|---|
| Padnie pve | **Teraz (pve2 jeszcze nie ma):** codzienny backup VM panelu w PBS + opisane odtworzenie (z PBS na naprawionym albo innym hoście) + eksport statyczny procedur na VPS i PDF w telefonie. **Gdy pojawi się pve2:** replikacja VM co 5 min + Proxmox HA (klaster 2 węzły + QDevice), według `ZAPASOWY-SERWER.md` z HLC |
| Zepsuta aktualizacja panelu | Snapshot VM przed aktualizacją; poprzedni obraz Dockera zostaje (`docker compose` z przypiętą wersją) |
| Utrata bazy | Co noc: `sqlite3 .backup` + pliki → archiwum; VM w PBS; archiwum dodatkowo na VPS (restic, zaszyfrowane) |
| Panel leży, a trzeba działać | **Eksport statyczny** procedur i runbooków (HTML + PDF) po każdej zmianie i co godzinę: na VPS (za logowaniem Pangolina — bez haseł ze skarbca), później także na pve2 |
| Nikt nie wie, że panel leży | **Strażnik na VPS**: panel co minutę wysyła sygnał życia do monitora na VPS (Uptime Kuma „push” albo healthchecks). Brak sygnału 3 min → powiadomienie z VPS-a |
| Powiadomienia nie dochodzą, bo padł dom | Panel wysyła na **Discord (webhook)**; gdy padnie dom lub panel, na ten sam Discord pisze **strażnik z VPS-a** |

---

## 8. Wdrożenie

- VM `denali` na pve: Debian 13, 2 vCPU, 2 GB RAM, 20 GB dysku, `qemu-guest-agent`, start z hostem (wcześnie w kolejności).
- `/srv/denali/`: `compose.yml`, `.env`, `data/` (baza, pliki, kopie konfiguracji), `export/` (statyczne procedury).
- Kontenery: `app` (FastAPI), `worker` (integracje i alerty), HTTPS przez NPM. Aktualizacja: `git pull && docker compose up -d --build`
  albo nowy tag obrazu — **bez samoaktualizacji z panelu** (uprawnienia roota z panelu były ryzykiem w HLC).
- Backup VM w PBS, kopia `data/` na VPS. Replikacja na pve2 + HA — gdy pve2 powstanie.
- README: instalacja, backup, restore (z PBS i z kopii `data/`), uruchomienie na innym hoście, odzyskanie dostępu (reset hasła z konsoli).

---

## 9. Migracja z HLC

Jedno polecenie, które czyta **kopię** `hlc.db` (tylko do odczytu) i zapisuje do nowej bazy:

```
denali migrate-hlc --db hlc.db --uploads uploads/ --old-key <HLC_FERNET_KEY> [--dry-run]
```

| Z HLC | Do | Uwagi |
|---|---|---|
| `devices` (pola dokumentacji) | `device`, `device_address` | typy urządzeń mapowane 1:1; `crit` 1–3 → P3–P1; pola Proxmoxa odtworzy integracja |
| `deps` | `dependency` | typ i twardość bez zmian |
| `links` | `cable` | z gniazdami |
| `devices.runbook` | `runbook` + `runbook_device` | jeden runbook na urządzenie; tytuł z pierwszego nagłówka |
| `issues` | `incident` (zamknięte, `source=hlc`) | opis → `cause_md`, „jak naprawiono” → `fix_md` |
| `sites.panic` (`[{t, steps[]}]`) | `procedure` + `procedure_step` | `sites.call` → `contact` |
| `secrets`, `sites.wifi` | `vault_secret` | odszyfrowane starym kluczem, zaszyfrowane nowym; nigdy nie lądują w logu migracji |
| `files` + `uploads/` | `file` | kopiowane pliki |
| `net_hosts` | `device_address` (MAC), `network_client` | pomoże przypiąć klientów z routera |
| `dns_names` | `device_address` (hostname) | |
| `checks` | `check` | tylko definicje |
| `channels` | `notification_channel` | opcjonalnie |
| `apps` | `device.app_url`, `pulpit_group` | ikony aplikacji na pulpicie B |
| użytkownicy, metryki, AI, panel Start, shell | — | nie przenosimy |

`--dry-run` pokazuje liczby i listę rzeczy do ręcznego przejrzenia (np. urządzenia bez typu, runbooki ze śladami haseł).
Migrację można powtórzyć — drugi przebieg niczego nie dubluje.

---

## 10. Plan etapów (doprecyzowany)

| Etap | Zakres | Kryterium odbioru |
|---|---|---|
| **2. Rdzeń** | szkielet aplikacji, logowanie + 2FA + sesje, role, kiosk, dokumentacja urządzeń, zależności, kable, runbooki, procedury, incydenty (ręczne), skarbiec, migracja z HLC, eksport statyczny, pulpit B (bez danych na żywo), README | przy awarii dowolnego urządzenia w 1 minutę widzę jego rolę, zależności, runbook i historię problemów |
| **3. Status** | adaptery Proxmox, PBS, TrueNAS, Komodo, OpenWrt, TP-Link, sprawdzenia (LAN + VPS), analiza przyczyny, incydenty automatyczne, powiadomienia (Discord), strażnik na VPS | wyłączenie testowej VM daje jeden incydent z przyczyną, skutkami i powiadomieniem |
| **4. Aktualizacje** | agent (apt, reboot-required, NFS), zbiorczy widok aktualizacji | jedna lista ze wszystkich źródeł zgadza się z tym, co pokazują Proxmox/Komodo/OpenWrt |
| **5. Akcje** | lista dozwolonych, potwierdzenia, audyt, akcje Proxmox/Komodo/OpenWrt/agent | każda akcja ma wpis w audycie; akcja spoza listy jest niemożliwa nawet przez API |

---

## 11. Odpowiedzi (30.10.2026) i co jeszcze otwarte

| Pytanie | Odpowiedź / stan |
|---|---|
| Nazwa | **Denali** |
| OIDC | Masz dostawcę, ale OIDC → [POMYSLY.md](POMYSLY.md); najpierw standard logowania dla wszystkich aplikacji |
| VPN | **Tailscale** |
| AX23 — wersja sprzętowa | Do sprawdzenia później. Nie blokuje Etapu 2; adapter OpenWrt i TP-Link powstają w Etapie 3 |
| pve2 | Nie istnieje. Odporność na start: PBS + eksport na VPS; replikacja i HA, gdy pve2 powstanie |
| Powiadomienia | **Discord (webhook)** |
| Tablet | Układ pulpitu elastyczny (od 800 px szerokości); dopasowanie, gdy będzie wiadomo, jaki to model |
| Akceptacja | Projekt zaakceptowany 30.10.2026 **z zastrzeżeniem**: minimum ustawień na pojedynczym urządzeniu (zasada nadrzędna w rozdz. 0) |
