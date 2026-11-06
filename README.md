# Denali

Panel do homelaba: widzę wszystkie urządzenia, aplikacje, VM-ki i ich aktualizacje, mogę nimi zarządzać,
a gdy coś padnie albo się zmieni, w minutę wiem **co, dlaczego i gdzie szukać**.

Dokumenty: [PROJEKT.md](PROJEKT.md) (projekt i decyzje), [AUDYT.md](AUDYT.md) (audyt poprzedniego panelu HLC),
[POMYSLY.md](POMYSLY.md) (odłożone pomysły).

**Stan: Etap 5 (akcje) — kod gotowy, czeka na sprawdzenie na serwerach** (Etap 4 też jeszcze nie odebrany). Jest: logowanie z 2FA, urządzenia,
zależności i kable, profile, kolejka „do zatwierdzenia”, analiza przyczyny, incydenty, procedury, runbooki, pliki,
skarbiec, pulpit, tablet, eksport awaryjny, kopie zapasowe; integracje Proxmox, PBS, TrueNAS, Komodo, router OpenWrt, router TP-Link i OVHcloud (VPS),
sprawdzenia z LAN i z internetu, stan na żywo z automatycznymi incydentami, powiadomienia na Discord, strażnik na VPS,
odświeżanie na żywo (SSE); jedna lista aktualizacji (Proxmox, PBS, Komodo, OpenWrt, agent) i agent na maszynach (apt,
wymagany restart, NFS, dyski, pamięć, usługi failed, kontenery); alerty, oś czasu i terminy; dział Infrastruktura z zakładkami Mapa (rysowana sama), Proxmox, TrueNAS, Docker, Sieć, VPS,
Adresy IP (zajęte i wolne, konflikty) i Szafa (co na której wysokości); akcje z listy dozwolonych (Proxmox: start,
wyłączenie, restart, snapshot; Komodo: start, restart, stop, Deploy; agent: apt upgrade po snapshocie i restart
maszyny, gdy jest wymagany; OpenWrt: kopia konfiguracji) z potwierdzeniem i audytem — z karty urządzenia i z zakładek Proxmox i Docker; TrueNAS:
wszystkie powiadomienia (dzwonek), dyski z temperaturą i testami SMART. OpenWrt — kod gotowy, testy na nagranym API rpcd, nie sprawdzony na routerze.

