# Pomysły na później

Rzeczy, które nie służą bezpośrednio celowi panelu teraz albo czekają na decyzję. Nie implementujemy ich bez osobnej zgody.

## Z HLC (świadomie odłożone)

- **Asystent AI** — diagnoza incydentu na podstawie dowodów; jeśli kiedyś, to tylko do odczytu, bez prawa zapisu i akcji.
- **Widżety pulpitu**: pogoda, kalendarz, wspólna lista zadań.
- **„Zgłoś problem”** dla domowników (z tabletu albo telefonu).
- **Prognoza zapełnienia dysku** — z danych Proxmoxa/TrueNAS, bez własnej bazy metryk.
- **Test odtworzenia backupu** (tymczasowa VM bez sieci) — PBS weryfikuje kopie, ale nie sprawdza, czy maszyna wstaje.
- **Bezpieczny restart hosta Proxmox** z kolejnością startu liczoną z zależności.
- **Diagnoza „z internetu”**: łańcuch DNS (Cloudflare) → VPS → tunel → usługa w LAN.
- **Podsumowanie tygodnia** i raport dostępności.
- **Rejestr IP i portów** z wykrywaniem konfliktów.
- **UPS przez NUT** (alerty, czas na baterii).

## Nowe

- **Widżet Home Assistanta na pulpicie** (np. stan alarmu, temperatura) — tablet i tak będzie miał HA obok.
- **Logowanie przez OIDC** — po ustandaryzowaniu logowania we wszystkich aplikacjach (dostawca już jest). Własne konto Denali zostaje jako awaryjne.
- **Adapter Omada / UniFi** — zależnie od wybranych access pointów.
- **OpenWrt zamiast oryginalnego oprogramowania TP-Linka** (decyzja z 30.10.2026: na razie nie). Tylko Archer AX23 w wersji sprzętowej v1 (AX23 v2 i AX10 nie są obsługiwane). Dałoby: osobne konto tylko do odczytu (rpcd/ubus) zamiast hasła administratora, stabilne API zamiast nieoficjalnej biblioteki tplinkrouterc6u, klientów z DHCP/ARP/Wi-Fi, kopię konfiguracji (`sysupgrade -b`) jako jedyną akcję na routerze. Przed flashowaniem: sprawdzić wersję na naklejce.
- **Panel dla sklepu** jako osobna instancja (lokalizacja w ustawieniach).
- **Kopia zdjęć poza NAS** — wskazówka z makiety: `tank/photos` ma tylko snapshoty na tym samym NAS-ie.
- **Zarządzanie komputerami** i **budowanie infrastruktury z szablonów** (z zadania: „później”).
