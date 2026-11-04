"""Akcje (Etap 5): restart VM, aktualizacja stacka, apt upgrade przez agenta…

Akcja dochodzi do skutku tylko wtedy, gdy przejdzie wszystkie zamki — sprawdzane tutaj, po stronie serwera, przy
każdym zleceniu (formularz i bezpośredni POST przechodzą tę samą drogę):
  1. akcje włączone w ogóle (wyłącznik w „Akcjach” — jednym kliknięciem wyłącza wszystko),
  2. klucz akcji na liście „Dozwolone akcje” urządzenia (z profilu albo wyjątek na urządzeniu),
  3. integracja ma prawo do akcji: wybrany zakres jednego tokenu (Proxmox) albo osobny token do akcji (Komodo);
     agent: akcja na lokalnej białej liście maszyny — sprawdza ją sam agent,
  4. akcja pasuje do obiektu i jego stanu, a na obiekcie nie trwa inna,
  5. akcja niebezpieczna: świeże potwierdzenie hasłem i kodem 2FA (5 minut).
Każde zlecenie, odmowa i wynik trafiają do audytu, a przy trwającym incydencie urządzenia — do jego przebiegu.

Przebieg (action_run): zlecona → trwa → ok | blad. „czeka” = najpierw inna akcja (snapshot przed aktualizacją,
aktualizacja przed restartem maszyny).
Zlecenie bierze do wykonania ten, komu uda się zmienić stan „zlecona” → „trwa” (panel od razu albo worker, gdy panel
nie zdążył) — akcja nie wykona się dwa razy.
"""
import asyncio
import json
import logging
import time

from sqlalchemy import select

from . import auth, polecenia, profiles
from .adapters import AdapterError, registry
from .db import kv_get, kv_set
from .models import (action_run, device, external_ref, incident, incident_device, incident_event, integration,
                     observed_state)

log = logging.getLogger("denali.actions")
ENABLED_KEY = "actions.enabled"
RUN_TIMEOUT = 30                # jedno wywołanie API źródła
QUICK_WAIT = 10                 # tyle panel czeka na koniec zadania, zanim odda je workerowi
TASK_TIMEOUT = 30 * 60          # zadanie w źródle bez końca dłużej = nie wiadomo, co się stało
STUCK_AFTER = 5 * 60            # „trwa” bez zadania w źródle (panel padł w trakcie)
AGENT_ACK_TIMEOUT = 15 * 60     # agent nie potwierdził przyjęcia zlecenia
AGENT_RUN_TIMEOUT = 2 * 3600    # agent nie podał wyniku
ACTIVE = ("czeka", "zlecona", "trwa")
STATUS_LABEL = {"czeka": "czeka", "zlecona": "zlecona", "trwa": "trwa", "ok": "wykonana", "blad": "błąd"}


class ActionError(ValueError):
    pass


def catalog():
    """{„proxmox.reboot”: (klasa adaptera, ActionSpec)} — wszystkie akcje, jakie zna kod."""
    return {f"{t}.{a.key}": (cls, a) for t, cls in registry().items() for a in cls.actions}


def enabled(conn):
    return bool(kv_get(conn, ENABLED_KEY, True))


def set_enabled(conn, on, user, ip=""):
    kv_set(conn, ENABLED_KEY, bool(on))
    auth.audit(conn, "actions_enable" if on else "actions_disable", user=user, ip=ip)


# ───────────────────────── token do akcji ─────────────────────────
def action_secrets(row, box):
    return json.loads(box.decrypt(row["action_ct"])) if row["action_ct"] else None


def has_credentials(row, spec=None):
    """Czy integracja może wykonać akcję (spec) albo, bez spec, jakąkolwiek."""
    cls = registry().get(row["type"])
    if cls and cls.grants:
        chosen = (row["config"] or {}).get("permissions") or []
        return spec.grant in chosen if spec else bool(chosen)
    return bool(cls and (cls.push or (cls.action_fields and row["action_ct"])))


def save_grants(conn, row, keys, user, ip=""):
    """Wybrane zakresy jednego tokenu. Stary osobny token do akcji przestaje być potrzebny, więc znika z bazy."""
    cls = registry()[row["type"]]
    chosen = [g.key for g in cls.grants if g.key in keys]
    conn.execute(integration.update().where(integration.c.id == row["id"]).values(
        config={**(row["config"] or {}), "permissions": chosen}, action_ct=None))
    auth.audit(conn, "integration_grants", user=user, ip=ip, target=row["name"], params={"permissions": chosen})


