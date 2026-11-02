# Do zrobienia

Stan na 31.10.2026. Etapy 2 (rdzeń) i 3 (status) odebrane. Etapy 4 (aktualizacje) i 5 (akcje): kod gotowy, czekają
na wdrożenie i sprawdzenie na serwerach. Odłożone pomysły są w [POMYSLY.md](POMYSLY.md), decyzje w [PROJEKT.md](PROJEKT.md).

---

## 1. Po Twojej stronie — teraz

- [ ] `git push origin main`, potem *Deploy* stacka `denali` w Komodo (nowe: odświeżanie na żywo, zależność klientów
      od routera/AP, sprawdzenia z internetu, **Etap 4**, zakładki Infrastruktury, **Etap 5** z uzupełnieniami — migracje bazy 0009–0013
      wykonają się same przy starcie).
- [ ] **Nginx Proxy Manager:** proxy host `denali.serwispowers.com` → `http://IP-VM:8000`, certyfikat Let's Encrypt
      (DNS Challenge, Cloudflare), Force SSL + HSTS, Access List tylko LAN + Tailscale (`100.64.0.0/10`).
      Instrukcja: README → Instalacja → krok 4.
- [ ] Po NPM: w *Environment* stacka **`DENALI_DEV=0`** (teraz jest `1`, bo panel działa po zwykłym HTTP).
- [ ] Po NPM: sprawdź w *Audycie*, czy przy logowaniu jest adres Twojego komputera, a nie NPM
      (jeśli NPM — popraw `DENALI_TRUSTED_PROXIES`).
- [ ] Rekord DNS `denali` w Cloudflare (szara chmurka) albo wpis w lokalnym DNS.
- [ ] Procedura **„Jak odtworzyć Denali”** (Procedury → Nowa, zakres „gdy nie działa sam panel”) — trafi na górę kopii awaryjnej.
- [ ] Zapisz w menedżerze haseł: `DENALI_ENCRYPTION_KEY` i 10 kodów zapasowych 2FA (jeśli jeszcze nie).

## 2. Bezpieczeństwo — do domknięcia

- [ ] Hasło `Khuba123` trafiło do logu Komodo i do rozmowy — jeśli używasz go gdziekolwiek, zmień je tam.
- [ ] `HLC_FERNET_KEY` trafił do rozmowy — po wyłączeniu HLC usuń jego stare kopie (`/var/lib/hlc/backups`).
- [ ] **Usuń zasób `hlc.serwispowers.com` z Pangolina** (audyt, problem B0), gdy Denali zastąpi HLC.
- [ ] Wyłącz HLC (kontener `hlc-dom`), gdy upewnisz się, że w Denali niczego nie brakuje.
- [ ] Razem z HLC **usuń stare agenty HLC** z maszyn (usługa `hlc-agent` jako root, token w `/etc/hlc-agent/`):
      `sudo python3 /opt/hlc-agent/agent.py --uninstall` (usuwa usługę, program, konfigurację i bufor). Instalator
      nowego agenta Denali sam ostrzega, gdy znajdzie stary.
- [ ] Wyjaśnij, czym było „627379008” (jeśli to hasło routera — wpisz je tylko w formularzu Denali).

## 3. Etap 4 — sprawdzenie na prawdziwych serwerach

Kod sprawdzony testami i przez prawdziwe HTTP (rejestracja i raport), ale **sam agent nie był uruchomiony na Linuksie**
(brak maszyny przy pisaniu) — pierwsza instalacja może wymagać poprawki (wtedy wklej wynik polecenia albo
`journalctl -u denali-agent -n 30`).

- [ ] *Aktualizacje*: pakiety węzła pve zgadzają się z Proxmoxem (*pve → Updates*), PBS z PBS (*Administration → Updates*).
- [ ] *Aktualizacje*: stacki z nowszym obrazem zgadzają się z Komodo (kropka „update available” przy stacku).
- [ ] Agent na jednej VM z Dockerem: *Integracje → Dodaj: Agent na maszynie*, wklej polecenie. Po minucie: VM
      przypięta (bez kolejki), pakiety na liście = `apt list --upgradable` na tej VM.
- [ ] Na tej VM: montaż NFS z TrueNAS widoczny na stronie agenta, na karcie VM zależność „dane · wykryta, aktualna”
      od TrueNAS. Po kilkunastu minutach (timer) „dane pod montażem sprawdzone …”.
