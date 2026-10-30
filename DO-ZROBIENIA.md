# Do zrobienia

Stan na 30.10.2026. Etap 2 (rdzeń) i kod Etapu 3 (stan na żywo) są gotowe; Etap 3 czeka na sprawdzenie na prawdziwych
serwerach i akceptację. Odłożone pomysły są w [POMYSLY.md](POMYSLY.md), decyzje w [PROJEKT.md](PROJEKT.md).

---

## 1. Po Twojej stronie — teraz

- [ ] `git push origin main`, potem *Deploy* stacka `denali` w Komodo (ostatnie zmiany: PBS, TrueNAS, strażnik, router).
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
- [ ] Wyjaśnij, czym było „627379008” (jeśli to hasło routera — wpisz je tylko w formularzu Denali).

## 3. Etap 3 — sprawdzenie na prawdziwych serwerach

Na prawdziwych danych działa na razie tylko **Proxmox**. Reszta jest sprawdzona testami na odpowiedziach w formacie
z dokumentacji — pierwsze podłączenie może wymagać poprawki (wtedy wklej treść błędu).

- [ ] Integracje → **Komodo** (klucz tylko do odczytu) → Do zatwierdzenia → „Przypnij dopasowane”.
- [ ] Integracje → **PBS** (token z rolą Audit) — kopie pojawią się na kartach VM.
- [ ] Integracje → **TrueNAS** (klucz API) — pule i snapshoty na karcie TrueNAS.
- [ ] Integracje → **Router TP-Link** (+ drugi jako AP) — klienci sieci w „Do zatwierdzenia”.
- [ ] Powiadomienia → **webhook Discorda** (przyjdzie wiadomość testowa).
- [ ] Powiadomienia → **strażnik**: Uptime Kuma na VPS, monitor *Push*, 60 s, Retries 2, powiadomienie na Discord.
- [ ] **Test kryterium Etapu 3:** wyłącz testową VM z zaznaczonym *Start at boot* → po 1–2 min incydent na pulpicie
      i wiadomość na Discordzie → po włączeniu „Rozwiązane”.
- [ ] Akceptacja Etapu 3.

## 4. Braki względem projektu (Etap 3)

- [ ] **Sprawdzenia z VPS** („działa w LAN, nie działa z internetu”) — jest tylko strażnik; sprawdzenia aplikacji
      z zewnątrz jeszcze nie.
- [ ] **Odświeżanie na żywo (SSE)** — pulpit i tablet odświeżają się co minutę zwykłym przeładowaniem strony.
- [ ] **OpenWrt** — odłożone (POMYSLY.md); router działa przez nieoficjalną bibliotekę TP-Link.
- [ ] Z migracji HLC **nie przeniesione**: sprawdzenia, kanały powiadomień, konta (z założenia — wracają w nowej postaci).

## 5. Rzeczy niesprawdzone albo do decyzji

- [ ] **PDF kopii awaryjnej** — WeasyPrint jest w obrazie Dockera, ale PDF nie był generowany na serwerze
      (sprawdź, czy w `/opt/denali/data/export/` jest `denali-procedury.pdf`).
- [ ] **Synchronizacja kopii awaryjnej na VPS** — skrypt i instrukcja są (`deploy/vps/README.md`), nic nie zostało ustawione.
- [ ] **Kopia poza domem** (restic na VPS) — opisana w README, nieustawiona.
- [ ] **Końce linii w gicie** — dodać `.gitattributes` (`* text=auto eol=lf`), żeby skrypty `.sh` nie psuły się
      po wyciągnięciu repo na Windowsie.
- [ ] Testy lokalnie działają na Pythonie 3.15, obraz na 3.12 — dotąd bez problemów, ale warto kiedyś puścić testy w obrazie.

## 6. Następne etapy

| Etap | Zakres | Kryterium odbioru |
|---|---|---|
| **4. Aktualizacje** | odchudzony agent (apt, „wymagany restart”, montaże NFS z wykrywaniem danych pod montażem), instalacja jednym poleceniem bez `curl \| sudo sh`, jedna lista aktualizacji ze wszystkich źródeł (Proxmox, Komodo, agent, router) | lista zgadza się z tym, co pokazują Proxmox / Komodo / router |
| **5. Akcje** | lista dozwolonych akcji (przez profil), potwierdzenia, audyt; Proxmox (restart, snapshot), Komodo (aktualizacja stacka), agent (`apt upgrade` z lokalnej białej listy), router tylko kopia konfiguracji | każda akcja ma wpis w audycie; akcja spoza listy jest niemożliwa nawet przez API |
