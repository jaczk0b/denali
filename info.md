Zakładam, że pytasz o obie strony: konfigurację sieci i to, co musi umieć Denali. Niczego nie zmieniałem. Same VLAN-y robisz na routerze, a panel prawie nie zależy od podsieci, więc 10.42.x będzie w nim działać. Brakuje mu tylko adaptera OpenWrt, którego dziś nie ma.

Plan sieci
Najprościej, gdy numer VLAN-u równa się trzeciemu oktetowi:

VLAN	Podsieć	Co w niej	Dostęp
10	10.42.10.0/24	infrastruktura: Proxmox, TrueNAS, Denali, NPM, router, AP	wszędzie
20	10.42.20.0/24	komputery, telefony	internet i wybrane usługi z 10
30	10.42.30.0/24	IoT, Home Assistant	tylko internet
40	10.42.40.0/24	goście	tylko internet
Trzymaj w jednym VLAN-ie serwery, które dużo do siebie wysyłają, np. Proxmox i TrueNAS. Inaczej NFS i kopie pójdą przez CPU AX23, co mocno ograniczy przepustowość.

Konfiguracja OpenWrt
Na każdy VLAN potrzebujesz trzech rzeczy: bridge-vlan z portami, interfejsu z adresem i DHCP oraz strefy firewalla.


config bridge-vlan
    option device 'br-lan'
    option vlan '20'
    list ports 'lan1:t'      # trunk, np. do Proxmoxa albo switcha
    list ports 'lan3:u*'     # port dostępowy

config interface 'dom'
    option device 'br-lan.20'
    option proto 'static'
    option ipaddr '10.42.20.1'
    option netmask '255.255.255.0'
Nazwy portów sprawdź na swoim routerze. Każdą sieć Wi-Fi przypisujesz do interfejsu przez option network 'dom'.

Pozostałe urządzenia:

Proxmox: ustaw vmbr0 jako VLAN-aware, a na karcie sieciowej każdej VM i LXC podaj tag.
AX10: o ile wiem, na fabrycznym oprogramowaniu nie przypisze sieci Wi-Fi do VLAN-ów, więc wszyscy jego klienci trafią do jednej sieci. Goście i IoT muszą być na AX23 albo na innym AP.
Switch: do rozdzielania VLAN-ów na portach potrzebny jest zarządzalny. Niezarządzalny tego nie zrobi, a w repo nie ma informacji, jaki masz.
Co zmienić przy Denali
Bez zmian w kodzie: widok Adresy sam wylicza sieci /24 z adresów (infra_views.py:298), więc 10.42.x pojawią się same. Maska /24 jest tam jednak na sztywno, więc inne maski wyświetlą się błędnie.
Dostęp do panelu: lista dostępu w NPM ma 192.168.0.0/16 (README.md:126). Dopisz 10.42.0.0/16 przed migracją, bo inaczej odetniesz sobie panel.
Tailscale: subnet router musi ogłaszać nowe podsieci.
DENALI_TRUSTED_PROXIES: zaktualizuj, jeśli NPM zmieni adres (.env.example:11).
Integracje: popraw adresy Proxmoxa, PBS, TrueNAS i Komodo, a także udziały NFS.
Firewall: VLAN 10 musi mieć dostęp do pozostałych sieci, żeby sprawdzenia Denali widziały urządzenia w innych VLAN-ach.
Stare adresy: adres w dokumentacji zostaje po zmianie, więc ręcznie wpisane 192.168.1.x trzeba poprawić. Inaczej widok Adresy pokaże starą sieć, a sprawdzenia mogą zgłaszać fałszywe awarie.
Nowy kod to adapter openwrt przez rpcd/ubus z kontem tylko do odczytu. Dawałby klientów DHCP ze wszystkich VLAN-ów, a przy okazji listę sieci, którą widok Adresy pokazałby jako np. „VLAN 30 · IoT” z prawdziwą maską. VLAN-ów nie trzeba by wtedy wpisywać ręcznie w panelu, co pasuje do zasady minimum ustawień. Na razie to tylko plan: infra_views.py przewiduje źródło openwrt, ale denali/adapters/ go nie ma.

Kolejność migracji
Sprawdź wersję AX23 na naklejce, bo OpenWrt obsługuje tylko v1, a PROJEKT.md ma to wciąż do sprawdzenia.
Zrób kopię konfiguracji i wgraj OpenWrt, zostawiając na razie 192.168.1.x.
Dodaj VLAN-y obok starej sieci, zostawiając jeden port bez tagu jako awaryjny.
Przenieś najpierw NPM i Denali, potem resztę infrastruktury, a na końcu klientów.
Włącz blokady firewalla dopiero, gdy wszystko działa, a potem usuń 192.168.1.x.
Jeśli kiedyś użyjesz k3s, pamiętaj, że jego domyślna sieć podów to właśnie 10.42.0.0/16. W repo nie ma śladu k3s, ale wtedy ta pula by kolidowała.

Masz zarządzalny switch i ile VLAN-ów ma iść przez Wi-Fi? Od tego zależy, czy AX10 zostanie, czy trzeba go wymienić. Mogę też od razu napisać adapter openwrt, ale testowo sprawdzę go dopiero na fałszywych odpowiedziach, dopóki router nie będzie działał na OpenWrt.