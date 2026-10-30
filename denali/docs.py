"""Runbooki, procedury (kroki, urządzenia, kontakty) i kontakty."""
import re
import time

from sqlalchemy import delete, func, or_, select

from .models import (contact, device, procedure, procedure_contact, procedure_device, procedure_step, runbook,
                     runbook_device)

SCOPES = ("infrastruktura", "panel")
# device_down:<nazwa urządzenia> albo device_down:typ:<typ>; puste = procedura tylko przypięta do urządzeń
_TRIGGER = re.compile(r"^device_down:(typ:[a-z]+|[^\s:][^:]{0,98})$")


class DocsError(ValueError):
    pass


def _title(t):
    t = (t or "").strip()
    if not t:
        raise DocsError("Podaj tytuł.")
    if len(t) > 200:
        raise DocsError("Tytuł jest za długi.")
    return t


def _link(conn, table, key_col, key, device_ids):
    conn.execute(delete(table).where(table.c[key_col] == key))
    ids = sorted(set(device_ids or []))
    if ids:
        found = {r.id for r in conn.execute(select(device.c.id).where(device.c.id.in_(ids)))}
        if found != set(ids):
            raise DocsError("Któreś z wybranych urządzeń już nie istnieje.")
        conn.execute(table.insert(), [{key_col: key, "device_id": d} for d in ids])


# ───────────────────────── runbooki ─────────────────────────
def save_runbook(conn, title, body_md, device_ids=(), user="", runbook_id=None, now=None):
    values = {"title": _title(title), "body_md": body_md or "", "updated_at": now or time.time(), "updated_by": user}
    if runbook_id is None:
        runbook_id = conn.execute(runbook.insert().values(**values)).inserted_primary_key[0]
    elif not conn.execute(runbook.update().where(runbook.c.id == runbook_id).values(**values)).rowcount:
        raise DocsError("Nie ma takiego runbooka.")
    _link(conn, runbook_device, "runbook_id", runbook_id, device_ids)
    return runbook_id


def runbooks_for_device(conn, device_id):
    return conn.execute(select(runbook).join(runbook_device, runbook_device.c.runbook_id == runbook.c.id)
                        .where(runbook_device.c.device_id == device_id).order_by(runbook.c.title)).mappings().all()


def delete_runbook(conn, runbook_id):
    conn.execute(delete(runbook).where(runbook.c.id == runbook_id))


# ───────────────────────── procedury ─────────────────────────
def save_procedure(conn, title, steps, *, scope="infrastruktura", intro_md="", trigger="", device_ids=(),
                   contact_ids=(), user="", procedure_id=None, now=None):
    """steps: [(tytuł, treść)] w kolejności. Kroki są zapisywane od nowa przy każdej zmianie."""
    if scope not in SCOPES:
        raise DocsError("Procedura dotyczy infrastruktury albo panelu.")
    trigger = (trigger or "").strip()
    if trigger and not _TRIGGER.match(trigger):
        raise DocsError("Wyzwalacz ma postać device_down:<nazwa> albo device_down:typ:<typ>.")
    steps = [(_title(t), b or "") for t, b in steps]
    if not steps:
        raise DocsError("Procedura potrzebuje co najmniej jednego kroku.")
    values = {"title": _title(title), "scope": scope, "intro_md": intro_md or "", "trigger": trigger,
              "updated_at": now or time.time(), "updated_by": user}
    if procedure_id is None:
        procedure_id = conn.execute(procedure.insert().values(**values)).inserted_primary_key[0]
    elif not conn.execute(procedure.update().where(procedure.c.id == procedure_id).values(**values)).rowcount:
        raise DocsError("Nie ma takiej procedury.")
    conn.execute(delete(procedure_step).where(procedure_step.c.procedure_id == procedure_id))
    conn.execute(procedure_step.insert(), [{"procedure_id": procedure_id, "position": i, "title": t, "body_md": b}
                                           for i, (t, b) in enumerate(steps, 1)])
    _link(conn, procedure_device, "procedure_id", procedure_id, device_ids)
    conn.execute(delete(procedure_contact).where(procedure_contact.c.procedure_id == procedure_id))
    cids = sorted(set(contact_ids or []))
    if cids:
        if conn.execute(select(func.count()).select_from(contact).where(contact.c.id.in_(cids))).scalar() != len(cids):
            raise DocsError("Któryś z wybranych kontaktów już nie istnieje.")
        conn.execute(procedure_contact.insert(), [{"procedure_id": procedure_id, "contact_id": c} for c in cids])
    return procedure_id


def get_procedure(conn, procedure_id):
    p = conn.execute(select(procedure).where(procedure.c.id == procedure_id)).mappings().first()
    if not p:
        return None
    out = dict(p)
    out["steps"] = conn.execute(select(procedure_step).where(procedure_step.c.procedure_id == procedure_id)
                                .order_by(procedure_step.c.position)).mappings().all()
    out["devices"] = conn.execute(select(device.c.id, device.c.name).join(
        procedure_device, procedure_device.c.device_id == device.c.id)
        .where(procedure_device.c.procedure_id == procedure_id).order_by(device.c.name)).mappings().all()
    out["contacts"] = conn.execute(select(contact).join(procedure_contact, procedure_contact.c.contact_id == contact.c.id)
                                   .where(procedure_contact.c.procedure_id == procedure_id)
                                   .order_by(contact.c.name)).mappings().all()
    return out


def list_procedures(conn, scope=None):
    q = select(procedure).order_by(procedure.c.title)
    if scope:
        q = q.where(procedure.c.scope == scope)
    return conn.execute(q).mappings().all()


def procedures_for_device(conn, device_id):
    """Procedury na awarię urządzenia: przypięte do niego albo z pasującym wyzwalaczem (po nazwie lub typie)."""
    d = conn.execute(select(device.c.name, device.c.kind).where(device.c.id == device_id)).first()
    if not d:
        return []
    linked = select(procedure_device.c.procedure_id).where(procedure_device.c.device_id == device_id)
    q = select(procedure).where(or_(
        procedure.c.id.in_(linked),
        func.lower(procedure.c.trigger) == f"device_down:{d.name}".lower(),
        procedure.c.trigger == f"device_down:typ:{d.kind}",
    )).order_by(procedure.c.title)
    return conn.execute(q).mappings().all()


def mark_tested(conn, procedure_id, now=None):
    conn.execute(procedure.update().where(procedure.c.id == procedure_id).values(last_tested_at=now or time.time()))


def delete_procedure(conn, procedure_id):
    conn.execute(delete(procedure).where(procedure.c.id == procedure_id))


# ───────────────────────── kontakty ─────────────────────────
def save_contact(conn, name, phone="", note="", contact_id=None):
    name = (name or "").strip()
    if not name:
        raise DocsError("Podaj nazwę kontaktu.")
    phone = (phone or "").strip()
    if phone and not re.match(r"^[+0-9 ()-]{3,40}$", phone):
        raise DocsError("Telefon może zawierać tylko cyfry, spacje, +, - i nawiasy.")
    values = {"name": name[:200], "phone": phone, "note": (note or "").strip()[:300]}
    if contact_id is None:
        return conn.execute(contact.insert().values(**values)).inserted_primary_key[0]
    conn.execute(contact.update().where(contact.c.id == contact_id).values(**values))
    return contact_id
