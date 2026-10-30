"""Eksport awaryjny: treść, brak sekretów, zapis tylko przy zmianie, PDF (jeśli jest WeasyPrint)."""
import pytest

from denali import docs, export, inventory as inv, vault
from denali.worker import tick


@pytest.fixture
def filled(conn, app):
    pve = inv.create_device(conn, "pve", "host", role="serwer główny", place="szafa")
    nas = inv.create_device(conn, "truenas", "vm", parent_id=pve)
    inv.add_address(conn, nas, "ip", "192.168.1.30")
    c = docs.save_contact(conn, "Dostawca internetu", "+48 22 100 200")
    docs.save_procedure(conn, "Jak odtworzyć Denali", [("Przywróć VM z PBS", "`qmrestore …`")], scope="panel")
    docs.save_procedure(conn, "Padł TrueNAS", [("Sprawdź konsolę", ""), ("Restart VM", "")], device_ids=[nas],
                        contact_ids=[c])
    docs.save_runbook(conn, "TrueNAS <obsługa>", "Pula **tank**\n\n<script>x()</script>", [nas])
    vault.add(conn, app.state.box, "root TrueNAS", "HASLO-KTORE-NIE-WYCIEKNIE", role="admin", user="t",
              device_id=nas, username="root")
    return {"pve": pve, "nas": nas}


def read_all(out):
    return {p.name: p.read_text(encoding="utf-8") for p in out.iterdir() if p.suffix == ".html"}


def test_eksport_ma_procedury_runbooki_kontakty_i_urzadzenia(conn, settings, filled):
    assert export.run(conn, settings.data_dir, pdf=False)
    out = export.export_dir(settings.data_dir)
    pages = read_all(out)
    assert {"index.html", "urzadzenia.html"} <= set(pages)
    assert len([n for n in pages if n.startswith("procedura-")]) == 2 and (out / "eksport.css").exists()
    index = pages["index.html"]
    assert index.index("Jak odtworzyć Denali") < index.index("Padł TrueNAS")    # „gdy nie działa panel” na górze
    assert 'href="tel:+4822100200"' in index
    assert "192.168.1.30" in pages["urzadzenia.html"] and "truenas" in pages["urzadzenia.html"]
    runbook = next(v for k, v in pages.items() if k.startswith("runbook-"))
    assert "<strong>tank</strong>" in runbook and "<script>" not in runbook and "TrueNAS &lt;obsługa&gt;" in runbook


def test_eksport_bez_hasel(conn, settings, filled):
    export.run(conn, settings.data_dir, pdf=False)
    everything = "".join(read_all(export.export_dir(settings.data_dir)).values())
    assert "HASLO-KTORE-NIE-WYCIEKNIE" not in everything and "root TrueNAS" not in everything


def test_zapis_tylko_przy_zmianie(conn, settings, filled):
    assert export.run(conn, settings.data_dir, pdf=False, now=1000)
    assert not export.run(conn, settings.data_dir, pdf=False, now=2000)      # nic się nie zmieniło
    docs.save_contact(conn, "Elektryk", "600 100 200")
    assert export.run(conn, settings.data_dir, pdf=False, now=3000)
    assert "Elektryk" in (export.export_dir(settings.data_dir) / "index.html").read_text(encoding="utf-8")
    assert not (settings.data_dir / "export.tmp").exists() and not (settings.data_dir / "export.old").exists()


def test_usunieta_procedura_znika_z_eksportu(conn, settings, filled):
    export.run(conn, settings.data_dir, pdf=False)
    for p in docs.list_procedures(conn):
        docs.delete_procedure(conn, p["id"])
    export.run(conn, settings.data_dir, pdf=False)
    assert not [n for n in read_all(export.export_dir(settings.data_dir)) if n.startswith("procedura-")]


def test_worker_robi_eksport(app, settings):
    with app.state.engine.begin() as c:           # zatwierdzone, bo worker ma własne połączenie
        docs.save_procedure(c, "Padł pve", [("Krok", "")])
    state = {}
    tick(app.state.engine, settings, state, now=10_000)
    assert (export.export_dir(settings.data_dir) / "index.html").exists()
    assert state["export_forced_at"] == 10_000


def test_eksport_z_konsoli_do_wlasnego_katalogu(conn, settings, filled, tmp_path):
    out = tmp_path / "na-laptopa"
    export.run(conn, settings.data_dir, pdf=False, force=True, out=out)
    assert (out / "index.html").exists()


@pytest.mark.skipif(not export.pdf_available(), reason="brak WeasyPrint (jest w obrazie Dockera)")
def test_pdf(conn, settings, filled):
    export.run(conn, settings.data_dir, pdf=True)
    pdf = export.export_dir(settings.data_dir) / export.PDF_NAME
    assert pdf.read_bytes()[:4] == b"%PDF"