def save_credentials(conn, box, row, form, user, ip=""):
    cls = registry()[row["type"]]
    if not cls.action_fields:
        raise ActionError("Ta integracja nie ma osobnego tokenu do akcji.")
    old = action_secrets(row, box) or {}
    values = {}
    for f in cls.action_fields:
        raw = str(form.get(f.key) or "").strip()
        values[f.key] = raw or (old.get(f.key, "") if f.kind == "secret" else "")
    try:
        cls.validate_action_secrets(values)
    except AdapterError as e:
        raise ActionError(str(e)) from None
    conn.execute(integration.update().where(integration.c.id == row["id"]).values(
        action_ct=box.encrypt(json.dumps(values))))
    auth.audit(conn, "action_token_set", user=user, ip=ip, target=row["name"])


def clear_credentials(conn, row, user, ip=""):
    conn.execute(integration.update().where(integration.c.id == row["id"]).values(action_ct=None))
    auth.audit(conn, "action_token_remove", user=user, ip=ip, target=row["name"])


def credentials_hint(row, box):
    """Identyfikator tokenu do akcji (bez sekretu) do pokazania na stronie integracji."""
    values = action_secrets(row, box) if row["action_ct"] else None
    if not values:
        return ""
    cls = registry()[row["type"]]
    return next((str(values.get(f.key, "")) for f in cls.action_fields if f.kind != "secret"), "zapisany")


def adapter_for(row, box, transport=None):
    from .integrations import secrets_of
    cls = registry()[row["type"]]
    return cls(row["config"] or {}, secrets_of(row, box), transport=transport,
               action_secrets=(action_secrets(row, box) or {}) if cls.action_fields else {})


# ───────────────────────── co można zrobić z urządzeniem ─────────────────────────
def _options(conn, device_id):
    """Każda akcja, którą kod umie wykonać na obiektach przypiętych do urządzenia (bez oceny, czy wolno)."""
    rows = conn.execute(
        select(external_ref.c.id.label("ref_id"), external_ref.c.external_id, integration)
        .join(integration, integration.c.id == external_ref.c.integration_id)
        .where(external_ref.c.device_id == device_id, external_ref.c.status == "zatwierdzony")
        .order_by(integration.c.name, external_ref.c.external_id)).mappings().all()
    types = registry()
    out = []
    for r in rows:
        cls = types.get(r["type"])
        if not cls or not cls.actions:
            continue
        st = conn.execute(select(observed_state).where(observed_state.c.integration_id == r["id"],
                                                       observed_state.c.external_id == r["external_id"])
                          ).mappings().first()
        for spec in cls.actions:
            if r["external_id"].startswith(spec.targets):
                out.append({"key": f"{r['type']}.{spec.key}", "spec": spec, "cls": cls, "ref_id": r["ref_id"],
                            "external_id": r["external_id"], "integration": r, "state": st})
    return out


def _busy(conn, integration_id, external_id):
    return conn.execute(select(action_run.c.id).where(
        action_run.c.integration_id == integration_id, action_run.c.external_id == external_id,
        action_run.c.status.in_(ACTIVE)).limit(1)).first() is not None


def _blocker(conn, o, allowed, on):
    """Dlaczego teraz nie wolno (tekst dla człowieka) albo None."""
    row, spec, st = o["integration"], o["spec"], o["state"]
    if not on:
        return "Akcje są wyłączone (Akcje → Włącz)."
    if not row["enabled"]:
        return f"Integracja „{row['name']}” jest wyłączona."
    if o["key"] not in allowed:
        return "Niedozwolona — nie ma jej na liście „Dozwolone akcje” (profil albo wyjątek na urządzeniu)."
    if not has_credentials(row, spec):
        if o["cls"].grants:
            label = next(g.label for g in o["cls"].grants if g.key == spec.grant)
            return f"Integracja „{row['name']}” nie ma zakresu „{label}” — zaznacz go w jej Uprawnieniach."
        return f"Integracja „{row['name']}” nie ma tokenu do akcji."
    if o["cls"].push and spec.key not in ((st["data"] if st else {}) or {}).get("actions", []):
        return ("Agent na maszynie na nią nie pozwala — lokalna lista: "
                f"sudo python3 /opt/denali-agent/denali-agent.py pozwol {spec.key}")
    if spec.when and (st["state"] if st else "unknown") not in spec.when:
        return "Nie w tym stanie obiektu."
    if not spec.terminal and _busy(conn, row["id"], o["external_id"]):
        return "Na tym obiekcie trwa inna akcja."
    return None


