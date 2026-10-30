"""Wiersz poleceń Denali.

  python -m denali genkeys                          wypisuje nowy DENALI_ENCRYPTION_KEY (dopisz do .env)
  python -m denali migrate                          aktualizuje schemat bazy
  python -m denali serve                            serwer WWW
  python -m denali worker                           zadania w tle
  python -m denali create-user LOGIN [--role podglad]   nowe konto (domyślnie administrator); pyta o hasło
  python -m denali reset-password LOGIN [--disable-2fa] nowe hasło, wylogowuje wszystkie sesje
  python -m denali users                            lista kont
  python -m denali export [--out DIR] [--pdf|--no-pdf] eksport awaryjny procedur (HTML + PDF), np. na laptopa
  python -m denali backup [--out DIR]               kopia bazy i plików (worker robi ją sam raz na dobę)
  python -m denali restore ARCHIWUM [--force]       odtworzenie z kopii (panel i worker zatrzymane)
  python -m denali migrate-hlc ZRODLO [--site NAZWA] [--wykonaj]
                                                    import z HLC (ZIP z `hlc backup`, także http://…); bez --wykonaj próba
"""
import argparse
import asyncio
import getpass
import logging
import sys


def _ask_password(login: str) -> str:
    pw = getpass.getpass(f"Hasło dla {login} (min. 12 znaków): ")
    if pw != getpass.getpass("Powtórz hasło: "):
        sys.exit("Hasła się różnią — nic nie zmieniono.")
    return pw


def main(argv=None):
    ap = argparse.ArgumentParser(prog="denali", description="Denali — panel do homelaba")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("genkeys")
    sub.add_parser("migrate")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker")
    c = sub.add_parser("create-user")
    c.add_argument("login")
    c.add_argument("--role", choices=("admin", "podglad"), default="admin")
    r = sub.add_parser("reset-password")
    r.add_argument("login")
    r.add_argument("--disable-2fa", action="store_true", help="wyłącz też logowanie dwuetapowe (zgubiony telefon)")
    sub.add_parser("users")
    b = sub.add_parser("backup")
    b.add_argument("--out", help="katalog docelowy (domyślnie DENALI_DATA_DIR/backups)")
    rs = sub.add_parser("restore")
    rs.add_argument("archive")
    rs.add_argument("--force", action="store_true", help="zastąp istniejącą bazę (zostanie odłożona obok)")
    mh = sub.add_parser("migrate-hlc")
    mh.add_argument("source", help="kopia HLC: plik ZIP, adres http://… do ZIP-a, katalog albo hlc.db")
    mh.add_argument("--site", action="append", help="tylko ta lokalizacja z HLC (można podać kilka razy)")
    mh.add_argument("--wykonaj", action="store_true", help="zapisz (bez tego: próba na sucho, nic nie zmienia)")
    e = sub.add_parser("export")
    e.add_argument("--out", help="katalog docelowy (domyślnie DENALI_DATA_DIR/export)")
    e.add_argument("--pdf", action=argparse.BooleanOptionalAction, default=None,
                   help="z PDF (domyślnie: jeśli jest WeasyPrint)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "genkeys":
        from cryptography.fernet import Fernet
        print(f"DENALI_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
        print("# Zapisz te klucze także w menedżerze haseł.", file=sys.stderr)
        return

    from .config import get_settings
    settings = get_settings()
    settings.check()

    if a.cmd == "migrate":
        from .migrate import upgrade
        upgrade(settings)
        print("Schemat bazy aktualny.")
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("denali.app:create_app", factory=True, host=a.host, port=a.port, proxy_headers=True,
                    forwarded_allow_ips=",".join(settings.trusted_proxies), server_header=False)
    elif a.cmd == "backup":
        from . import backup
        print(f"Kopia zapisana: {backup.create(settings, out_dir=a.out)}")
    elif a.cmd == "restore":
        from . import backup
        try:
            n = backup.restore(settings, a.archive, force=a.force)
        except backup.BackupError as e:
            sys.exit(str(e))
        print(f"Odtworzono bazę i {n} plików. Uruchom panel i worker.")
    elif a.cmd == "migrate-hlc":
        import os
        from . import hlc_import
        from .crypto import Box
        from .db import make_engine
        key = os.environ.get("HLC_FERNET_KEY") or getpass.getpass(
            "HLC_FERNET_KEY z /opt/hlc/.env (wklej i Enter; puste = bez haseł ze skarbca): ")
        try:
            report = hlc_import.migrate(make_engine(settings.db_url), settings, Box(settings.encryption_key), a.source,
                                        old_key=key or None, sites=a.site, apply=a.wykonaj)
        except hlc_import.MigrationError as e:
            sys.exit(str(e))
        print(report.text(applied=a.wykonaj))
    elif a.cmd == "export":
        from . import export
        from .db import make_engine
        if a.pdf and not export.pdf_available():
            sys.exit("Brak WeasyPrint (pip install '.[pdf]' + biblioteki pango) — uruchom bez --pdf albo w kontenerze.")
        engine = make_engine(settings.db_url)
        with engine.connect() as conn:
            export.run(conn, settings.data_dir, pdf=a.pdf, force=True, out=a.out)
        print(f"Eksport zapisany w {a.out or export.export_dir(settings.data_dir)}")
    elif a.cmd == "worker":
        from .worker import run
        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            pass
    else:
        from . import auth
        from .db import make_engine
        from .models import app_user
        engine = make_engine(settings.db_url)
        if a.cmd == "users":
            with engine.connect() as conn:
                for u in conn.execute(app_user.select().order_by(app_user.c.login)).mappings():
                    print(f"{u['login']:20} {u['role']:8} 2FA: {'tak' if u['totp_ct'] else 'nie'}"
                          f"{'  (wyłączone)' if u['disabled'] else ''}")
            return
        try:
            if a.cmd == "create-user":
                pw = _ask_password(a.login)
                with engine.begin() as conn:
                    auth.create_user(conn, a.login, pw, a.role)
                    auth.audit(conn, "user_create", user="konsola serwera", target=a.login, params={"role": a.role})
                print(f"Konto {a.login} utworzone. Administrator przy pierwszym logowaniu musi włączyć 2FA.")
            elif a.cmd == "reset-password":
                with engine.connect() as conn:
                    user = auth.get_user(conn, a.login)
                if not user:
                    sys.exit(f"Nie ma konta „{a.login}”. Lista kont: python -m denali users")
                pw = _ask_password(a.login)
                with engine.begin() as conn:
                    auth.set_password(conn, user["id"], pw)
                    if a.disable_2fa:
                        auth.disable_totp(conn, user["id"])
                    auth.audit(conn, "password_reset", user="konsola serwera", target=a.login,
                               params={"disable_2fa": a.disable_2fa})
                print(f"Hasło konta {a.login} zmienione, wszystkie sesje wylogowane"
                      + (", 2FA wyłączone." if a.disable_2fa else "."))
        except auth.AuthError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
