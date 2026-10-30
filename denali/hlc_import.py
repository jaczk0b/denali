"""Migracja z HLC: czyta kopię HLC (ZIP z `python -m hlc backup`, katalog albo sam hlc.db) i zapisuje do Denali.

Zasady:
- baza HLC jest otwierana tylko do odczytu; stare HLC nie jest zmieniane,
- domyślnie próba na sucho: wszystko wykonuje się naprawdę w transakcji, która na końcu jest cofana
  (raport pokazuje dokładnie to, co się stanie); zapis dopiero z apply=True,
- hasła odszyfrowane starym kluczem trafiają do skarbca Denali zaszyfrowane nowym — nigdy do raportu,
- drugi przebieg niczego nie dubluje (lista przeniesionych rzeczy w ustawieniu „hlc_import.done”).
"""
import json
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from . import auth, docs, files, inventory as inv, vault
from .db import kv_get, kv_set
from .models import incident, incident_device, incident_event

KIND_MAP = {"kasa": "inne", "terminal": "inne", "fiskalna": "inne", "skaner": "inne"}
DEP_MAP = {"hosting": "usluga"}
PASSWORDISH = re.compile(r"\b(has[łl][oa]?|password|passwd|pass|pin|token|secret)\s*[:=]", re.I)
DONE_KEY = "hlc_import.done"
MAX_DOWNLOAD = 2 * 1024 ** 3


class MigrationError(Exception):
    pass


class Report:
    def __init__(self):
        self.counts = {}
        self.warnings = []
        self.sites = []

    def add(self, what, n=1):
        self.counts[what] = self.counts.get(what, 0) + n

    def warn(self, text):
        self.warnings.append(text)

    def text(self, applied):
        out = ["ZAPISANO w Denali:" if applied else "PRÓBA NA SUCHO — nic nie zostało zapisane. Tak wyglądałby import:"]
        out.append("Lokalizacje: " + ", ".join(self.sites))
        for k, v in self.counts.items():
            out.append(f"  {k}: {v}")
        if self.warnings:
            out.append(f"\nDo przejrzenia ręcznie ({len(self.warnings)}):")
            out += [f"  - {w}" for w in self.warnings]
        if not applied:
            out.append("\nJeśli wszystko się zgadza, uruchom to samo polecenie z --wykonaj.")
        return "\n".join(out)


# ───────────────────────── źródło ─────────────────────────
def _safe_extract(zf, dest):
    db_found = False
    for m in zf.infolist():
        name = m.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p]
        if not parts or ".." in parts or name.startswith("/") or m.is_dir():
            continue
        if parts[0] == "instance":
            parts = parts[1:]
        if parts == ["hlc.db"] or (len(parts) >= 2 and parts[0] == "uploads"):
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            db_found |= parts == ["hlc.db"]
    if not db_found:
        raise MigrationError("W archiwum nie ma instance/hlc.db — to na pewno kopia z `python -m hlc backup`?")


def prepare_source(src, workdir):
    """Zwraca (ścieżka do hlc.db, katalog uploads albo None)."""
    workdir = Path(workdir)
    if str(src).startswith(("http://", "https://")):
        target = workdir / "hlc-kopia.zip"
        with urllib.request.urlopen(src, timeout=60) as resp, open(target, "wb") as out:   # noqa: S310 — adres podaje admin
            copied = 0
            while chunk := resp.read(1024 * 1024):
                copied += len(chunk)
                if copied > MAX_DOWNLOAD:
                    raise MigrationError("Plik jest większy niż 2 GB — to na pewno kopia HLC?")
                out.write(chunk)
        src = target
    p = Path(src)
    if not p.exists():
        raise MigrationError(f"Nie ma {p}")
    if p.is_file() and zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            _safe_extract(zf, workdir / "hlc")
        p = workdir / "hlc"
    if p.is_dir():
        for base in (p, p / "instance"):
            if (base / "hlc.db").exists():
                up = base / "uploads"
                return base / "hlc.db", up if up.is_dir() else None
        raise MigrationError(f"W {p} nie ma hlc.db")
    return p, (p.parent / "uploads") if (p.parent / "uploads").is_dir() else None


def _open_ro(db_path):
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("SELECT 1 FROM devices LIMIT 1")
    except sqlite3.DatabaseError:
        con.close()
        raise MigrationError("To nie jest baza HLC (brak tabeli devices).") from None
    return con


