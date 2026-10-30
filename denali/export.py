"""Eksport statyczny: procedury, runbooki, kontakty i spis urządzeń jako zwykłe pliki HTML (+ PDF).

Na wypadek, gdy panel leży: pliki trafiają na VPS i do telefonu. Celowo BEZ skarbca, plików i audytu.
Zapis tylko przy zmianie treści i atomowo (nowy katalog → podmiana), żeby synchronizacja nie złapała pół wersji.
"""
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from . import __version__, docs, inventory as inv
from .models import contact, dependency, device, device_address, runbook, runbook_device
from .templating import templates

log = logging.getLogger("denali.export")
CSS = Path(__file__).resolve().parent / "static" / "eksport.css"
PDF_NAME = "denali-procedury.pdf"


def export_dir(data_dir):
    return Path(data_dir) / "export"


def collect(conn):
    """Wszystko, co trafia do eksportu — bez sekretów."""
    devices = conn.execute(select(device).order_by(device.c.name)).mappings().all()
    names = {d["id"]: d["name"] for d in devices}
    addrs, deps = {}, {}
    for a in conn.execute(select(device_address).order_by(device_address.c.is_primary.desc(), device_address.c.kind)):
        addrs.setdefault(a.device_id, []).append(a.value)
    for x in conn.execute(select(dependency)):
        deps.setdefault(x.device_id, []).append(
            f"{names.get(x.depends_on_id, '?')} ({inv.DEP_TYPES.get(x.type, x.type)}{'' if x.hard else ', miękko'})")
    g = inv.load_graph(conn)
    impact = {d["id"]: sorted(g.name(n) for n in g.impact(d["id"])[0]) for d in devices}
    procs = [docs.get_procedure(conn, p["id"]) for p in docs.list_procedures(conn)]
    rbs = []
    for r in conn.execute(select(runbook).order_by(runbook.c.title)).mappings():
        ids = [x.device_id for x in conn.execute(select(runbook_device.c.device_id)
                                                 .where(runbook_device.c.runbook_id == r["id"]))]
        rbs.append({**r, "devices": [names[i] for i in ids if i in names]})
    return {
        "devices": devices, "names": names, "addrs": addrs, "deps": deps, "impact": impact,
        "procedures": procs, "runbooks": rbs,
        "contacts": conn.execute(select(contact).order_by(contact.c.name)).mappings().all(),
    }


def render_pages(data, generated_at):
    """{nazwa pliku: html}. Linki między stronami są względne — działa z dysku, z VPS-a i z telefonu."""
    ctx = {**data, "generated_at": generated_at, "version": __version__}
    pages = {"index.html": templates.get_template("eksport/index.html").render(ctx),
             "urzadzenia.html": templates.get_template("eksport/urzadzenia.html").render(ctx)}
    for p in data["procedures"]:
        pages[f"procedura-{p['id']}.html"] = templates.get_template("eksport/procedura.html").render({**ctx, "p": p})
    for r in data["runbooks"]:
        pages[f"runbook-{r['id']}.html"] = templates.get_template("eksport/runbook.html").render({**ctx, "r": r})
    return pages


def content_hash(data):
    """Skrót samej treści (bez daty wygenerowania), żeby wykryć, czy coś się zmieniło."""
    def plain(x):
        if isinstance(x, (list, tuple)):
            return [plain(i) for i in x]
        if isinstance(x, dict) or hasattr(x, "keys"):
            return {str(k): plain(x[k]) for k in sorted(x.keys(), key=str)}
        return x
    blob = json.dumps(plain(data), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256((blob + __version__).encode()).hexdigest()


def build_pdf(pages, out_file):
    """PDF ze wszystkimi procedurami i runbookami. Wymaga WeasyPrint (jest w obrazie Dockera)."""
    from weasyprint import CSS as WCSS, HTML   # import tu: WeasyPrint jest opcjonalny
    order = ["index.html"] + sorted(k for k in pages if k.startswith("procedura-")) \
        + sorted(k for k in pages if k.startswith("runbook-")) + ["urzadzenia.html"]
    docs_ = [HTML(string=pages[k]).render(stylesheets=[WCSS(filename=str(CSS))]) for k in order]
    first = docs_[0]
    first.copy([p for d in docs_ for p in d.pages]).write_pdf(out_file)


def pdf_available():
    try:
        import weasyprint  # noqa: F401
        return True
    except (ImportError, OSError):   # OSError: brak bibliotek systemowych (pango) — np. na Windowsie
        return False


def run(conn, data_dir, pdf=None, force=False, now=None, out=None):
    """Generuje eksport, jeśli treść się zmieniła. Zwraca True, gdy zapisano nową wersję.
    out: katalog docelowy (domyślnie data_dir/export — ten synchronizowany na VPS)."""
    out = Path(out) if out else export_dir(data_dir)
    data = collect(conn)
    digest = content_hash(data)
    marker = out / ".hash"
    if not force and marker.exists() and marker.read_text() == digest:
        return False
    pages = render_pages(data, now or time.time())
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for name, html in pages.items():
        (tmp / name).write_text(html, encoding="utf-8")
    shutil.copyfile(CSS, tmp / "eksport.css")
    if pdf is None:
        pdf = pdf_available()
    if pdf:
        try:
            build_pdf(pages, tmp / PDF_NAME)
        except Exception:   # PDF to dodatek — jego błąd nie może zablokować eksportu HTML
            log.exception("Nie udało się zrobić PDF; eksport HTML zapisany bez niego")
    (tmp / ".hash").write_text(digest)
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.rename(old)
    tmp.rename(out)
    shutil.rmtree(old, ignore_errors=True)
    log.info("Eksport statyczny zapisany: %s (%d stron%s)", out, len(pages), ", PDF" if pdf else "")
    return True