- [ ] Agent w kontenerze LXC: jeśli usługa nie startuje (ograniczenia LXC przy zabezpieczeniach systemd), wklej
      `journalctl -u denali-agent -n 30`.
- [ ] Akceptacja Etapu 4.

## 3a. Etap 5 — sprawdzenie na prawdziwych serwerach

Kod sprawdzony testami na nagranych odpowiedziach API (Proxmox, Komodo) i na plikach zleceń agenta. **Żadna akcja nie
była wykonana na prawdziwym Proxmoxie, Komodo ani maszynie** — przy pierwszej próbie wklej wynik z *Akcje → przebieg*.
Zacznij od maszyny testowej (VM 120 albo inna, której restart niczego nie psuje).

- [ ] Proxmox: rola `PanelActions` (`VM.PowerMgmt`, `VM.Snapshot`) i token `denali@pve!akcje` na `/vms` (instrukcja na
      stronie integracji Proxmox) → *Akcje — osobny token* → zapisz (wymaga hasła i kodu).
- [ ] *Profile*: w profilu maszyn wirtualnych zaznacz „Proxmox VE: Zrób snapshot” i „Uruchom ponownie”.
- [ ] Testowa VM: *Akcje → Zrób snapshot* → przebieg „wykonana”, snapshot widać w Proxmoxie (*VM → Snapshots*).
      Potem „Wyłącz” i „Uruchom” — stan VM na karcie zmienia się po następnym odczycie (do minuty).
- [ ] Jeśli Proxmox odpowie 403: tę samą rolę na `/vms` nadaj też użytkownikowi `denali@pve` (token z oddzielonymi
      uprawnieniami ma najwyżej to, co jego użytkownik).
- [ ] Komodo: użytkownik `denali-akcje` z **Execute** na jednym stacku testowym, klucz w *Akcje — osobny token*,
      w profilu „Komodo: Uruchom ponownie” → restart stacka → przebieg „wykonana” (Komodo: *Updates* pokazuje ten sam restart).
- [ ] Agent 1.1 na testowej VM: nowe polecenie z *Dodaj: Agent na maszynie* (stary agent 1.0 nie zna akcji), potem
      `sudo python3 /opt/denali-agent/denali-agent.py pozwol apt_upgrade`. Na stronie agenta: „Lokalna lista: apt_upgrade”.
- [ ] Profil: „Agent na maszynie: Zainstaluj aktualizacje” → karta VM → *Zainstaluj aktualizacje* z „Najpierw snapshot”
      → snapshot, potem apt upgrade (do 2 minut na odebranie zlecenia), wynik apt w przebiegu, lista pakietów pusta po kolejnym raporcie.
      Gdyby utknęło na „zlecona”: `systemctl status denali-agent-akcje.path` i `journalctl -u denali-agent-akcje -n 50`.
- [ ] Agent w LXC: czy `denali-agent-akcje.path` startuje (ograniczenia LXC).
- [ ] Bezpośredni POST na adres akcji niedozwolonej → odmowa i wpis `action_blocked` w *Audycie* (sprawdzone testami,
      warto raz zobaczyć w prawdziwym panelu).
- [ ] *Akcje → Wyłącz wszystkie akcje* → przyciski znikają z kart; włączenie wymaga hasła.
- [ ] Akceptacja Etapu 5.

### Uzupełnienia Etapu 5 (2.11.2026) — do sprawdzenia razem z nim

- [ ] *Infrastruktura → Proxmox* i *Docker*: kolumna *Akcje* z przyciskami tylko tych akcji, które są dozwolone
      i gotowe (np. „Zrób snapshot…”); przycisk otwiera to samo okno potwierdzenia co karta urządzenia.
- [ ] Agent 1.2 na testowej VM (ponowna instalacja poleceniem z panelu), `sudo python3 /opt/denali-agent/denali-agent.py
      pozwol reboot`, w profilu „Agent na maszynie: Uruchom ponownie maszynę”. *Zainstaluj aktualizacje* z „Potem uruchom
      ponownie, jeśli wymagany” → po aktualizacji wynik restartu: „Restart niepotrzebny” albo „Restart za minutę”, a VM
      faktycznie wstaje (w LXC też: `shutdown -r` w kontenerze).
