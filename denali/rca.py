"""Graf zależności i analiza przyczyny awarii. Czysta logika — bez bazy, bez sieci.

Krawędź „dziecko zależy od rodzica” pochodzi z trzech źródeł:
  hosting  — urządzenie działa na rodzicu (VM na hoście, stack na VM),
  kabel    — urządzenie wpięte kablem w rodzica (strona B kabla zależy od strony A),
  zależność wpisana albo wykryta (zasilanie, sieć, dane, usługa) — twarda albo miękka.

Twarda: bez rodzica dziecko nie działa. Miękka: działa gorzej (np. bez backupu).
"""
from dataclasses import dataclass, field

DOWN = "down"


@dataclass(frozen=True)
class Edge:
    child: int
    parent: int
    hard: bool = True
    via: str = "hosting"          # hosting | kabel | zasilanie | siec | dane | usluga


@dataclass
class Finding:
    """Jedna przyczyna i to, co z niej wynika."""
    cause: int
    effects: list = field(default_factory=list)       # leżą, bo leży przyczyna (bez osobnych powiadomień)
    degraded: list = field(default_factory=list)      # działają gorzej (miękka zależność od przyczyny lub skutku)
    upstream_ok: list = field(default_factory=list)   # [(rodzic, via)] — działają, więc to nie one
    upstream_unknown: list = field(default_factory=list)  # [(rodzic, via)] — brak danych, sprawdź też je


class Graph:
    def __init__(self, edges, names=None):
        self.names = dict(names or {})
        self.up, self.down = {}, {}
        for e in edges:
            if e.child == e.parent:
                continue
            self.up.setdefault(e.child, []).append(e)
            self.down.setdefault(e.parent, []).append(e)

    def name(self, node):
        return self.names.get(node, str(node))

    def ancestors(self, node, hard_only=True):
        """Wszystko, od czego node zależy, przechodnio. Odporne na pętle."""
        seen, stack = set(), [node]
        while stack:
            for e in self.up.get(stack.pop(), []):
                if (e.hard or not hard_only) and e.parent not in seen:
                    seen.add(e.parent)
                    stack.append(e.parent)
        return seen

    def creates_cycle(self, child, parent):
        """Czy dodanie krawędzi child → parent zrobi pętlę."""
        return child == parent or child in self.ancestors(parent, hard_only=False)

    def impact(self, root):
        """Co stanie (twarde, przechodnio) i co będzie działać gorzej (miękkie, bez dalszej propagacji), gdy padnie root."""
        hard, stack = set(), [root]
        while stack:
            for e in self.down.get(stack.pop(), []):
                if e.hard and e.child != root and e.child not in hard:
                    hard.add(e.child)
                    stack.append(e.child)
        soft = {e.child for n in hard | {root} for e in self.down.get(n, []) if not e.hard}
        return hard, soft - hard - {root}

    def analyze(self, states):
        """states: {urządzenie: ok | warn | down | unknown}. Brak wpisu = unknown.

        Przyczyna to urządzenie najgłębsze: leży, a nic z tego, od czego twardo zależy, nie leży.
        (Wyjątek: pętla leżących urządzeń — wtedy każde z pętli jest przyczyną, żeby nic nie zginęło.)
        """
        down = {n for n, s in states.items() if s == DOWN}
        down_anc = {n: self.ancestors(n) & down for n in down}
        causes = sorted(n for n in down if all(n in down_anc[a] for a in down_anc[n]))
        findings = []
        for c in causes:
            f = Finding(cause=c)
            f.effects = self._sorted(n for n in down if n not in causes and c in down_anc[n])
            hard, soft = self.impact(c)
            f.degraded = self._sorted(n for n in soft if n not in down)
            for e in self.up.get(c, []):
                state = states.get(e.parent, "unknown")
                if state in ("ok", "warn"):
                    f.upstream_ok.append((e.parent, e.via))
                elif state != DOWN:
                    f.upstream_unknown.append((e.parent, e.via))
            findings.append(f)
        findings.sort(key=lambda f: (-len(f.effects), self.name(f.cause)))
        return findings

    def _sorted(self, nodes):
        return sorted(nodes, key=self.name)
