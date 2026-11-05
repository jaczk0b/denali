# Pomysły na później

Rzeczy, które nie służą bezpośrednio celowi panelu teraz albo czekają na decyzję. Nie implementujemy ich bez osobnej zgody.

## Z HLC (świadomie odłożone)

- **Asystent AI** — diagnoza incydentu na podstawie dowodów; jeśli kiedyś, to tylko do odczytu, bez prawa zapisu i akcji.
- **Widżety pulpitu**: pogoda, kalendarz, wspólna lista zadań.
- **„Zgłoś problem”** dla domowników (z tabletu albo telefonu).
- **Test odtworzenia backupu** (tymczasowa VM bez sieci) — PBS weryfikuje kopie, ale nie sprawdza, czy maszyna wstaje.
- **Restart hosta z panelu**: panel liczy kolejność i zakłada przerwę, ale sam restart robisz w Proxmoxie (wymagałby `Sys.PowerMgmt`).
- **Kopie konfiguracji z maszyn przez agenta** (git + diff): zostało tylko OpenWrt i ostatnie wersje przeniesione z HLC.
- **Proxmox HA i replikacja**: kworum jest, reszta ma sens dopiero z pve2.
- **Release notes przy aktualizacjach obrazów**: Komodo nie podaje numeru nowej wersji.
- **Diagnoza „z internetu”**: łańcuch DNS (Cloudflare) → VPS → tunel → usługa w LAN.
- **UPS przez NUT** (alerty, czas na baterii).

## Nowe

- **Widżet Home Assistanta na pulpicie** (np. stan alarmu, temperatura) — tablet i tak będzie miał HA obok.
- **Adapter Omada / UniFi** — zależnie od wybranych access pointów.
- **OpenWrt zamiast oryginalnego oprogramowania TP-Linka** (decyzja z 30.10.2026: na razie nie; integracja OpenWrt jest w panelu od 4.11.2026 — czeka tylko flashowanie). Tylko Archer AX23 w wersji sprzętowej v1 (AX23 v2 i AX10 nie są obsługiwane). Dałoby: osobne konto tylko do odczytu (rpcd/ubus) zamiast hasła administratora, stabilne API zamiast nieoficjalnej biblioteki tplinkrouterc6u, klientów z DHCP/ARP/Wi-Fi, kopię konfiguracji (`sysupgrade -b`) jako jedyną akcję na routerze. Przed flashowaniem: sprawdzić wersję na naklejce.
- **Panel dla sklepu** jako osobna instancja (lokalizacja w ustawieniach).
- **Kopia zdjęć poza NAS** — wskazówka z makiety: `tank/photos` ma tylko snapshoty na tym samym NAS-ie.
- **Zarządzanie komputerami** i **budowanie infrastruktury z szablonów** (z zadania: „później”).