Spis treści:
[Instalacja](#instalacja) ·
[Pierwsze kroki](#pierwsze-kroki) ·
[Agent i aktualizacje](#agent-i-aktualizacje) ·
[Akcje](#akcje) ·
[Terminale](#terminale) ·
[Monitorowanie](#monitorowanie) ·
[Alerty](#alerty) ·
[Tablet](#tablet-przy-szafie) ·
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
| Websockets Support | **włączone** — bez tego nie połączy się konsola na karcie Terminale (odświeżanie na żywo to SSE, zwykłe HTTP; panel sam wyłącza buforowanie nagłówkiem `X-Accel-Buffering: no`) |
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

### 6. Logowanie przez Pocket ID (opcjonalnie)

Własne konto (hasło + 2FA) zostaje zawsze — gdy Pocket ID nie działa, logujesz się nim jak dotąd.

1. W Pocket ID: *OIDC Clients → Add*: nazwa `Denali`, *Callback URL* `https://denali.serwispowers.com/login/oidc/callback`
   (dokładnie `DENALI_BASE_URL` + `/login/oidc/callback`). Zapisz *Client ID* i *Client secret*.
2. W *Environment* stacka:
   ```
   DENALI_OIDC_ISSUER=https://id.serwispowers.com      # adres Pocket ID, bez ukośnika na końcu
   DENALI_OIDC_CLIENT_ID=…
   DENALI_OIDC_CLIENT_SECRET=…
   ```
   i *Deploy*. Na stronie logowania pojawi się przycisk „Zaloguj przez Pocket ID”.
3. Zaloguj się hasłem → *Konto → Pocket ID → Połącz* → potwierdź hasłem → zaloguj się w Pocket ID.
   Od teraz przycisk wpuszcza Cię na to samo konto.

**Grupy (dla domowników):** w Pocket ID utwórz grupy, np. `denali-admins` i `denali-podglad`, a w *Environment*
`DENALI_OIDC_ADMIN_GROUP=denali-admins` i `DENALI_OIDC_VIEWER_GROUP=denali-podglad`. Wtedy osoba z grupy dostaje konto
w Denali przy pierwszym logowaniu (bez hasła — tylko przez Pocket ID), rola idzie za grupą przy każdym logowaniu,
a usunięcie z grup odbiera dostęp. Ostatni administrator nigdy nie traci roli. Bez ustawionych grup wchodzą tylko
konta połączone ręcznie. `DENALI_OIDC_USER_GROUP` (np. `denali-uzytkownicy`) robi to samo dla kont z rolą `uzytkownik`
(zob. *Udostępnianie*); usługi przydzielasz im po pierwszym logowaniu.

Zasady:
- konto łączy się wyłącznie po identyfikatorze z Pocket ID, nigdy po nazwie — ktoś z nazwą `jakub` w Pocket ID nie
  wejdzie na Twoje konto (dostanie `jakub-oidc`);
- po wejściu przez Pocket ID 2FA Denali nie jest pytane — drugim składnikiem jest klucz dostępu w Pocket ID;
- pokazanie hasła ze skarbca i akcja niebezpieczna: *Potwierdź w Pocket ID* (Pocket ID musi zapytać o klucz jeszcze
  raz — jeśli tylko przekieruje z powrotem, Denali odmówi; wtedy potwierdź hasłem Denali);
- *Rozłącz* (Konto) wylogowuje wszystkie sesje otwarte przez Pocket ID;
- Pocket ID musi mieć ważny certyfikat (https) — Denali łączy się z nim bezpośrednio z kontenera.

---

## Pierwsze kroki

1. **Procedura „Jak odtworzyć Denali”** — *Procedury → Nowa*, zakres „gdy nie działa sam panel”. Wpisz, gdzie są
   kopie, gdzie jest `.env` i odsyłacz do tej instrukcji. Pojawi się na górze kopii awaryjnej na VPS-ie.
2. **Urządzenia** — *Infrastruktura → Dodaj urządzenie*: wystarczą nazwa, typ i „na czym działa”. Od Etapu 3
   urządzenia będą przychodzić same z integracji i wystarczy je zatwierdzić.
3. **Kopia awaryjna na VPS** — według [deploy/vps/README.md](deploy/vps/README.md).
4. **Tablet** — *Tablet → Utwórz kod*, na tablecie otwórz `https://denali.serwispowers.com/tablet` i wpisz kod.
5. **Strażnik z VPS-a** i powiadomienia na Discord — menu konta → *Kanały powiadomień*.
6. **Agent** na VM-kach z Dockerem i usługami — niżej.

---

## Agent i aktualizacje

*Aktualizacje* to jedna lista ze wszystkich źródeł. Aktualizujesz tam, gdzie zawsze, albo akcją z karty urządzenia
(apt upgrade przez agenta, Deploy stacka w Komodo) — patrz [Akcje](#akcje).

| Źródło | Co podaje | Co trzeba zrobić |
|---|---|---|
| Proxmox VE | pakiety węzła (jak *Węzeł → Updates*) | nic — token PVEAuditor już to widzi |
| PBS | pakiety serwera (jak *Administration → Updates*) | nic — rola Audit |
| Komodo | usługi stacków z nowszym obrazem | w stacku włączone *Poll for updates* |
| OpenWrt | pakiety (`opkg list-upgradable`, na 25.x `apk list --upgradable`) | skrypt ze strony integracji (konto, ACL, cron `opkg update` raz na dobę) |
| Agent | pakiety apt (z poprawkami bezpieczeństwa), wymagany restart, montaże NFS | instalacja na maszynie (niżej) |

TrueNAS i router TP-Link na razie nie podają aktualizacji (TrueNAS — później; TP-Link nie ma tego w API).
TrueNAS za to pokazuje na swojej zakładce wszystkie powiadomienia z dzwonka (od informacji po krytyczne), a pod nimi
dyski: pula, temperatura i ostatni test SMART. „Uwaga” na karcie TrueNAS dają tylko błędy i poważniejsze alerty,
nieudany test SMART i za gorący dysk (HDD od 55 °C, SSD od 70 °C). Testy SMART planujesz w TrueNAS
(*Data Protection → S.M.A.R.T. Tests*) — Denali tylko czyta wyniki.

**Router OpenWrt** dodajesz w Integracjach: na stronie integracji jest skrypt do wklejenia w SSH routera. Zakłada dwa
konta rpcd (nie systemowe): `denali` czyta stan (model, wersja, WAN, porty, Wi-Fi, klienci z DHCP, tablicy ARP i Wi-Fi,
pakiety), `denali-kopia` może tylko zrobić kopię konfiguracji. ACL daje dokładnie te obiekty ubus i pliki, których panel
używa. Klienci trafiają do kolejki jak przy TP-Linku, a zakładka *Sieć* pokazuje dodatkowo WAN, porty z prędkością
i sieci Wi-Fi. Gdy WAN nie działa, router ma „uwagę”. Aktualizacji firmware'u panel nie sprawdza (to `owut` albo LuCI
*Attended Sysupgrade*). Wymaga LuCI albo pakietów `uhttpd-mod-ubus` i `cgi-io`.

**OVHcloud (VPS)** dodajesz w Integracjach; instrukcja założenia kluczy tylko do odczytu jest na stronie integracji.
Zakładka VPS pokazuje stan maszyny, termin opłaty, plan, adresy, kopie, zaległości i ostatnie faktury, a po zatwierdzeniu
VPS-a w kolejce także przycisk terminala SSH. Przy odnowieniu ręcznym „uwaga” pojawia się 14 dni przed terminem. „Uwaga”
nie wysyła powiadomienia na Discord; awaria (VPS wyłączony albo wygasły) już tak. Public Cloud nie jest obsługiwany.

**Klucz panelu** to jeden klucz SSH na wszystkie maszyny. Panel tworzy go sam, a przy pierwszym logowaniu hasłem w Terminalach
dopisuje go na maszynie (pole „Dodaj klucz panelu”) i zapamiętuje sam klucz, nie hasło. Klucz publiczny i gotowe polecenie
skopiujesz z karty Terminale, gdy panel nie może go dopisać sam, np. przy logowaniu hasłem wyłączonym na serwerze.

**Agent** instaluj tylko tam, gdzie API nie wystarcza — zwykle VM-ki z Dockerem i usługami. Na Proxmoxie, PBS i TrueNAS
nie. *Integracje → Dodaj: Agent na maszynie → Wygeneruj polecenie* i wklej je na maszynie (Debian/Ubuntu z systemd,
Python 3.9+). Polecenie pobiera plik, sprawdza jego SHA-256 i dopiero wtedy go uruchamia; kod jest jednorazowy i ważny
godzinę. Maszyna sama przypnie się do swojej VM z Proxmoxa (po IP albo MAC).

Wymaga działającego HTTPS (Nginx Proxy Manager, krok 4 instalacji): agent łączy się z `DENALI_BASE_URL` i sprawdza
certyfikat — token nie idzie zwykłym HTTP.

- `denali-agent.service` — raport co minutę, jako zwykły użytkownik `denali-agent`, system tylko do odczytu.
- `denali-agent-ukryte.timer` — raz dziennie, jako root, **bez sieci**: czy pod montażami NFS nie leżą dane zapisane,
  gdy udział nie był podłączony.
- `denali-agent-docker.timer` — co 10 minut, jako root, **bez sieci**: stan kontenerów, rozmiar warstwy i pliki core. Gniazdo
  Dockera to w praktyce root, więc usługa raportu go nie dotyka, tylko czyta wynik z pliku. Logi i zmienne środowiskowe
  kontenerów nie opuszczają maszyny.
- `denali-agent-akcje.path` → `denali-agent-akcje.service` (root) — wykonuje akcję zleconą z panelu, **tylko jeśli
  jest na lokalnej liście** w `/etc/denali-agent/config.json`. Domyślnie lista jest pusta: agent niczego nie wykona.
  Pozwolenie: `sudo python3 /opt/denali-agent/denali-agent.py pozwol apt_upgrade` i `… pozwol reboot` (cofnięcie:
  `zabron`), albo od razu przy instalacji: `… install … --akcje apt_upgrade,reboot`. Panel tej listy nie zmieni.
- Agent nie słucha na żadnym porcie. Zlecenie przychodzi w odpowiedzi na jego raport; usługa raportu tylko zapisuje
  je do pliku, a wykonuje osobna usługa po sprawdzeniu lokalnej listy.
- Agent sprzed 1.4 działa dalej, ale nie zgłasza dysków, usług ani kontenerów: strona agenta pokaże „nieaktualny”.
- Agent zainstalowany przed wersją 1.1 nie zna akcji, a przed 1.2 — restartu maszyny. Wygeneruj nowe polecenie i wklej
  je jeszcze raz (ta sama maszyna zostaje tą samą integracją, z historią; lokalna lista akcji zostaje).
- Stan: `systemctl status denali-agent`, `journalctl -u denali-agent -n 30`; raport bez wysyłania:
  `python3 /opt/denali-agent/denali-agent.py print`.
- Usunięcie: `sudo python3 /opt/denali-agent/denali-agent.py uninstall`, a w Denali usuń agenta (unieważnia token).
- Brak raportu dłużej niż 5 minut: stan maszyny „nieznany” i błąd przy agencie na liście integracji.

Montaż NFS z `/etc/fstab` staje się zależnością „dane” od serwera (np. TrueNAS) — gdy TrueNAS padnie, incydent wskaże
TrueNAS, a nie VM. Profil *Dostępne aktualizacje: ignoruj* chowa urządzenie z licznika (np. maszyna testowa).

---

## Akcje

Proxmox ma jedno konto i jeden token do wszystkiego: w integracji zaznaczasz zakresy (odczyt jest zawsze, do tego Akcje
i Konsola), panel pokazuje polecenie `pveum` z dokładnie tymi uprawnieniami, a po zapisie sprawdza, czy token je ma.
Dodanie zakresu i zmiana adresu Proxmoxa przy nadanych zakresach wymagają potwierdzenia hasłem. Komodo ma nadal osobny
klucz do akcji.

Restart VM, aktualizacja stacka, `apt upgrade` — z karty urządzenia (*Akcje*), a dla maszyn i stacków także z zakładek
*Infrastruktura → Proxmox* i *Docker* (kolumna *Akcje*, tylko administrator; przyciski pojawiają się tylko przy akcjach,
które już przeszły zamki). Akcja wykona się tylko, gdy przejdzie
wszystkie zamki, sprawdzane po stronie serwera przy każdym zleceniu (także przy bezpośrednim wywołaniu adresu):

1. **akcje włączone** — *menu konta → Akcje* (ta strona pokazuje też dla każdej integracji, czego brakuje, i pozwala od razu zmienić profile); „Wyłącz wszystkie akcje” działa od razu, włączenie wymaga hasła i kodu;
2. akcja jest na liście **„Dozwolone akcje”** urządzenia — ustawiasz ją raz w *Profilach* (np. profil maszyn
   wirtualnych: snapshot, restart), na karcie urządzenia tylko wyjątki; domyślnie lista jest pusta;
3. integracja ma **osobny token do akcji** (*Integracje → … → Akcje — osobny token*), zapisany oddzielnie od tokenu
   do odczytu; usunięcie go wyłącza akcje tej integracji. Agent zamiast tokenu ma lokalną listę na maszynie;
4. akcja pasuje do stanu obiektu (np. „Uruchom” tylko wyłączonej VM) i nie trwa na nim inna;
5. **niebezpieczne** (wyłączenie, restart, zatrzymanie, Deploy, apt upgrade, restart maszyny) — po potwierdzeniu hasłem i kodem 2FA
   (ważne 5 minut).

Zmiana list dozwolonych i tokenu do akcji też wymaga hasła i kodu. Gdy potwierdzenie jest potrzebne, panel sam pokazuje
okno z hasłem (albo Pocket ID), a po nim dokańcza kliknięte działanie bez ponownego wypełniania formularza. Każde zlecenie, odmowa i wynik są w *Audycie*,
historia w *Akcjach*, a przy trwającym incydencie urządzenia — w jego przebiegu.

| Źródło | Akcje | Token do akcji |
|---|---|---|
| Proxmox VE | Uruchom, Wyłącz, Uruchom ponownie, Zatrzymaj natychmiast, Zrób snapshot (VM i LXC) | jeden token integracji, zakres „Akcje” (`VM.PowerMgmt`, `VM.Snapshot`), polecenie układa panel |
| Komodo | Uruchom, Uruchom ponownie, Zatrzymaj, Aktualizuj (Deploy) — na stackach | klucz osobnego użytkownika z prawem **Execute** tylko na wybranych stackach |
| OpenWrt | Kopia konfiguracji (to samo co `sysupgrade -b` / LuCI *Generate archive*) — na routerze nic się nie zmienia | osobne konto rpcd `denali-kopia` z prawem tylko do kopii (`cgi-io backup`) |
| Agent | Zainstaluj aktualizacje (`apt-get update` + `apt-get upgrade`, bez usuwania pakietów), Uruchom ponownie maszynę (`shutdown -r +1`), Usuń pliki core, Zamontuj ponownie NFS, Usuń nieużywane obrazy, Przytnij dziennik, Kopia baz Postgres (od agenta 1.4) | lokalna lista na maszynie (`pozwol KLUCZ`: `apt_upgrade`, `reboot`, `clean_cores`, `nfs_remount`, `docker_prune`, `journal_vacuum`, `db_backup`) |

`apt upgrade` na VM z Proxmoxa: w oknie potwierdzenia „Najpierw snapshot w Proxmoxie” (gdy snapshot tej VM jest
dozwolony i Proxmox ma token do akcji) — aktualizacja ruszy dopiero po udanym snapshocie, a nieudany snapshot ją
wstrzymuje. „Potem uruchom ponownie, jeśli jest wymagany restart” (gdy restart przez agenta jest dozwolony): po udanej
aktualizacji agent sprawdza `/run/reboot-required` i nowsze jądro i tylko wtedy restartuje maszynę za minutę; nieudana
aktualizacja = bez restartu. Na VM z Proxmoxa restart „z zewnątrz” to dalej „Uruchom ponownie” z Proxmoxa.
Panel nie ma akcji na samym sobie, a na routerze tylko kopię konfiguracji.

**Kopia konfiguracji OpenWrt** trafia na kartę urządzenia (*Kopie konfiguracji*), zaszyfrowana w bazie — archiwum
zawiera hasła Wi-Fi i skróty haseł kont. Listę widzi tylko administrator, pobranie wymaga hasła i kodu i trafia do
audytu. Kopia identyczna z poprzednią nie tworzy nowego wpisu; panel trzyma 10 różnych kopii na router. Kopie są w bazie,
więc wchodzą do kopii zapasowej panelu. Przywracasz je w LuCI: *System → Backup / Flash Firmware → Restore backup*.

---

## Terminale

Karta tylko dla administratora (dolny pasek na pulpicie, górne menu na pozostałych stronach). Dwa narzędzia, oba
bez *Profili*: zgodą jest zakres w integracji albo lokalna lista agenta, a kartę wyłącza też wyłącznik akcji. „Wyłącz wszystkie akcje”
zamyka też otwarte konsole (w ciągu 15 sekund).

**Terminal** — prawdziwa powłoka po SSH w przeglądarce (xterm.js), dla każdego urządzenia z adresem na karcie: VM, LXC,
hosta Proxmoxa, NAS-a. Działa jak cmd: Ctrl+C kopiuje zaznaczenie (bez zaznaczenia przerywa program), Ctrl+V wkleja, prawy
przycisk robi jedno albo drugie, okno dopasowuje się do ekranu. Z boku są gotowe polecenia: klik wkleja, Enter uruchamia.
Własne dopiszesz w *Terminale → Moje polecenia*. Login i hasło podajesz za pierwszym razem, a po udanym logowaniu trafiają
zaszyfrowane do Skarbca urządzenia (można to odznaczyć). Potem wystarczy przycisk *Terminal* na liście. Klucz serwera zapamiętuje przy pierwszym połączeniu i przy zmianie odmawia. Root po
haśle zwykle jest wyłączony (`PermitRootLogin`): zaloguj się zwykłym użytkownikiem albo użyj klucza prywatnego.

**Ekran (VNC)** — ekran VM albo LXC z Proxmoxa w przeglądarce (noVNC), przez panel, bez kluczy SSH w bazie.
Jednorazowo w Proxmoxie:

1. *Integracje → Proxmox*, sekcja *Uprawnienia*: zaznacz „Konsola”, wklej pokazane polecenie w Shell węzła, *Zapisz i sprawdź* na dole formularza.
2. W Nginx Proxy Manager włącz *Websockets Support*.

Zakres dotyczy wszystkich VM i LXC tego Proxmoxa.

Gdy konsola się nie połączy, pasek konsoli i *Ostatnie sesje konsoli* podają powód. Otwarcie wymaga hasła i kodu, a sesja kończy się po 2 godzinach albo po wylogowaniu. System w maszynie dalej pyta
o własne hasło. Treści sesji panel nie zapisuje, w *Audycie* są otwarcie, odmowa i koniec (`console_open`,
`console_blocked`, `console_close`). Konsoli hosta Proxmoxa tu nie ma — token API wymagałby do niej `Sys.Console` i
hasła konta na hoście; hosta obsługujesz z panelu Proxmoxa.

**Gotowe polecenia** — stała lista tylko do odczytu (dyski, pamięć, procesy, usługi z błędem, logi, Docker, porty,
adresy), przez agenta 1.3 lub nowszego. Panel wysyła tylko klucz polecenia, a treść jest w kodzie agenta, więc dowolnego
polecenia zlecić się nie da. Na maszynie: `sudo python3 /opt/denali-agent/denali-agent.py pozwol polecenia`. Wynik pojawia się po około minucie, z kolejnym raportem agenta.

## Alerty

Incydent znaczy „urządzenie nie działa”. Alert znaczy „działa, ale coś wymaga reakcji”. Lista jest w *Powiadomienia → Alerty*, skrót na
pulpicie i na karcie urządzenia. Sam stan „uwaga” niczego nie wysyła, bo uwagą jest też np. lista pakietów sprzed
tygodnia: powiadomienie dostają tylko rzeczy z tej listy.

| Skąd | Co |
|---|---|
| TrueNAS | pula inna niż ONLINE, pula zajęta od 90%, nieudany test SMART, za gorący dysk, błąd snapshotów albo replikacji, alerty TrueNAS od poziomu ERROR |
| PBS | kopia starsza niż 36 h (krytyczne po 7 dniach), magazyn zajęty od 85% |
| Proxmox | dysk węzła i kontenera LXC, RAM węzła, magazyny Proxmoxa, brak kworum klastra |
| Komodo | stack `unhealthy` albo w pętli restartów |
| Agent | dysk, i-węzły, RAM, swap, usługi failed, NFS (odłączony, wolny, dane pod montażem), kontener niezdrowy, restartujący się, zatrzymany z błędem, rosnąca warstwa zapisywalna, pliki core |
| Sprawdzenia | certyfikat za mniej niż 14 dni, aplikacja niedostępna z internetu dłużej niż 5 minut |
| Terminy | gwarancja, domena, licencja: 30 dni przed, pilne 7 dni przed |
| Panel | brak internetu w domu |

- Powiadomienie idzie po 5 minutach, jedna wiadomość na wagę, „rozwiązane” po 10 minutach spokoju.
- Progi dysku (domyślnie 85 i 95%) i „Powiadamiaj o alertach” ustawiasz w *Profilach*, na urządzeniu tylko wyjątki.
- *Wycisz* na 1, 7 albo 30 dni: alert zostaje na liście, ale nie wysyła wiadomości.
- Gdy urządzenie leży albo ma planowaną przerwę, alert czeka: mówi o tym incydent.
- Gdy źródło milczy (agent nie raportuje), alert zostaje otwarty: brak danych to nie naprawa.

**Brak internetu w domu.** Panel co pół minuty łączy się z 1.1.1.1, 8.8.8.8 i 9.9.9.9. Po dwóch porażkach VPS i adresy
publiczne dostają „nie da się sprawdzić” zamiast fałszywej awarii. Wiadomość o przerwie przychodzi po powrocie łącza.

**Oś czasu** (*Powiadomienia → Oś czasu*): zmiany z ostatnich 90 dni, także te spoza panelu: nowy obraz, nowy lub usunięty
kontener, restart systemu i kontenera, zmiana adresu, instalacja pakietów. Zmiany z 24 h przed awarią trafiają do incydentu.

**Eksport Markdown** (*Powiadomienia → Alerty*, na karcie urządzenia w *Ustawieniach*): stan do diagnozy, dla siebie
albo dla modelu językowego. Bez skarbca i plików, sekrety w notatkach są maskowane według wzorców. Maskowanie zmniejsza
ryzyko, ale nie daje gwarancji: przejrzyj plik, zanim go gdzieś wkleisz.

## Monitorowanie

- **Własne sprawdzenia** (karta urządzenia → *Ustawienia*): HTTP, TCP, ping, certyfikat TLS, DNS, tylko tam, gdzie te liczone same nie wystarczą.
- **Sprawdzenia liczą się same** z adresów urządzenia: ping (IP albo nazwa hosta), HTTP (adres aplikacji), „z internetu”
  (domena w publicznym DNS), **DNS** (nazwa hosta z kropką, np. `nas.lan`, ma wskazywać IP urządzenia — najwyżej „uwaga”)
  i **porty TCP** wpisane w *Edytuj → Porty do sprawdzania* (np. `5432, 1883` dla bazy i MQTT).
- **Historia** na karcie urządzenia: pasek dostępności (24 h i 7 dni) i czas odpowiedzi z ostatniej doby.
  Zmiany stanu zostają 180 dni, próbki czasu odpowiedzi (co 5 min) 30 dni.
- **Planowana przerwa** — karta urządzenia → *Zacznij przerwę* (15 min – 4 h). Awaria urządzenia i tego, co od niego
  twardo zależy, nie otworzy incydentu ani nie wyśle powiadomienia. Kończy się sama albo *Zakończ teraz*.
- **Migotanie** — gdy przyczyna padnie 3 razy w 30 minut, incydent zostaje otwarty i zamyka się dopiero po 10 minutach
  spokoju: jedno powiadomienie i jedno „rozwiązane” zamiast serii.
- **Powiadomienia** mówią, co dokładnie zgłosiło źródło (np. „192.168.1.10 nie odpowiada”, „port 5432 zamknięty”),
  a „rozwiązane” — co było, co jest teraz i ile trwało.
- **Udostępnianie** (*Konto → Dodatki → Udostępnianie domownikom*) daje wybranym osobom własną stronę: tylko przydzielone usługi (link
  albo adres do skopiowania, np. SMB), status bez nazw urządzeń i powodów awarii oraz instrukcje, np. gotową do Tailscale.
  Konto ma rolę `uzytkownik`: panel wygeneruje mu hasło albo konto powstanie samo przy logowaniu Pocket ID z grupy
  `DENALI_OIDC_USER_GROUP`. Reszta panelu jest dla tej roli zamknięta, ale do sieci wpuszcza Tailscale: osoba musi być
  zaproszona do tailnetu (udostępnione urządzenie nie przekazuje podsieci subnet routera), a ACL ma ograniczać ją
  do SMB i panelu. Bez tego ma dostęp do całego LAN-u.
- **Ikony pulpitu** dodajesz przyciskiem „+” w kategorii albo w trybie *Edytuj pulpit* (przeciąganie za uchwyt albo strzałki z klawiatury, nazwa kategorii w miejscu, usuwanie z „Cofnij”, okno „Dodaj do pulpitu”): dowolny
  adres, strona panelu (też terminal konkretnej maszyny), adres do skopiowania (`\\\\nas\\zdjecia`, `smb://`, `nfs://`, `ssh://`:
  kliknięcie kopiuje, bo przeglądarka tego nie otworzy) albo urządzenie (wtedy ikona pokazuje jego stan). Panel proponuje jednym kliknięciem aplikacje
  z adresem, integracje i swoje strony. Kategorie możesz nazwać i ułożyć po swojemu; ikony znajdzie też Ctrl+K.
- **Statystyki** na pulpicie to widżety z integracji. W trybie edycji *Dodaj statystykę* prowadzi przez źródło, metryki i progi, z podglądem na prawdziwych danych. Progi działają jak zwykłe alerty: trafiają do „Wymaga uwagi”, a powiadomienie idzie po 5 minutach (wyłączysz je jednym polem).
- **Pulpit** zaczyna się od jednego zdania: „Wszystko działa”, „Nie odpowiada: … (jeszcze nie awaria)” albo
  „1 problem: …” z linkiem do incydentu. Obok widżet **Nadchodzące**: wygasające certyfikaty, opłaty OVH,
  aktualizacje bezpieczeństwa czekające ponad 7 dni, stara kopia Denali i magazyny, które się zapełniają.
- **Co się zmieniło przed awarią** — nowy incydent sam dostaje listę zmian z ostatnich 2 godzin przy przyczynie i tym,
  od czego zależy: akcje z panelu, zmieniona konfiguracja routera, wahania stanu, koniec przerwy, edycja dokumentacji.
  Te same zmiany idą w powiadomieniu („Wcześniej: …”).
- **Restart z panelu** (Proxmox, agent, Komodo) sam zakłada planowaną przerwę (10–60 min, zależnie od akcji).
  Serwer fizyczny ma *Bezpieczny restart…*: kolejność wyłączania i uruchamiania maszyn liczona z zależności,
  przerwa jednym kliknięciem, stan na żywo. Sam restart węzła robisz w Proxmoxie — panel nie ma do tego uprawnień.
- **Mapa**: od lewej *Z zewnątrz* (VPS), *Internet* (łącze od dostawcy — stan z testu łącza), sieć, serwery w ramkach,
  aplikacje. Kolor paska na kafelku = sieć (VLAN z routera OpenWrt, bez niego /24 z adresu). Powiązania widać po najechaniu na urządzenie (albo wszystkie
  przyciskiem), przerwane dane zawsze. *Ułóż mapę* (administrator): przeciągasz ramki i kafelki, *Zapisz układ*;
  *Układ automatyczny* wraca do rysowanego samego. *Co jeśli…?* pokazuje, co przestanie działać, gdy wybrane urządzenie
  padnie (symulacja, nic nie jest wyłączane).
- **Prognoza miejsca** — pule TrueNAS i magazyny PBS: zajętość co godzinę, prognoza z przyrostu w ostatnich 14 dniach
  (potrzeba min. 2 dni danych). Poniżej 14 dni do zapełnienia — wiadomość na Discord (raz na tydzień).
- **Podsumowanie tygodnia** w poniedziałek od 8:00 (albo *Kanały powiadomień → Wyślij teraz*), a dostępność za miesiąc pierwszego dnia miesiąca.
- **Kanały**: Discord, ntfy, Telegram, e-mail (menu konta → *Kanały powiadomień*). Krytyczny incydent bez reakcji może wracać co 1–24 h (*Powtórki*).
- **Producent po MAC** na zakładce *Sieć*: lista IEEE, pobierana raz na miesiąc do katalogu danych.
- **Ctrl+K** (albo `/`) — szybkie przejście do urządzenia, procedury, incydentu albo strony panelu.
- **Dla innych programów**: ustaw `DENALI_METRICS_TOKEN` w `.env`, a panel wystawi `/metrics` (Prometheus) i
  `/api/stan` (JSON). Oba wymagają nagłówka `Authorization: Bearer <token>`; bez tokenu w `.env` ich nie ma.
  Home Assistant, `configuration.yaml`:

  ```yaml
  rest:
    - resource: https://denali.serwispowers.com/api/stan
      headers: { Authorization: !secret denali_token }   # w secrets.yaml: "Bearer <token>"
      scan_interval: 60
      sensor:
        - name: Denali otwarte incydenty
          value_template: "{{ value_json.incidents | length }}"
  ```

## Tablet przy szafie

Zamiast monitora z KVM: na ścianie podgląd i alarmy, a gdy go zdejmiesz — po PIN-ie SSH, konsole VNC i akcje.

1. *Konto → Dodatki → Tablet na ścianie → Utwórz kod*, na tablecie otwórz `https://denali.serwispowers.com/tablet` i wpisz kod.
   Tablet działa tylko z adresu, z którego się połączył — daj mu stały adres w DHCP.
2. W tym samym miejscu przy tablecie: **Ustaw PIN** (6–12 cyfr, panel poprosi o hasło i kod 2FA).
3. Na tablecie *Odblokuj* → PIN → tablet działa jako Twoje konto administratora: kafle SSH, Konsole, Akcje,
   bezpieczny restart serwera. Po 10 minutach bez dotyku (najpóźniej po godzinie) sam wraca na ścianę, a otwarte
   konsole się zamykają. 5 złych prób kasuje PIN i wysyła powiadomienie.
4. *Ustawienia tego tabletu* (na dole ekranu): pełne statystyki albo tryb przyciemniony (zegar i werdykt; rozświetla się
   przy alarmie albo dotknięciu) oraz godziny ciszy — wtedy bez dźwięku i z przygaszonym ekranem.

Alarm: nowy incydent → czerwony ekran, wybudzenie ekranu i krótki dźwięk (poza ciszą).
**Fully Kiosk Browser** (Android), ustawienia:

| Gdzie | Co |
|---|---|
| Web Content Settings | *Autoplay Audio* — dźwięk alarmu bez wcześniejszego dotknięcia |
| Advanced Web Settings | *Enable JavaScript Interface* — panel sam wybudza ekran przy alarmie |
| Device Management | *Keep Screen On* wyłączone, *Screen Off Timer* np. 5 min — ekran gaśnie, alarm go budzi |
| Motion Detection | opcjonalnie: wybudzanie ekranu, gdy podejdziesz |
| Kiosk Mode | blokada wyjścia z przeglądarki |

## Migracja z HLC

Przenosi z HLC: urządzenia (z notatkami, dostępem, portami), adresy, zależności, kable, runbooki, log problemów,
pliki, hasła (do skarbca), procedury „na wypadek”, kontakt alarmowy, hasła Wi-Fi i ikony aplikacji ze strony Start.
Do tego terminy (gwarancja, domena), porty TCP, własne sprawdzenia, opis sieci i pul DHCP (jako runbook), ostatnią wersję
kopii konfiguracji (jako pliki urządzenia) i oś czasu z 90 dni. Migrację można powtórzyć po aktualizacji Denali: doda tylko to,
czego wcześniej nie było. Nie przenosi kont (załóż w Denali), kanałów powiadomień, sprawdzeń „z internetu” (Denali liczy je
samo), historii alertów ani raportów.
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
  (wylogowanie wszędzie, wygasanie po 12 h bezczynności). Opcjonalnie Pocket ID (OIDC z PKCE) obok własnego konta.
- Role: administrator i podgląd. Tablet: tylko podgląd, tylko ze swojego adresu.
- Skarbiec zaszyfrowany; pokazanie hasła wymaga ponownego potwierdzenia hasłem i 2FA i trafia do audytu.
- Każda zmiana ma wpis w audycie. Powłoka tylko na karcie Terminale: dla administratora, po potwierdzeniu, z zapisem otwarcia i zamknięcia.
- Akcje tylko z jawnej listy w kodzie i z listy dozwolonych urządzenia, osobnym tokenem o minimalnych uprawnieniach;
  niebezpieczne po ponownym potwierdzeniu; wyłącznik wszystkich akcji jednym kliknięciem.
- Agent: token tylko w `/etc/denali-agent/config.json` (root i grupa agenta), w Denali tylko jego skrót; raport
  wysyła zwykły użytkownik bez prawa zapisu do systemu; akcję wykonuje tylko z lokalnej listy, której panel nie zmieni.

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
`export`, `backup`, `actions`), `denali/web/` — trasy, `denali/templates/` — widoki, `denali/migrations/` — schemat bazy (Alembic).
PDF eksportu wymaga WeasyPrint (`pip install -e ".[pdf]"` + biblioteki pango; w obrazie Dockera jest).