def _rows(con, table, where="", params=()):
    try:
        return [dict(r) for r in con.execute(f'SELECT * FROM "{table}" {where}', params)]
    except sqlite3.OperationalError:
        return []          # tabela z nowszej wersji HLC, której ta baza nie ma


def _json(v, default):
    if v in (None, ""):
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _ts(v):
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat, lambda x: datetime.strptime(x[:10], "%Y-%m-%d")):
        try:
            return parse(s).timestamp()
        except ValueError:
            continue
    return None


# ───────────────────────── import ─────────────────────────
class _Run:
    def __init__(self, conn, settings, box, con, uploads, fernet, report, apply, user):
        self.conn, self.settings, self.box, self.con = conn, settings, box, con
        self.uploads, self.fernet, self.r, self.apply, self.user = uploads, fernet, report, apply, user
        self.done = dict(kv_get(conn, DONE_KEY, {}) or {})
        self.dev = {k[4:]: v for k, v in self.done.items() if k.startswith("dev:")}

    def seen(self, key):
        return key in self.done

    def mark(self, key, value=True):
        self.done[key] = value

    def dec(self, token):
        if not token:
            return ""
        if not self.fernet:
            raise ValueError("brak klucza")
        return self.fernet.decrypt(token.encode()).decode()

    def device_exists(self, did):
        return did is not None and inv.get_device(self.conn, did) is not None

    # urządzenia
    def devices(self, rows):
        for d in rows:
            key = f"dev:{d['id']}"
            if self.seen(key) and self.device_exists(self.done[key]):
                continue
            if not int(d.get("approved", 1) or 0):
                self.r.add("urządzenia pominięte (niezatwierdzone w HLC)")
                continue
            kind = d.get("kind") or "inne"
            if d.get("pve_type") == "lxc":
                kind = "lxc"
            elif d.get("pve_type") == "qemu" and kind not in ("vm", "nas"):
                kind = "vm"
            kind = KIND_MAP.get(kind, kind)
            if kind not in inv.KINDS:
                self.r.warn(f"{d['name']}: nieznany typ „{d.get('kind')}” — ustawiony „inne”")
                kind = "inne"
            crit = int(d.get("crit") or 1)
            notes = self._notes(d)
            did = inv.create_device(self.conn, (d.get("name") or "bez nazwy")[:200], kind, role=(d.get("purpose") or "")[:300],
                                    criticality=min(3, max(1, 4 - crit)), place=(d.get("place") or "")[:200],
                                    serial=(d.get("serial") or "")[:100], notes_md=notes)
            self.mark(key, did)
            self.dev[d["id"]] = did
            self.r.add("urządzenia")
            ip = (d.get("ip") or "").strip()
            if ip:
                self._address(did, d["name"], ip, primary=True)
            if PASSWORDISH.search(notes) or PASSWORDISH.search(d.get("runbook") or ""):
                self.r.warn(f"{d['name']}: notatki albo runbook wyglądają, jakby zawierały hasło — przenieś je do skarbca")

    def _notes(self, d):
        parts = [d.get("notes") or ""]
        for field, title in (("access", "Dostęp"), ("backup", "Kopie zapasowe"), ("restore_tested", "Test odtworzenia"),
                             ("vault", "Hasła (opis z HLC)"), ("checked", "Sprawdzone")):
            if (d.get(field) or "").strip():
                parts.append(f"## {title}\n\n{d[field].strip()}")
        for field, title in (("ports", "Porty"), ("expiry", "Terminy"), ("panel", "Panele"), ("cost", "Koszty")):
            val = _json(d.get(field), None)
            if val:
                items = val if isinstance(val, list) else [val]
                lines = [", ".join(f"{k}: {v}" for k, v in i.items() if v not in ("", None)) if isinstance(i, dict) else str(i)
                         for i in items]
                parts.append(f"## {title}\n\n" + "\n".join(f"- {x}" for x in lines if x))
        return "\n\n".join(p for p in parts if p.strip())

    def _address(self, did, name, value, primary=False, kind=None):
        value = value.strip()
        if ":" in value and kind is None and value.count(":") == 1:
            value = value.split(":")[0]           # „192.168.1.5:8080” → adres bez portu
        kinds = [kind] if kind else ["ip", "hostname"]
        for k in kinds:
            try:
                inv.add_address(self.conn, did, k, value, primary=primary and k == "ip")
                self.r.add(f"adresy ({k})")
                return
            except inv.InventoryError:
                continue
        self.r.warn(f"{name}: adres „{value}” nie jest poprawnym IP ani nazwą hosta — pominięty")

    def parents(self, rows):
        for d in rows:
            parent_old = d.get("parent_id")
            child, parent = self.dev.get(d["id"]), self.dev.get(parent_old)
            if not child or not parent or self.seen(f"parent:{d['id']}"):
                continue
            self.mark(f"parent:{d['id']}")
            try:
                inv.update_device(self.conn, child, parent_id=parent)
            except inv.InventoryError:
                try:
                    inv.add_dependency(self.conn, child, parent, "usluga", hard=True, note="z HLC: działa na")
                    self.r.warn(f"{d['name']}: „działa na” nie pasuje do typów w Denali — zapisane jako twarda zależność")
                except inv.InventoryError as e:
                    self.r.warn(f"{d['name']}: nie przeniesiono „działa na” ({e})")

    def deps(self, rows):
        for x in rows:
            key = f"dep:{x['id']}"
            a, b = self.dev.get(x["device_id"]), self.dev.get(x["parent_id"])
            if self.seen(key) or not a or not b:
                continue
            self.mark(key)
            typ = DEP_MAP.get(x.get("type") or "siec", x.get("type") or "siec")
            if typ not in inv.DEP_TYPES:
                typ = "usluga"
            try:
                inv.add_dependency(self.conn, a, b, typ, hard=bool(int(x.get("hard", 1) or 0)), note=x.get("note") or "")
                self.r.add("zależności")
            except inv.InventoryError as e:
                self.r.warn(f"zależność {x['device_id']} → {x['parent_id']}: {e}")

    def links(self, rows):
        for x in rows:
            key = f"link:{x['id']}"
            a, b = self.dev.get(x["a"]), self.dev.get(x["b"])
            if self.seen(key) or not a or not b:
                continue
            self.mark(key)
            try:
                inv.add_cable(self.conn, a, b, x.get("ap") or "", x.get("bp") or "")
                self.r.add("kable")
            except inv.InventoryError:
                try:     # w HLC strona kabla nie miała znaczenia — próbujemy odwrotnie
                    inv.add_cable(self.conn, b, a, x.get("bp") or "", x.get("ap") or "")
                    self.r.add("kable")
                except inv.InventoryError as e:
                    self.r.warn(f"kabel {x['a']} – {x['b']}: {e}")

    def runbooks(self, rows):
        for d in rows:
            body = (d.get("runbook") or "").strip()
            did = self.dev.get(d["id"])
            if not body or not did or self.seen(f"rb:{d['id']}"):
                continue
            first = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
            title = first if body.lstrip().startswith("#") and first else f"{d['name']} — runbook"
            rid = docs.save_runbook(self.conn, title[:200], body, [did], user=self.user)
            self.mark(f"rb:{d['id']}", rid)
            self.r.add("runbooki")

    def issues(self, rows, names):
        for x in rows:
            key = f"issue:{x['id']}"
            did = self.dev.get(x["device_id"])
            if self.seen(key) or not did:
                continue
            started = _ts(x.get("date")) or _ts(x.get("created")) or time.time()
            closed = x.get("status") not in ("open", "otwarty", "")
            ended = (_ts(x.get("closed")) or started) if closed else None
            iid = self.conn.execute(incident.insert().values(
                title=(x.get("title") or f"Problem: {names.get(x['device_id'], '?')}")[:200], root_device_id=did,
                status="zamkniety" if closed else "trwa", severity="ostrzezenie", source="hlc", started_at=started,
                ended_at=ended, cause_md=x.get("desc") or "", fix_md=x.get("fix") or "")).inserted_primary_key[0]
            self.conn.execute(incident_device.insert().values(incident_id=iid, device_id=did, role="przyczyna"))
            self.conn.execute(incident_event.insert().values(incident_id=iid, ts=started, kind="otwarty",
                                                             text="Przeniesione z HLC.", user="migracja"))
            self.mark(key, iid)
            self.r.add("problemy (log)")

    def files(self, rows):
        for f in rows:
            key = f"file:{f['id']}"
            did = self.dev.get(f["device_id"])
            if self.seen(key) or not did:
                continue
            stored = Path(f.get("stored") or "").name
            src = self.uploads / stored if self.uploads and stored else None
            if not src or not src.is_file():
                self.r.warn(f"plik „{f.get('name')}”: brak w uploads/ — pominięty")
                continue
            self.mark(key)
            if self.apply:
                files.store(self.conn, self.settings.data_dir, did, f.get("name") or stored, src.read_bytes(),
                            f.get("mime") or "", user=self.user)
            self.r.add("pliki")

    def secrets(self, rows, names):
        for s in rows:
            key = f"secret:{s['id']}"
            did = self.dev.get(s["device_id"])
            if self.seen(key) or not did:
                continue
            label = (s.get("label") or "hasło").strip() or "hasło"
            if s.get("scheme", "fernet") != "fernet":
                self.r.warn(f"{names.get(s['device_id'], '?')} / {label}: hasło w starym skarbcu z hasłem lokalizacji — "
                            "odblokuj je w HLC i powtórz migrację")
                continue
            try:
                plain = self.dec(s.get("blob_enc"))
            except (InvalidToken, ValueError):
                self.r.add("hasła NIEodszyfrowane (zły albo brak klucza)")
                continue
            if not plain:
                continue
            vault.add(self.conn, self.box, label[:200], plain, role="admin", user=self.user, device_id=did,
                      note=(s.get("desc") or "")[:500])
            self.mark(key)
            self.r.add("hasła do skarbca")

    def site_docs(self, site):
        sid = site["id"]
        cid = None
        call = (site.get("call") or "").strip()
        if call and not self.seen(f"contact:{sid}"):
            phone = re.search(r"\+?\d[\d \-()]{5,}\d", call)
            name = (call.replace(phone.group(0), "") if phone else call).strip(" -–:,;") or f"Kontakt alarmowy ({site['name']})"
            try:
                cid = docs.save_contact(self.conn, name[:200], phone.group(0) if phone else "", note=f"z HLC: {site['name']}")
            except docs.DocsError:
                cid = docs.save_contact(self.conn, name[:200], "", note=f"z HLC: {call}"[:300])
            self.mark(f"contact:{sid}", cid)
            self.r.add("kontakty")
        elif call:
            cid = self.done.get(f"contact:{sid}")
        for i, p in enumerate(_json(site.get("panic"), [])):
            key = f"proc:{sid}:{i}"
            if self.seen(key) or not isinstance(p, dict):
                continue
            steps = []
            for st in p.get("steps") or []:
                st = str(st).strip()
                if st:
                    steps.append((st[:200], st if len(st) > 200 else ""))
            if not steps:
                steps = [("Brak kroków w HLC — uzupełnij", "")]
            pid = docs.save_procedure(self.conn, (p.get("t") or f"Procedura {i + 1}")[:200], steps,
                                      contact_ids=[cid] if cid else [], user=self.user)
            self.mark(key, pid)
            self.r.add("procedury")
        for i, w in enumerate(_json(site.get("wifi"), [])):
            key = f"wifi:{sid}:{i}"
            if self.seen(key) or not isinstance(w, dict):
                continue
            ssid = str(w.get("ssid") or w.get("name") or f"sieć {i + 1}")
            secret, notes = "", []
            for k, v in w.items():
                if isinstance(v, dict) and "hlc_enc" in v:
                    try:
                        secret = self.dec(v["hlc_enc"])
                    except (InvalidToken, ValueError):
                        self.r.add("hasła NIEodszyfrowane (zły albo brak klucza)")
                elif k in ("pass", "password", "haslo") and isinstance(v, str) and v:
                    secret = v
                elif k not in ("ssid", "name") and v not in ("", None):
                    notes.append(f"{k}: {v}")
            if not secret:
                continue
            vault.add(self.conn, self.box, f"Wi-Fi: {ssid}"[:200], secret, role="admin", user=self.user,
                      note="; ".join(notes)[:500])
            self.mark(key)
            self.r.add("hasła do skarbca")

    def apps(self, rows):
        for a in rows:
            key = f"app:{a['id']}"
            url = (a.get("url") or "").strip()
            if self.seen(key) or not url.startswith(("http://", "https://")):
                continue
            group = "zarzadzanie" if a.get("audience") == "admin" else "codzien"
            icon = (a.get("icon") or "").strip()
            icon = icon if 0 < len(icon) <= 3 else ""
            did = self.dev.get(a.get("device_id") or "")
            if did:
                cur = inv.get_device(self.conn, did)
                fields = {"pulpit_group": group}
                if not cur["app_url"]:
                    fields["app_url"] = url[:500]
                if icon and not cur["icon"]:
                    fields["icon"] = icon
                inv.update_device(self.conn, did, **fields)
            else:
                inv.create_device(self.conn, (a.get("name") or url)[:200], "usluga", app_url=url[:500], pulpit_group=group,
                                  icon=icon, role=(a.get("description") or "")[:300])
                self.r.add("urządzenia")
            self.mark(key)
            self.r.add("ikony aplikacji na pulpicie")

    def addresses(self, net_hosts, dns_names):
        for h in net_hosts:
            did = self.dev.get(h.get("device_id") or "")
            key = f"mac:{h.get('mac')}"
            if not did or self.seen(key):
                continue
            self.mark(key)
            if h.get("mac"):
                self._address(did, h.get("hostname") or h["mac"], h["mac"], kind="mac")
        for n in dns_names:
            did = self.dev.get(n.get("device_id") or "")
            key = f"dns:{n.get('name')}:{n.get('device_id')}"
            if not did or self.seen(key) or not n.get("name"):
                continue
            self.mark(key)
            self._address(did, n["name"], n["name"], kind="hostname")