- [ ] *Docker*: przy każdej usłudze obraz i tag (np. `immich-server:release`) zgadzają się z plikiem compose w Komodo.
- [ ] *TrueNAS*: lista powiadomień = dzwonek w TrueNAS (bez odrzuconych). Dyski: pula, temperatura, ostatni test SMART
      zgadzają się z *Storage → Disks* i *Data Protection → S.M.A.R.T. Tests*. Jeśli zamiast wyników SMART jest
      „TrueNAS nie podał wyników SMART” albo nie ma temperatur — wklej wersję TrueNAS (25.10 zmieniło API SMART).

### Etap 3 — zostało do sprawdzenia po wdrożeniu

- [x] Integracje: Proxmox, Komodo, PBS, TrueNAS, router TP-Link; powiadomienia na Discord; strażnik (potwierdzone 31.10).
- [x] **Test kryterium Etapu 3:** wyłączona testowa VM → incydent → Discord → „Rozwiązane” (potwierdzone 31.10).
- [ ] Po wdrożeniu: pulpit na tablecie i w przeglądarce zmienia się **sam**, bez przeładowania strony.
- [ ] Po wdrożeniu: na karcie urządzenia w Wi-Fi „zależy od <AP> · sieć · twarda · wykryta, aktualna” (do 5 min po
      odczycie routera). Sprawdź, czy router pokazuje klientów AP jako swoich (wtedy mają też miękką zależność od routera).
- [ ] Po wdrożeniu: na karcie aplikacji wystawionej przez Pangolina blok **„Z internetu (przez publiczny DNS)”**
      z „HTTP 200/302”. Działa tylko, gdy *adres aplikacji* na karcie to publiczna nazwa (np. `https://immich.serwispowers.com`);
      przy adresie z IP w LAN sprawdzenia z internetu nie ma.
- [x] Akceptacja Etapu 3 (31.10.2026).

## 4. Braki względem projektu (Etap 3)

- [x] **Sprawdzenia z internetu** („działa w LAN, nie działa z internetu”) — zamiast skryptu na VPS (VPS nie jest
      w Tailscale): Denali bierze adres z publicznego DNS (1.1.1.1 / 8.8.8.8) i łączy się przez internet → Pangolin →
      aplikacja, z pełnym sprawdzeniem certyfikatu. Błąd daje „uwaga” na urządzeniu (bez incydentu i powiadomienia —
      w domu aplikacja działa). Gdy padnie internet w domu: „nie da się sprawdzić”, a powiadomi strażnik (decyzja 31.10).
- [x] **Odświeżanie na żywo (SSE)** — pulpit, listy urządzeń i incydentów oraz karta incydentu podmieniają treść
      przy zmianie stanu (do ~5 s po zapisie workera); nie w trakcie pisania. Do sprawdzenia na tablecie i przez NPM.
- [ ] **OpenWrt** — odłożone (POMYSLY.md); router działa przez nieoficjalną bibliotekę TP-Link.
- [ ] Z migracji HLC **nie przeniesione**: sprawdzenia, kanały powiadomień, konta (z założenia — wracają w nowej postaci).

## 5. Rzeczy niesprawdzone albo do decyzji

- [ ] **PDF kopii awaryjnej** — WeasyPrint jest w obrazie Dockera, ale PDF nie był generowany na serwerze
      (sprawdź, czy w `/opt/denali/data/export/` jest `denali-procedury.pdf`).
- [ ] **Synchronizacja kopii awaryjnej na VPS** — skrypt i instrukcja są (`deploy/vps/README.md`), nic nie zostało ustawione.
- [ ] **Kopia poza domem** (restic na VPS) — opisana w README, nieustawiona.
- [x] **Końce linii w gicie** — `.gitattributes` (`* text=auto eol=lf`).
- [ ] Testy lokalnie działają na Pythonie 3.15, obraz na 3.12 — dotąd bez problemów, ale warto kiedyś puścić testy w obrazie.

## 6. Etap 4 — czego nie ma (świadomie)

- [ ] **Router TP-Link**: brak aktualizacji firmware'u na liście — nieoficjalne API tego nie podaje (wróci z OpenWrt).
- [ ] **TrueNAS**: aktualizacje systemu jeszcze nie na liście (do dopisania w adapterze).
- [ ] **Powiadomienia o aktualizacjach** (np. raz dziennie o nowych poprawkach bezpieczeństwa) — ustawienie profilu
      „powiadom” na razie tylko liczy urządzenie w liczniku.

## 6a. Etap 5 — czego nie ma (świadomie)

