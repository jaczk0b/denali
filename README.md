# Denali

Panel do homelaba: widzę wszystkie urządzenia, aplikacje, VM-ki i ich aktualizacje, mogę nimi zarządzać,
a gdy coś padnie albo się zmieni, w minutę wiem **co, dlaczego i gdzie szukać**.

Dokumenty: [PROJEKT.md](PROJEKT.md) (projekt i decyzje), [AUDYT.md](AUDYT.md) (audyt poprzedniego panelu HLC),
[POMYSLY.md](POMYSLY.md) (odłożone pomysły).

**Stan: Etap 4 (aktualizacje) — kod gotowy, czeka na sprawdzenie na serwerach.** Jest: logowanie z 2FA, urządzenia,
zależności i kable, profile, kolejka „do zatwierdzenia”, analiza przyczyny, incydenty, procedury, runbooki, pliki,
skarbiec, pulpit, tablet, eksport awaryjny, kopie zapasowe; integracje Proxmox, PBS, TrueNAS, Komodo i router TP-Link,
sprawdzenia z LAN i z internetu, stan na żywo z automatycznymi incydentami, powiadomienia na Discord, strażnik na VPS,
odświeżanie na żywo (SSE); jedna lista aktualizacji (Proxmox, PBS, Komodo, agent) i agent na maszynach (apt,
wymagany restart, NFS). Nie ma jeszcze: OpenWrt (odłożone); akcji — Etap 5.