def for_device(conn, device_id, terminal=False):
    """Akcje urządzenia do karty: każda z oceną, czy wolno ją teraz zlecić, i dlaczego nie.
    terminal=True: te z karty Terminale (konsola, gotowe polecenia) zamiast zwykłych."""
    allowed = set(profiles.value(conn, device_id, "actions"))
    on = enabled(conn)
    out = []
    for o in _options(conn, device_id):
        if o["spec"].terminal != terminal:
            continue
        o["allowed"] = o["key"] in allowed
        o["reason"] = _blocker(conn, o, allowed, on)
        out.append(o)
    return out


def known_for_device(conn, device_id):
    """Klucze akcji, które mają sens dla urządzenia (do listy „Dozwolone akcje” na karcie)."""
    seen, out = set(), []
    for o in _options(conn, device_id):
        if o["key"] not in seen:
            seen.add(o["key"])
            out.append((o["key"], o["spec"], o["cls"]))
    return out


def find(conn, device_id, ref_id, key, terminal=False):
    o = next((x for x in for_device(conn, device_id, terminal) if x["ref_id"] == ref_id and x["key"] == key), None)
    if not o:
        raise ActionError("Tej akcji nie ma dla tego urządzenia.")
    return o


def snapshot_option(conn, device_id):
    """Snapshot w Proxmoxie, który można zrobić przed aktualizacją przez agenta (gotowy do zlecenia) albo None."""
    return next((o for o in for_device(conn, device_id) if o["key"] == "proxmox.snapshot" and not o["reason"]), None)


def reboot_option(conn, device_id, ref_id):
    """Restart przez tego samego agenta po aktualizacji (gotowy do zlecenia) albo None."""
    return next((o for o in for_device(conn, device_id)
                 if o["key"] == "agent.reboot" and o["ref_id"] == ref_id and not o["reason"]), None)


def ready_by_target(conn, device_ids):
    """{(integration_id, external_id): [akcje gotowe do zlecenia]} — przyciski na zakładkach Infrastruktury."""
    out = {}
    for did in sorted(set(device_ids)):
        for o in for_device(conn, did):
            if not o["reason"]:
                o["device_id"] = did
                out.setdefault((o["integration"]["id"], o["external_id"]), []).append(o)
    return out


# ───────────────────────── zlecenie ─────────────────────────
def _incident_note(conn, device_id, text, user, now):
    if not device_id:
        return
    ids = {r.id for r in conn.execute(
        select(incident.c.id).join(incident_device, incident_device.c.incident_id == incident.c.id, isouter=True)
        .where(incident.c.status != "zamkniety",
               (incident_device.c.device_id == device_id) | (incident.c.root_device_id == device_id)))}
    for iid in ids:
        conn.execute(incident_event.insert().values(incident_id=iid, ts=now, kind="akcja", text=text, user=user))


def _insert(conn, o, dev, user, ip, params, status, now, after_id=None, label=None):
    return conn.execute(action_run.insert().values(
        created_at=now, device_id=dev.id, integration_id=o["integration"]["id"], external_id=o["external_id"],
        action=o["key"], label=label or o["spec"].label, target=dev.name, params=params, status=status, after_id=after_id,
        requested_by=user, ip=ip)).inserted_primary_key[0]