def migrate(engine, settings, box, source, old_key=None, sites=None, apply=False, user="konsola serwera"):
    """Cały import w jednej transakcji: zatwierdzonej przy apply=True, cofniętej przy próbie na sucho."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            report = run(conn, settings, box, source, old_key, sites, apply, user)
        except BaseException:
            trans.rollback()
            raise
        if apply:
            trans.commit()
        else:
            trans.rollback()
    return report


def run(conn, settings, box, source, old_key=None, sites=None, apply=False, user="konsola serwera"):
    """Import z HLC w transakcji wywołującego (zatwierdza albo cofa ją wywołujący — patrz migrate)."""
    report = Report()
    fernet = None
    if old_key:
        try:
            fernet = Fernet(old_key.strip())
        except ValueError:
            raise MigrationError("HLC_FERNET_KEY ma zły format — skopiuj całą wartość z /opt/hlc/.env") from None
    with tempfile.TemporaryDirectory() as tmp:
        db_path, uploads = prepare_source(source, tmp)
        con = _open_ro(db_path)
        try:
            all_sites = _rows(con, "sites", "ORDER BY sort, name")
            if sites:
                wanted = {s.lower() for s in sites}
                chosen = [s for s in all_sites if s["name"].lower() in wanted]
                missing = wanted - {s["name"].lower() for s in chosen}
                if missing:
                    raise MigrationError(f"W HLC nie ma lokalizacji: {', '.join(sorted(missing))}. "
                                       f"Są: {', '.join(s['name'] for s in all_sites)}")
            else:
                chosen = all_sites
            report.sites = [s["name"] for s in chosen]
            site_ids = [s["id"] for s in chosen]
            q = f"WHERE site_id IN ({','.join('?' * len(site_ids))})" if site_ids else "WHERE 0"
            devs = _rows(con, "devices", q, site_ids)
            dev_ids = {d["id"] for d in devs}
            names = {d["id"]: d["name"] for d in devs}

            def for_devs(table, col="device_id"):
                return [r for r in _rows(con, table) if r.get(col) in dev_ids]

            r = _Run(conn, settings, box, con, uploads, fernet, report, apply, user)
            r.devices(devs)
            r.parents(devs)
            r.deps(for_devs("deps"))
            r.links([x for x in _rows(con, "links") if x.get("a") in dev_ids and x.get("b") in dev_ids])
            r.runbooks(devs)
            r.issues(for_devs("issues"), names)
            r.files(for_devs("files"))
            secrets = for_devs("secrets")
            if secrets and not fernet:
                report.warn(f"Bez HLC_FERNET_KEY {len(secrets)} haseł nie zostanie przeniesionych — podaj klucz")
            r.secrets(secrets, names)
            for s in chosen:
                r.site_docs(s)
            r.apps([a for a in _rows(con, "apps") if not a.get("device_id") or a.get("device_id") in dev_ids])
            r.addresses(for_devs("net_hosts"), for_devs("dns_names"))
            not_moved = {"sprawdzenia (dojdą z integracjami w Etapie 3)": len(_rows(con, "checks")),
                         "kanały powiadomień (Discord ustawisz w Etapie 3)": len(_rows(con, "channels")),
                         "konta użytkowników (załóż w Denali: create-user)": len(_rows(con, "users"))}
            for k, v in not_moved.items():
                if v:
                    report.add(f"nie przenoszone — {k}", v)
            if apply:
                kv_set(conn, DONE_KEY, r.done)
                auth.audit(conn, "hlc_import", user=user, params={"lokalizacje": report.sites, **report.counts})
        finally:
            con.close()
    return report