- [ ] **Router**: brak akcji. Jedyna planowana (kopia konfiguracji, `sysupgrade -b`) wymaga OpenWrt — biblioteka
      TP-Linka jej nie ma.
- [ ] **TrueNAS, PBS**: bez akcji (nie było ich w projekcie).
- [x] **Restart maszyny przez agenta** po aktualizacji („wymagany restart”) — agent 1.2: akcja `reboot` i opcja
      „potem uruchom ponownie, jeśli wymagany” przy aktualizacji (2.11.2026).
- [ ] **Cofnięcie snapshotu** (rollback) — świadomie tylko w Proxmoxie (nieodwracalne, łatwo stracić dane).
- [x] Przyciski akcji na zakładkach *Proxmox* i *Docker* (2.11.2026).

## 7. Infrastruktura — zakładki z makiety (przed Etapem 5)

W makiecie dział *Infrastruktura* miał zakładki Mapa · Proxmox · TrueNAS · Docker · Sieć; w Denali była tylko lista.
Kod gotowy (31.10.2026), tylko do odczytu — akcje (restart, snapshot, aktualizuj) są na karcie urządzenia.
Zakładka źródła (Proxmox, TrueNAS, Docker, Sieć) pokazuje się dopiero, gdy jest podłączona integracja tego typu.

- [x] **Zakładki**: Mapa · Lista · Proxmox · TrueNAS · Docker · Sieć · Adresy IP · Szafa; „Infrastruktura” w menu
      otwiera Mapę.
- [x] **Proxmox** — węzły (CPU, RAM, czas działania), maszyny i kontenery (stan, CPU, RAM, dysk, ostatnia kopia z PBS).
- [x] **TrueNAS** — pule (zajętość), snapshoty i replikacja, udziały NFS, kto korzysta (zależności „dane”), alerty.
- [x] **Docker** — serwery i stacki z Komodo, pogrupowane po serwerze, z nowszymi obrazami.
- [x] **Sieć** — router i AP, kto jest w sieci (kabel / Wi-Fi, sygnał, przez który AP), nieprzypisani klienci.
- [x] **Adresy IP** — sieci /24 wyliczone same z adresów urządzeń i klientów routera: zajęte (przez co), wolne
      zakresy, konflikty. Bez ręcznego wpisywania sieci; Tailscale i adresy publiczne pominięte.
- [x] **Szafa** — przód i tył szafy, co na której wysokości (U): urządzenie w szynach, sam opis (patch panel,
      zaślepka) albo **półka**, na której stoi obok siebie kilka rzeczy (router, terminal, zwykły komputer — np.
      dół szafy U1–U8). Edycja kliknięciami: wolne U → dodaj (kafelek + wysokość), urządzenie → zmień wysokość,
      przesuń o 1U, przód/tył, wyjmij. Na karcie urządzenia „Szafa · U6”. Migracje bazy 0010–0011.
- [x] **Mapa** — rysowana sama z dokumentacji (sieć → serwery → maszyny → aplikacje; klienci sieci na żądanie),
      stan na żywo, zależność „dane” czerwona, gdy źródło nie działa.
- [ ] **Do sprawdzenia u Ciebie:** wygląd na prawdziwych danych (mapa przy wielu urządzeniach, tablet), czy liczby
      zgadzają się z Proxmoxem / Komodo / routerem.
- [x] Dyski i SMART w TrueNAS (pula, temperatura, ostatni test), wszystkie powiadomienia TrueNAS na zakładce;
      wersje obrazów w Docker = tag z compose (numeru nowszej wersji Komodo nie podaje — tylko „pod tym tagiem jest
      nowszy obraz”) (2.11.2026).
- [ ] Później: porty i prędkość łącza w Sieci (dopiero z OpenWrt), DHCP: która pula jest rozdawana.

## 8. Następne etapy

| Etap | Zakres | Kryterium odbioru |
|---|---|---|
| **5. Akcje** — kod gotowy 31.10.2026, do sprawdzenia (punkt 3a) | lista dozwolonych akcji (przez profil), potwierdzenia, audyt; Proxmox (start, wyłącz, restart, zatrzymaj, snapshot), Komodo (start, restart, stop, Deploy), agent (`apt upgrade` z lokalnej białej listy, snapshot przed); router — bez akcji do czasu OpenWrt | każda akcja ma wpis w audycie; akcja spoza listy jest niemożliwa nawet przez API |