def request(conn, *, device_id, ref_id, key, user, role, ip="", reauth_ok=False, snapshot_first=False,
            reboot_after=False, command=None, now=None):
    """Zleca akcję po sprawdzeniu wszystkich zamków. Zwraca id przebiegu, który trzeba wykonać od razu
    (pierwszy w łańcuchu: snapshot albo sama akcja). reboot_after: po udanej aktualizacji przez agenta restart
    maszyny — tylko gdy jest wymagany (sprawdza agent). command: klucz gotowego polecenia z karty Terminale.
    Odmowa → ActionError i wpis „action_blocked” w audycie."""
    now = now or time.time()
    dev = conn.execute(select(device.c.id, device.c.name).where(device.c.id == device_id)).first()

    def refuse(why):
        auth.audit(conn, "action_blocked", user=user, ip=ip, target=dev.name if dev else str(device_id),
                   params={"action": str(key)[:64], "ref_id": ref_id}, result=why)
        raise ActionError(why)

    if role != "admin":
        refuse("Akcje może zlecać tylko administrator.")
    if not dev:
        refuse("Nie ma takiego urządzenia.")
    try:
        o = find(conn, device_id, ref_id, key, terminal=command is not None)
    except ActionError as e:
        refuse(str(e))
    if o["reason"]:
        refuse(o["reason"])
    params, label = {}, None
    if command is not None:
        if o["key"] != "agent.polecenia" or command not in polecenia.COMMANDS:
            refuse("Nie ma takiego polecenia.")
        params, label = {"command": command}, polecenia.COMMANDS[command].label
    if o["spec"].dangerous and not reauth_ok:
        refuse("Ta akcja wymaga potwierdzenia hasłem i kodem 2FA.")
    reboot = None
    if reboot_after and o["key"] == "agent.apt_upgrade":
        reboot = reboot_option(conn, device_id, ref_id)
        if not reboot:
            refuse("Restart po aktualizacji jest niemożliwy (agent: „Uruchom ponownie maszynę” niedozwolone w profilu "
                   "albo nie ma go na lokalnej liście maszyny).")
    first = None
    if snapshot_first and o["cls"].push:
        snap = snapshot_option(conn, device_id)
        if not snap:
            refuse("Snapshot przed aktualizacją jest niemożliwy (Proxmox: brak tokenu do akcji albo snapshot "
                   "niedozwolony w profilu).")
        first = _insert(conn, snap, dev, user, ip, {"description": f"Denali: przed „{o['spec'].label}”"},
                        "zlecona", now)
        auth.audit(conn, "action_request", user=user, ip=ip, target=dev.name,
                   params={"run": first, "action": snap["key"], "external_id": snap["external_id"]})
    run_id = _insert(conn, o, dev, user, ip, params, "czeka" if first else "zlecona", now, after_id=first, label=label)
    auth.audit(conn, "action_request", user=user, ip=ip, target=dev.name,
               params={"run": run_id, "action": key, "external_id": o["external_id"], "after": first, **params})
    if reboot:
        last = _insert(conn, reboot, dev, user, ip, {"if_required": True}, "czeka", now, after_id=run_id)
        auth.audit(conn, "action_request", user=user, ip=ip, target=dev.name,
                   params={"run": last, "action": reboot["key"], "external_id": reboot["external_id"],
                           "after": run_id, "if_required": True})
    _incident_note(conn, device_id, f"Akcja zlecona: {o['spec'].label} ({o['integration']['name']})"
                   + (" — najpierw snapshot" if first else "") + (" — potem restart, jeśli wymagany" if reboot else ""),
                   user, now)
    return first or run_id


def get(conn, run_id):
    return conn.execute(select(action_run).where(action_run.c.id == run_id)).mappings().first()


def _claim(conn, run_id, now):
    return conn.execute(action_run.update().where(action_run.c.id == run_id, action_run.c.status == "zlecona")
                        .values(status="trwa", started_at=now)).rowcount == 1


def finish(conn, run, ok, text, now=None, limit=2000):
    """Wynik akcji: zapis, audyt, przebieg incydentu, kolejny krok łańcucha, szybszy odczyt źródła."""
    now = now or time.time()
    text = str(text or ("wykonana" if ok else "nie powiodła się"))[:limit]
    if not conn.execute(action_run.update().where(action_run.c.id == run["id"], action_run.c.status.in_(ACTIVE))
                        .values(status="ok" if ok else "blad", finished_at=now, result=text)).rowcount:
        return
    auth.audit(conn, "action_result", user=run["requested_by"], ip=run["ip"], target=run["target"],
               params={"run": run["id"], "action": run["action"], "external_id": run["external_id"]},
               result=("ok: " if ok else "błąd: ") + text[:180])
    _incident_note(conn, run["device_id"], f"Akcja {'wykonana' if ok else 'nieudana'}: {run['label']} — {text[:300]}",
                   run["requested_by"], now)
    for nxt in conn.execute(select(action_run).where(action_run.c.after_id == run["id"],
                                                     action_run.c.status == "czeka")).mappings().all():
        if ok:
            # agent liczy czas na odebranie zlecenia od chwili, gdy ono naprawdę czeka (nie od zlecenia łańcucha)
            push = bool((cls := _row_and_cls(conn, nxt)[1]) and cls.push)
            conn.execute(action_run.update().where(action_run.c.id == nxt["id"]).values(
                status="zlecona", **({"task": {"queued_at": now}} if push else {})))
        else:
            finish(conn, nxt, False, f"Nie wykonano — poprzedni krok ({run['label']}) nie powiódł się.", now)
    if run["integration_id"]:       # świeży stan obiektu przy najbliższym obiegu workera
        conn.execute(integration.update().where(integration.c.id == run["integration_id"]).values(last_attempt_at=None))


