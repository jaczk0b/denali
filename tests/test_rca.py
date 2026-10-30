"""Analiza przyczyny na scenariuszu z makiety: zawieszony TrueNAS (VM 100) i jego skutki."""
from denali.rca import Edge, Graph

ROUTER, SWITCH, PVE, TRUENAS, DOCKER, IMMICH, JELLYFIN, FILEBROWSER, PBS, HA, UPS = range(1, 12)
NAMES = {ROUTER: "router", SWITCH: "switch", PVE: "pve", TRUENAS: "truenas", DOCKER: "docker", IMMICH: "immich",
         JELLYFIN: "jellyfin", FILEBROWSER: "filebrowser", PBS: "pbs", HA: "homeassistant", UPS: "ups"}


def homelab():
    edges = [
        Edge(SWITCH, ROUTER, True, "kabel"), Edge(PVE, SWITCH, True, "kabel"),
        Edge(PVE, UPS, True, "zasilanie"),
        Edge(TRUENAS, PVE), Edge(DOCKER, PVE), Edge(PBS, PVE), Edge(HA, PVE),
        Edge(IMMICH, DOCKER), Edge(JELLYFIN, DOCKER), Edge(FILEBROWSER, DOCKER),
        Edge(IMMICH, TRUENAS, True, "dane"), Edge(JELLYFIN, TRUENAS, True, "dane"),
        Edge(FILEBROWSER, TRUENAS, True, "dane"), Edge(PBS, TRUENAS, True, "dane"),
        Edge(HA, PBS, False, "dane"),            # backup Home Assistanta — miękko
    ]
    return Graph(edges, NAMES)


def states(down=(), unknown=()):
    s = {n: "ok" for n in NAMES}
    s.update({n: "down" for n in down})
    s.update({n: "unknown" for n in unknown})
    return s


def test_zawieszony_truenas_to_jedna_przyczyna_ze_skutkami():
    g = homelab()
    [f] = g.analyze(states(down=[TRUENAS, IMMICH, JELLYFIN, FILEBROWSER, PBS]))
    assert f.cause == TRUENAS
    assert f.effects == [FILEBROWSER, IMMICH, JELLYFIN, PBS]
    assert f.degraded == [HA]                         # bez PBS Home Assistant działa, ale bez backupu
    assert f.upstream_ok == [(PVE, "hosting")]        # host działa, więc problem jest w samej VM


def test_padniety_switch_tlumi_wszystko_za_nim():
    g = homelab()
    behind = [PVE, TRUENAS, DOCKER, IMMICH, JELLYFIN, FILEBROWSER, PBS, HA]
    [f] = g.analyze(states(down=[SWITCH] + behind))
    assert f.cause == SWITCH
    assert sorted(f.effects) == sorted(behind)
    assert f.upstream_ok == [(ROUTER, "kabel")]


def test_najglebsza_przyczyna_a_nie_dowolny_korzen():
    # leży pve i wszystko na nim; switch działa — przyczyną jest pve, nie TrueNAS ani Docker
    g = homelab()
    behind = [TRUENAS, DOCKER, IMMICH, JELLYFIN, FILEBROWSER, PBS, HA]
    [f] = g.analyze(states(down=[PVE] + behind))
    assert f.cause == PVE and len(f.effects) == len(behind)
    assert sorted(f.upstream_ok) == [(SWITCH, "kabel"), (UPS, "zasilanie")]


def test_brak_danych_o_rodzicu_jest_pokazany():
    g = homelab()
    [f] = g.analyze(states(down=[PVE, TRUENAS, DOCKER, IMMICH, JELLYFIN, FILEBROWSER, PBS, HA], unknown=[UPS]))
    assert f.cause == PVE
    assert f.upstream_unknown == [(UPS, "zasilanie")]  # „nie wiadomo, czy UPS działa — sprawdź też jego”


def test_dwie_niezalezne_awarie_to_dwie_przyczyny():
    g = homelab()
    findings = g.analyze(states(down=[TRUENAS, IMMICH, JELLYFIN, FILEBROWSER, PBS, HA]))
    assert [f.cause for f in findings] == [TRUENAS, HA]   # najpierw ta z większą liczbą skutków
    assert findings[1].effects == []


def test_miekka_zaleznosc_nie_jest_przyczyna():
    # PBS leży, Home Assistant też — ale HA zależy od PBS tylko miękko, więc to osobny problem
    g = homelab()
    causes = [f.cause for f in g.analyze(states(down=[PBS, HA]))]
    assert sorted(causes) == [PBS, HA]


def test_wszystko_dziala_brak_przyczyn():
    assert homelab().analyze(states()) == []


def test_skutki_awarii():
    hard, soft = homelab().impact(TRUENAS)
    assert hard == {IMMICH, JELLYFIN, FILEBROWSER, PBS}
    assert soft == {HA}


def test_wykrywanie_petli():
    g = homelab()
    assert g.creates_cycle(PVE, IMMICH)          # pve zależałby od czegoś, co działa na nim
    assert g.creates_cycle(PVE, PVE)
    assert not g.creates_cycle(IMMICH, PBS)


def test_petla_w_danych_nie_gubi_przyczyny():
    g = Graph([Edge(1, 2, True, "usluga"), Edge(2, 1, True, "usluga"), Edge(3, 1)])
    findings = g.analyze({1: "down", 2: "down", 3: "down"})
    assert sorted(f.cause for f in findings) == [1, 2]
    assert all(f.effects == [3] for f in findings)