Spis treści:
[Instalacja](#instalacja) ·
[Pierwsze kroki](#pierwsze-kroki) ·
[Agent i aktualizacje](#agent-i-aktualizacje) ·
[Migracja z HLC](#migracja-z-hlc) ·
[Kopia zapasowa](#kopia-zapasowa) ·
[Odtworzenie](#odtworzenie) ·
[Zapasowy serwer](#uruchomienie-na-innym-serwerze) ·
[Odzyskanie dostępu](#odzyskanie-dostępu) ·
[Aktualizacja](#aktualizacja-panelu) ·
[Polecenia](#polecenia) ·
[Programista](#dla-programisty)

---

## Instalacja

### Co jest potrzebne

- VM na pve: Debian 13, 2 vCPU, 2 GB RAM, 20 GB dysku, `qemu-guest-agent`, start z hostem. Stały adres IP w LAN.
- Docker z wtyczką compose (`apt install docker.io docker-compose-plugin` albo według docs.docker.com).
- Istniejący **Nginx Proxy Manager** w LAN — robi HTTPS przed panelem.
- Tailscale z dostępem do LAN (subnet router) — tak wchodzisz do panelu spoza domu.

Panel jest dostępny **tylko z LAN i przez Tailscale**. Nie dodawaj go do Pangolina (Newt) ani do tunelu Cloudflare
i nie przekierowuj portów na routerze — każde z tych rozwiązań wystawia panel do internetu.

```
przeglądarka / tablet ──HTTPS──▶ Nginx Proxy Manager ──HTTP w LAN──▶ VM denali :8000 (app + worker)
   (LAN albo Tailscale)           certyfikat, lista dostępu
```

### Instalacja przez Komodo (z repozytorium na GitHubie)

Zamiast kroków 1–3 poniżej:

1. Wybierz stały katalog danych na serwerze, poza katalogiem repozytorium (ten Komodo może wyczyścić przy
   aktualizacji), np. `/opt/denali/data`, i raz nadaj go użytkownikowi panelu:
   `sudo mkdir -p /opt/denali/data && sudo chown 1000:1000 /opt/denali/data`.
2. Klucz szyfrowania wygeneruj na dowolnym komputerze z Pythonem i biblioteką `cryptography`:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   i **zapisz go w menedżerze haseł**.
3. W Komodo: *Stacks → New Stack*, źródło: to repozytorium, plik `compose.yaml`. Włącz budowanie obrazu przy
   wdrożeniu (obraz `denali:local` powstaje z `Dockerfile`, nie jest pobierany z rejestru) — inaczej aktualizacja
   repozytorium nie zmieni działającej wersji. W *Environment* wpisz (Komodo zapisze to jako `.env` obok compose):
   ```
   DENALI_ENCRYPTION_KEY=…klucz z punktu 2…
   DENALI_TRUSTED_PROXIES=["IP-twojego-NPM"]
   DENALI_DATA_PATH=/opt/denali/data
   DENALI_PORT=8000
   DENALI_BASE_URL=https://denali.serwispowers.com
   ```
4. *Deploy*. Dalej jak w krokach 4 i 5 — polecenia `python -m denali …` uruchamiasz w terminalu kontenera `app`
   w Komodo albo na serwerze: `docker exec -it <nazwa-stacka>-app-1 python -m denali create-user jakub`.

Compose korzysta z `env_file` z `required: false`, co wymaga Docker Compose 2.24 lub nowszego.

### 1. Pliki na serwer

Skopiuj katalog repozytorium do `/opt/denali` (np. `scp -r new/ root@VM:/opt/denali`). Katalog danych
(`DENALI_DATA_PATH`, domyślnie `/opt/denali/data`) musi należeć do użytkownika panelu (UID 1000):
`sudo mkdir -p /opt/denali/data && sudo chown 1000:1000 /opt/denali/data`.

```sh
cd /opt/denali
cp .env.example .env && chmod 600 .env
```

### 2. Klucz szyfrowania i ustawienia

```sh
docker compose build
docker compose run --rm --no-deps -T app python -m denali genkeys >> .env
nano .env        # DENALI_TRUSTED_PROXIES = adres IP Nginx Proxy Managera
```

**Od razu zapisz całą zawartość `.env` w menedżerze haseł.** Bez `DENALI_ENCRYPTION_KEY` hasła ze skarbca i 2FA
z kopii zapasowej są nie do odczytania (patrz [Odzyskanie dostępu](#odzyskanie-dostępu)).

`DENALI_TRUSTED_PROXIES` to jedyny adres, któremu panel wierzy, gdy podaje on „prawdziwe IP klienta”. Od tego zależą
limit prób logowania i przypięcie tabletu do adresu. Gdy NPM działa na innej maszynie — wpisz jej IP w LAN.
Gdy NPM działa w Dockerze na **tej samej** VM — wpisz adres, z którego przychodzi do Denali (sprawdzisz w kroku 5).

### 3. Start

```sh
docker compose up -d
docker compose ps                       # app: healthy, worker: running
curl -s http://127.0.0.1:8000/health
# {"status":"ok","version":"…","db":"ok","worker":"ok"}  — worker zgłasza się po ok. 30 s
```

Jeśli coś nie wstaje: `docker compose logs app worker`.

### 4. Nginx Proxy Manager i DNS

W NPM: *Hosts → Proxy Hosts → Add Proxy Host*:

| Pole | Wartość |
|---|---|
| Domain Names | `denali.serwispowers.com` |
| Scheme / Forward Hostname / Port | `http` / IP tej VM / `8000` |
| Block Common Exploits | włączone |
| Websockets Support | może zostać włączone (odświeżanie na żywo to SSE — zwykłe HTTP; panel sam wyłącza buforowanie nagłówkiem `X-Accel-Buffering: no`) |
| Access List | lista „tylko LAN + Tailscale”: *allow* `192.168.0.0/16` (Twoja podsieć) i `100.64.0.0/10`, *deny* `all` |
| SSL | *Request a new SSL Certificate* → *Use a DNS Challenge* → Cloudflare (token „Edit zone DNS” tylko dla `serwispowers.com`), *Force SSL* i *HSTS* włączone |

DNS: w Cloudflare rekord `A denali → <IP NPM w LAN>` z **wyłączonym proxy** (szara chmurka) albo wpis tylko
w lokalnym DNS. Adres prywatny w publicznym DNS niczego nie wystawia — spoza LAN i Tailscale nie da się z nim połączyć.
Certyfikat przez wyzwanie DNS nie wymaga otwierania portów.

Sprawdź: `https://denali.serwispowers.com/health` z przeglądarki.

### 5. Konto administratora

```sh
docker compose exec app python -m denali create-user jakub
```

Zaloguj się na `https://denali.serwispowers.com`. Panel od razu każe włączyć 2FA: zeskanuj kod QR w aplikacji
(Aegis, 2FAS, Google Authenticator…) i **zapisz 10 kodów zapasowych** w menedżerze haseł — każdy działa raz.

**Sprawdź adres klienta:** *Audyt* → wpis „login” → kolumna adresu. Musi tam być adres Twojego komputera.
Jeśli jest adres NPM, popraw `DENALI_TRUSTED_PROXIES` w `.env` i zrób `docker compose up -d`.

Konto tylko do podglądu (np. dla domownika): `create-user LOGIN --role podglad`.

---

## Pierwsze kroki

1. **Procedura „Jak odtworzyć Denali”** — *Procedury → Nowa*, zakres „gdy nie działa sam panel”. Wpisz, gdzie są
   kopie, gdzie jest `.env` i odsyłacz do tej instrukcji. Pojawi się na górze kopii awaryjnej na VPS-ie.
2. **Urządzenia** — *Infrastruktura → Dodaj urządzenie*: wystarczą nazwa, typ i „na czym działa”. Od Etapu 3
   urządzenia będą przychodzić same z integracji i wystarczy je zatwierdzić.
3. **Kopia awaryjna na VPS** — według [deploy/vps/README.md](deploy/vps/README.md).
4. **Tablet** — *Tablet → Utwórz kod*, na tablecie otwórz `https://denali.serwispowers.com/tablet` i wpisz kod.
5. **Strażnik z VPS-a** i powiadomienia na Discord — *Powiadomienia*.
6. **Agent** na VM-kach z Dockerem i usługami — niżej.

---

## Agent i aktualizacje

*Aktualizacje* to jedna lista ze wszystkich źródeł. Panel tylko pokazuje — aktualizujesz tam, gdzie zawsze
(akcje dopiero w Etapie 5).

| Źródło | Co podaje | Co trzeba zrobić |
|---|---|---|
| Proxmox VE | pakiety węzła (jak *Węzeł → Updates*) | nic — token PVEAuditor już to widzi |
| PBS | pakiety serwera (jak *Administration → Updates*) | nic — rola Audit |
| Komodo | usługi stacków z nowszym obrazem | w stacku włączone *Poll for updates* |
| Agent | pakiety apt (z poprawkami bezpieczeństwa), wymagany restart, montaże NFS | instalacja na maszynie (niżej) |

TrueNAS i router TP-Link na razie nie podają aktualizacji (TrueNAS — później; TP-Link nie ma tego w API).

**Agent** instaluj tylko tam, gdzie API nie wystarcza — zwykle VM-ki z Dockerem i usługami. Na Proxmoxie, PBS i TrueNAS
nie. *Integracje → Dodaj: Agent na maszynie → Wygeneruj polecenie* i wklej je na maszynie (Debian/Ubuntu z systemd,
Python 3.9+). Polecenie pobiera plik, sprawdza jego SHA-256 i dopiero wtedy go uruchamia; kod jest jednorazowy i ważny
godzinę. Maszyna sama przypnie się do swojej VM z Proxmoxa (po IP albo MAC).

Wymaga działającego HTTPS (Nginx Proxy Manager, krok 4 instalacji): agent łączy się z `DENALI_BASE_URL` i sprawdza
certyfikat — token nie idzie zwykłym HTTP.

- `denali-agent.service` — raport co minutę, jako zwykły użytkownik `denali-agent`, system tylko do odczytu.
- `denali-agent-ukryte.timer` — raz dziennie, jako root, **bez sieci**: czy pod montażami NFS nie leżą dane zapisane,
  gdy udział nie był podłączony (to jedyne, co wymaga roota).
- Agent nie słucha na żadnym porcie i niczego nie wykonuje na polecenie panelu.
- Stan: `systemctl status denali-agent`, `journalctl -u denali-agent -n 30`; raport bez wysyłania:
  `python3 /opt/denali-agent/denali-agent.py print`.
- Usunięcie: `sudo python3 /opt/denali-agent/denali-agent.py uninstall`, a w Denali usuń agenta (unieważnia token).
- Brak raportu dłużej niż 5 minut: stan maszyny „nieznany” i błąd przy agencie na liście integracji.

Montaż NFS z `/etc/fstab` staje się zależnością „dane” od serwera (np. TrueNAS) — gdy TrueNAS padnie, incydent wskaże
TrueNAS, a nie VM. Profil *Dostępne aktualizacje: ignoruj* chowa urządzenie z licznika (np. maszyna testowa).

---

## Migracja z HLC

Przenosi z HLC: urządzenia (z notatkami, dostępem, portami), adresy, zależności, kable, runbooki, log problemów,
pliki, hasła (do skarbca), procedury „na wypadek”, kontakt alarmowy, hasła Wi-Fi i ikony aplikacji ze strony Start.
Nie przenosi kont (załóż w Denali), sprawdzeń i kanałów powiadomień (wrócą z integracjami w Etapie 3).
HLC jest tylko czytane — nic w nim się nie zmienia. Drugie uruchomienie niczego nie dubluje.

Wystarczą dwa terminale w przeglądarce, bez kopiowania plików na komputer:

**1. Konsola HLC** (Proxmox → kontener HLC → *Console*, jako root):

```sh
cd /opt/hlc && set -a && . ./.env && set +a
HLC_INSTANCE=/var/lib/hlc .venv/bin/python -m hlc backup     # wypisze ścieżkę świeżej kopii ZIP
grep HLC_FERNET_KEY /opt/hlc/.env                            # klucz — skopiuj wartość po „=”
hostname -I                                                  # adres IP kontenera HLC
cd /var/lib/hlc/backups && python3 -m http.server 8099       # udostępnia kopię w LAN; zostaw włączone
```

**2. Terminal kontenera `app` Denali** (Komodo → stack `denali` → `app` → *Terminal*):

```sh
python -m denali migrate-hlc http://IP-HLC:8099/hlc-RRRR-MM-DD.zip --site Dom              # próba na sucho
python -m denali migrate-hlc http://IP-HLC:8099/hlc-RRRR-MM-DD.zip --site Dom --wykonaj    # zapis
```

Polecenie zapyta o `HLC_FERNET_KEY` (wklej i Enter). Bez klucza przeniesie wszystko poza hasłami.
Próba na sucho pokazuje dokładnie, co i ile zostanie przeniesione, oraz listę rzeczy do przejrzenia ręcznie
(np. notatki, które wyglądają, jakby zawierały hasło, albo nieprawidłowe adresy).

**3. Wróć do konsoli HLC i zatrzymaj udostępnianie: Ctrl+C.** Kopia zawiera zaszyfrowane hasła (bez klucza) i opis
sieci — nie zostawiaj jej dostępnej dłużej niż trzeba.

---

## Kopia zapasowa

| Co | Gdzie | Kiedy |
|---|---|---|
| Baza + pliki urządzeń (archiwum `denali-RRRRMMDD-GGMMSS.tar.gz`) | `data/backups/`, 14 ostatnich | worker, raz na dobę |
| Cała VM | PBS | zadanie backupu w Proxmoxie, codziennie |
| Eksport awaryjny (HTML + PDF, bez haseł) | `data/export/` → VPS | po każdej zmianie, co godzinę na pewno |
| `.env` (klucz szyfrowania) | menedżer haseł | raz, przy instalacji |

Archiwum w `data/backups` leży na tej samej VM — chroni przed błędem (np. skasowanym urządzeniem), nie przed awarią
dysku. Przed awarią chroni PBS. Kopię poza domem możesz dołożyć tym samym kluczem co eksport albo resticiem:

```sh
restic -r sftp:backup@vps:/srv/restic/denali backup /opt/denali/data/backups   # repozytorium szyfrowane hasłem restic
```

Kopia na żądanie (np. przed większą zmianą): `docker compose exec app python -m denali backup`.

W archiwum **nie ma** klucza szyfrowania (jest w menedżerze haseł) ani eksportu (worker odtworzy go sam).

---

## Odtworzenie

### Cała VM z PBS (najprościej)

W Proxmoxie: *VM denali → Backup → wybierz kopię → Restore*. Po starcie wszystko wraca samo, łącznie z `.env`.

### Z archiwum Denali (np. przywrócenie skasowanych danych)

```sh
cd /opt/denali
docker compose stop app worker
docker compose run --rm --no-deps app python -m denali restore /data/backups/denali-20261030-030000.tar.gz --force
docker compose start app worker
```

`--force` nie kasuje obecnej bazy — odkłada ją obok jako `data/denali.db.przed-odtworzeniem-…`. Archiwum ze starszej
wersji Denali zostanie automatycznie podniesione do bieżącego schematu.

---

## Uruchomienie na innym serwerze

Gdy pve padł na dłużej, a kopia VM z PBS nie wchodzi w grę. Wystarczy dowolna maszyna z Dockerem (inny host,
laptop, VPS w Tailscale).

Potrzebujesz: tego repozytorium, ostatniego archiwum `denali-*.tar.gz` (z PBS, z kopii poza domem albo z restica)
i `.env` z menedżera haseł.

```sh
mkdir -p /opt/denali && cd /opt/denali             # skopiuj tu repozytorium
mkdir -p data && chown 1000:1000 data
nano .env && chmod 600 .env                        # wklej .env z menedżera haseł
cp /ścieżka/denali-RRRRMMDD-GGMMSS.tar.gz data/
docker compose build
docker compose run --rm --no-deps app python -m denali restore /data/denali-RRRRMMDD-GGMMSS.tar.gz
docker compose up -d
```

Na koniec w NPM zmień w proxy hoście `denali` adres docelowy na IP nowej maszyny (a jeśli padł też NPM — zmień rekord `denali` w Cloudflare/lokalnym DNS na nową maszynę i otwórz panel po `http://IP:8000` z `DENALI_DEV=1` tylko na czas awarii). Tablet trzeba połączyć
na nowo (jego sesja jest przypięta do adresu), konta, 2FA i skarbiec działają jak wcześniej.

Do czasu, aż panel wstanie: procedury i kontakty są w kopii awaryjnej na VPS-ie i w PDF-ie w telefonie.

---

## Odzyskanie dostępu

| Sytuacja | Co zrobić |
|---|---|
| Zapomniane hasło | `docker compose exec app python -m denali reset-password jakub` — nowe hasło, wszystkie sesje wylogowane |
| Zgubiony telefon z 2FA | Zaloguj się kodem zapasowym. Nie masz kodów: `reset-password jakub --disable-2fa`, przy logowaniu włącz 2FA od nowa |
| „Za dużo nieudanych prób” | Odczekaj 15 minut (blokada dotyczy konta i adresu, nie całego panelu) |
| Tablet wylogowany | *Tablet → Utwórz kod* i połącz od nowa |
| Panel nie działa, a trzeba działać | Kopia awaryjna na VPS-ie (za Pangolinem) albo PDF w telefonie |
| Zgubiony `DENALI_ENCRYPTION_KEY` | Urządzenia, procedury, incydenty i pliki są bezpieczne. **Hasła ze skarbca i sekrety 2FA są stracone.** Wygeneruj nowy klucz (`genkeys`), wpisz go do `.env`, uruchom ponownie, każdemu kontu zresetuj 2FA (`reset-password LOGIN --disable-2fa`) i wpisz hasła do skarbca na nowo |

Wszystkie polecenia z konsoli trafiają do audytu jako „konsola serwera”. Dostęp do konsoli VM = pełny dostęp do panelu,
więc konsolę chroni się tak jak samego pve.

---

## Aktualizacja panelu

```sh
cd /opt/denali
docker compose exec app python -m denali backup     # kopia przed aktualizacją
# zrób też snapshot VM w Proxmoxie
# podmień pliki repozytorium na nową wersję
docker compose up -d --build                        # migracje bazy uruchamiają się same przy starcie
```

Migracje bazy działają tylko w przód. Powrót do starszej wersji = przywrócenie snapshotu VM albo archiwum sprzed
aktualizacji.

---

## Polecenia

W Dockerze poprzedź je `docker compose exec app` (gdy panel działa) albo `docker compose run --rm --no-deps app`
(gdy jest zatrzymany).

| Polecenie | Co robi |
|---|---|
| `python -m denali create-user LOGIN [--role podglad]` | nowe konto (domyślnie administrator) |
| `python -m denali users` | lista kont z informacją o 2FA |
| `python -m denali reset-password LOGIN [--disable-2fa]` | nowe hasło, wylogowanie wszystkich sesji |
| `python -m denali backup [--out DIR]` | kopia bazy i plików |
| `python -m denali restore ARCHIWUM [--force]` | odtworzenie z kopii |
| `python -m denali export [--out DIR] [--pdf]` | eksport awaryjny na żądanie |
| `python -m denali genkeys` | nowy klucz szyfrowania |
| `python -m denali migrate` | aktualizacja schematu bazy (robi się sama przy starcie) |

Logi: `docker compose logs -f app worker`. Stan: `https://denali.serwispowers.com/health`.
Kto co zmienił: *Audyt* w panelu.

---

## Bezpieczeństwo w skrócie

- Dostęp tylko z LAN i Tailscale; HTTPS w Nginx Proxy Manager (Let's Encrypt), bez otwartych portów w internecie.
- Hasła argon2id, obowiązkowe 2FA dla administratora, limit prób logowania, sesje po stronie serwera
  (wylogowanie wszędzie, wygasanie po 12 h bezczynności).
- Role: administrator i podgląd. Tablet: tylko podgląd, tylko ze swojego adresu.
- Skarbiec zaszyfrowany; pokazanie hasła wymaga ponownego potwierdzenia hasłem i 2FA i trafia do audytu.
- Każda zmiana ma wpis w audycie. Panel nie ma i nie będzie miał dostępu do powłoki serwerów.
- Agent: token tylko w `/etc/denali-agent/config.json` (root i grupa agenta), w Denali tylko jego skrót; raport
  wysyła zwykły użytkownik bez prawa zapisu do systemu; agent nie przyjmuje poleceń.

---

## Dla programisty

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"            # Windows: .venv\Scripts\pip
cp .env.example .env
.venv/bin/python -m denali genkeys >> .env
echo DENALI_DEV=1 >> .env                    # lokalnie bez HTTPS: ciasteczka bez flagi Secure, /docs włączone
.venv/bin/python -m denali migrate
.venv/bin/python -m denali create-user admin
.venv/bin/python -m denali serve             # http://127.0.0.1:8000
.venv/bin/python -m denali worker            # w drugim terminalu
.venv/bin/python -m pytest
```

Układ kodu: `denali/` — logika (`inventory`, `rca`, `profiles`, `discovery`, `incidents`, `docs`, `vault`, `files`,
`export`, `backup`), `denali/web/` — trasy, `denali/templates/` — widoki, `denali/migrations/` — schemat bazy (Alembic).
PDF eksportu wymaga WeasyPrint (`pip install -e ".[pdf]"` + biblioteki pango; w obrazie Dockera jest).