def _row_and_cls(conn, run):
    row = conn.execute(select(integration).where(integration.c.id == run["integration_id"])).mappings().first() \
        if run["integration_id"] else None
    return row, (registry().get(row["type"]) if row else None)


async def execute(engine, box, run_id, transport=None, wait=QUICK_WAIT):
    """Wykonuje zleconą akcję przez API źródła (agentowi zlecenie zabierze jego raport)."""
    now = time.time()
    with engine.begin() as conn:
        run = get(conn, run_id)
        if not run or run["status"] != "zlecona":
            return
        row, cls = _row_and_cls(conn, run)
        if not row or not cls:
            return finish(conn, run, False, "Integracja została usunięta.", now)
        if cls.push:
            return
        if not enabled(conn):
            return finish(conn, run, False, "Akcje wyłączono, zanim zlecenie zostało wykonane.", now)
        if not _claim(conn, run_id, now):
            return
        st = conn.execute(select(observed_state.c.data).where(
            observed_state.c.integration_id == row["id"], observed_state.c.external_id == run["external_id"])).first()
    key = run["action"].split(".", 1)[1]
    try:
        adapter = adapter_for(row, box, transport)
        res = await asyncio.wait_for(adapter.run_action(key, run["external_id"], st.data if st else {},
                                                        dict(run["params"] or {})), RUN_TIMEOUT)
    except AdapterError as e:
        res, error = None, str(e)
    except asyncio.TimeoutError:
        res, error = None, f"Źródło nie odpowiedziało w ciągu {RUN_TIMEOUT} s — sprawdź w nim, czy akcja ruszyła."
    except Exception as e:      # nieprzewidziany błąd adaptera nie może zostawić przebiegu w „trwa”
        log.exception("Akcja %s nie powiodła się", run["action"])
        res, error = None, f"Nieoczekiwany błąd ({e.__class__.__name__})."
    with engine.begin() as conn:
        run = get(conn, run_id)
        if res is None:
            return finish(conn, run, False, error)
        if not res.ok or not res.task:
            return finish(conn, run, res.ok, res.text)
        conn.execute(action_run.update().where(action_run.c.id == run_id).values(task=res.task, result=res.text))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        await asyncio.sleep(min(2, max(0.0, deadline - time.monotonic())))
        if await _check(engine, box, run_id, transport):
            return


async def _check(engine, box, run_id, transport=None, now=None):
    """Sprawdza zadanie w źródle. True = zakończone (zapisane)."""
    now = now or time.time()
    with engine.connect() as conn:
        run = get(conn, run_id)
        row, cls = _row_and_cls(conn, run) if run else (None, None)
    if not run or run["status"] != "trwa" or not run["task"]:
        return True
    if not row:
        with engine.begin() as conn:
            finish(conn, run, False, "Integracja została usunięta.", now)
        return True
    try:
        res = await asyncio.wait_for(adapter_for(row, box, transport).check_task(run["task"]), RUN_TIMEOUT)
    except (AdapterError, asyncio.TimeoutError) as e:
        res = None
        if now - (run["started_at"] or now) > TASK_TIMEOUT:
            with engine.begin() as conn:
                finish(conn, run, False, f"Nie wiadomo, czy się udało — sprawdź w źródle ({e or 'brak odpowiedzi'}).", now)
            return True
    if res is None:
        if now - (run["started_at"] or now) > TASK_TIMEOUT:
            with engine.begin() as conn:
                finish(conn, run, False, "Zadanie w źródle nie skończyło się w 30 minut — sprawdź w nim, co się dzieje.", now)
            return True
        return False
    with engine.begin() as conn:
        finish(conn, get(conn, run_id), res.ok, res.text, now)
    return True


