# Do zrobienia

Stan na 31.10.2026. Etap 2 (rdzeń) gotowy. Etap 3 (status): integracje i test kryterium (wyłączona VM → incydent
→ Discord → „Rozwiązane”) działają na serwerach; czeka na wdrożenie ostatnich zmian i akceptację. Odłożone pomysły są w [POMYSLY.md](POMYSLY.md), decyzje w [PROJEKT.md](PROJEKT.md).

---

## 1. Po Twojej stronie — teraz

- [ ] `git push origin main`, potem *Deploy* stacka `denali` w Komodo (nowe: odświeżanie na żywo, zależność klientów
      od routera/AP, sprawdzenia z internetu).
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
- [ ] Razem z HLC **usuń stare agenty HLC** z maszyn (usługa systemd jako root, konfiguracja z tokenem
      w `/etc/hlc-agent/`): `systemctl list-units | grep -i hlc` → `sudo systemctl disable --now <nazwa>` →
      `systemctl cat <nazwa>` (gdzie leży program) → usuń program i `/etc/hlc-agent`. Denali ich nie używa;
      nowy, odchudzony agent przyjdzie w Etapie 4.
- [ ] Wyjaśnij, czym było „627379008” (jeśli to hasło routera — wpisz je tylko w formularzu Denali).

## 3. Etap 3 — sprawdzenie na prawdziwych serwerach

- [x] Integracje: Proxmox, Komodo, PBS, TrueNAS, router TP-Link; powiadomienia na Discord; strażnik (potwierdzone 31.10).
- [x] **Test kryterium Etapu 3:** wyłączona testowa VM → incydent → Discord → „Rozwiązane” (potwierdzone 31.10).
- [ ] Po wdrożeniu: pulpit na tablecie i w przeglądarce zmienia się **sam**, bez przeładowania strony.
- [ ] Po wdrożeniu: na karcie urządzenia w Wi-Fi „zależy od <AP> · sieć · twarda · wykryta, aktualna” (do 5 min po
      odczycie routera). Sprawdź, czy router pokazuje klientów AP jako swoich (wtedy mają też miękką zależność od routera).
- [ ] Po wdrożeniu: na karcie aplikacji wystawionej przez Pangolina blok **„Z internetu (przez publiczny DNS)”**
      z „HTTP 200/302”. Działa tylko, gdy *adres aplikacji* na karcie to publiczna nazwa (np. `https://immich.serwispowers.com`);
      przy adresie z IP w LAN sprawdzenia z internetu nie ma.
- [ ] Akceptacja Etapu 3.

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

## 6. Następne etapy

| Etap | Zakres | Kryterium odbioru |
|---|---|---|
| **4. Aktualizacje** | odchudzony agent (apt, „wymagany restart”, montaże NFS z wykrywaniem danych pod montażem), instalacja jednym poleceniem bez `curl \| sudo sh`, jedna lista aktualizacji ze wszystkich źródeł (Proxmox, Komodo, agent, router) | lista zgadza się z tym, co pokazują Proxmox / Komodo / router |
| **5. Akcje** | lista dozwolonych akcji (przez profil), potwierdzenia, audyt; Proxmox (restart, snapshot), Komodo (aktualizacja stacka), agent (`apt upgrade` z lokalnej białej listy), router tylko kopia konfiguracji | każda akcja ma wpis w audycie; akcja spoza listy jest niemożliwa nawet przez API |