async def follow(engine, box, transport=None, now=None):
    """Worker: dokończa to, czego panel nie zdążył — zlecenia po snapshocie, długie zadania, zagubione przebiegi."""
    now = now or time.time()
    with engine.connect() as conn:
        rows = conn.execute(select(action_run).where(action_run.c.status.in_(ACTIVE))
                            .order_by(action_run.c.id)).mappings().all()
        push = {r["id"]: bool((c := _row_and_cls(conn, r)[1]) and c.push) for r in rows}
    for r in rows:
        try:
            if push[r["id"]]:
                with engine.begin() as conn:
                    _agent_timeouts(conn, r, now)
            elif r["status"] == "zlecona":
                await execute(engine, box, r["id"], transport, wait=0)
            elif r["status"] == "trwa" and r["task"]:
                await _check(engine, box, r["id"], transport, now)
            elif r["status"] == "trwa" and now - (r["started_at"] or now) > STUCK_AFTER:
                with engine.begin() as conn:
                    finish(conn, r, False, "Przerwane (panel uruchomił się ponownie w trakcie) — sprawdź w źródle.", now)
            elif r["status"] == "czeka" and (not r["after_id"] or not get_after(engine, r["after_id"])):
                with engine.begin() as conn:
                    finish(conn, r, False, "Poprzedni krok zniknął — nie wykonano.", now)
        except Exception:
            log.exception("Dokończenie akcji %s nie powiodło się", r["id"])


def get_after(engine, run_id):
    with engine.connect() as conn:
        return get(conn, run_id)


# ───────────────────────── agent ─────────────────────────
def _agent_timeouts(conn, r, now):
    delivered = (r["task"] or {}).get("delivered_at")
    queued = (r["task"] or {}).get("queued_at") or r["created_at"]
    if r["status"] == "zlecona" and not delivered and not enabled(conn):
        finish(conn, r, False, "Akcje wyłączono, zanim agent odebrał zlecenie.", now)
    elif r["status"] == "zlecona" and not delivered and now - queued > AGENT_ACK_TIMEOUT:
        finish(conn, r, False, "Agent nie odebrał zlecenia (nie wysyła raportów?).", now)
    elif r["status"] == "zlecona" and delivered and now - delivered > AGENT_ACK_TIMEOUT:
        finish(conn, r, False, "Agent nie potwierdził zlecenia — czy działa denali-agent-akcje.path?", now)
    elif r["status"] == "trwa" and now - (r["started_at"] or now) > AGENT_RUN_TIMEOUT:
        finish(conn, r, False, "Agent nie podał wyniku w 2 godziny — sprawdź na maszynie: "
                               "journalctl -u denali-agent-akcje -n 50", now)


def agent_exchange(conn, row, reported, now=None):
    """Raport agenta: wyniki zleceń → przebiegi; odpowiedź: nowe zlecenia i potwierdzenie przyjętych wyników.

    Zlecenie idzie do agenta raz. Agent i tak wykona je tylko, jeśli akcja jest na jego lokalnej liście."""
    now = now or time.time()
    ack = []
    for j in reported if isinstance(reported, list) else []:
        if not isinstance(j, dict) or not isinstance(j.get("id"), int):
            continue
        run = get(conn, j["id"])
        if not run or run["integration_id"] != row["id"]:
            continue
        state = j.get("state")
        if state == "trwa":
            if run["status"] == "zlecona":
                conn.execute(action_run.update().where(action_run.c.id == run["id"])
                             .values(status="trwa", started_at=float(j.get("started") or now)))
            continue
        if state in ("ok", "blad"):
            ack.append(run["id"])
            if run["status"] in ACTIVE:
                out = str(j.get("output") or "").strip()
                limit = polecenia.OUTPUT_MAX if (run["params"] or {}).get("command") else 1500
                finish(conn, run, state == "ok", (out[-limit:] if out else "") or
                       ("wykonana" if state == "ok" else f"kod wyjścia {j.get('rc')}"), now, limit=max(limit, 2000))
    jobs = []
    if enabled(conn):
        for run in conn.execute(select(action_run).where(action_run.c.integration_id == row["id"],
                                                         action_run.c.status == "zlecona")).mappings().all():
            if (run["task"] or {}).get("delivered_at"):
                continue
            conn.execute(action_run.update().where(action_run.c.id == run["id"]).values(task={"delivered_at": now}))
            job = {"id": run["id"], "action": run["action"].split(".", 1)[1]}
            if (run["params"] or {}).get("if_required"):
                job["params"] = {"if_required": True}
            elif (run["params"] or {}).get("command"):
                job["params"] = {"command": run["params"]["command"]}
            jobs.append(job)
    return jobs, ack


# ───────────────────────── listy ─────────────────────────
def recent(conn, device_id=None, limit=100):
    q = select(action_run).order_by(action_run.c.id.desc()).limit(limit)
    if device_id is not None:
        q = q.where(action_run.c.device_id == device_id)
    return conn.execute(q).mappings().all()
