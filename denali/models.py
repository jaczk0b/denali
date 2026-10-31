"""Schemat bazy (SQLAlchemy Core). Zmiany schematu idą przez migracje Alembic w denali/migrations."""
from sqlalchemy import JSON, Boolean, Column, Float, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

# Ustawienia i drobny stan systemu (np. ostatni sygnał życia workera). Czas jako znacznik UNIX.
setting = Table(
    "setting", metadata,
    Column("key", String(100), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)

# ───────────────────────── konta i bezpieczeństwo ─────────────────────────
app_user = Table(
    "app_user", metadata,
    Column("id", Integer, primary_key=True),
    Column("login", String(64), nullable=False, unique=True),
    Column("pw_hash", String(255), nullable=False),
    Column("role", String(16), nullable=False),                 # admin | podglad
    Column("totp_ct", Text),                                     # sekret TOTP, zaszyfrowany; NULL = 2FA wyłączone
    Column("totp_pending_ct", Text),                             # sekret w trakcie włączania 2FA
    Column("totp_last_step", Integer, nullable=False, default=0),  # ostatni użyty krok czasu — kod nie zadziała drugi raz
    Column("backup_codes", JSON, nullable=False, default=list),  # skróty SHA-256 niewykorzystanych kodów zapasowych
    Column("disabled", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
    Column("pw_changed_at", Float, nullable=False),
)

# Sesje po stronie serwera: w ciasteczku jest losowy token, w bazie tylko jego skrót.
session = Table(
    "session", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("public_id", String(16), nullable=False, unique=True),  # do listy sesji i wylogowania pojedynczej
    Column("user_id", Integer, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("kind", String(16), nullable=False),                    # zwykla | kiosk | pending (po haśle, przed 2FA)
    Column("csrf", String(64), nullable=False),
    Column("ip", String(64), nullable=False, default=""),
    Column("user_agent", String(300), nullable=False, default=""),
    Column("created_at", Float, nullable=False),
    Column("last_seen_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("reauth_at", Float),                                     # ostatnie ponowne potwierdzenie hasłem i kodem
)

login_attempt = Table(
    "login_attempt", metadata,
    Column("id", Integer, primary_key=True),
    Column("login", String(64), nullable=False, index=True),
    Column("ip", String(64), nullable=False, index=True),
    Column("ts", Float, nullable=False),
    Column("ok", Boolean, nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", Float, nullable=False, index=True),
    Column("user", String(64), nullable=False),
    Column("ip", String(64), nullable=False, default=""),
    Column("action", String(64), nullable=False),
    Column("target", String(200), nullable=False, default=""),
    Column("params", JSON, nullable=False, default=dict),
    Column("result", String(200), nullable=False, default="ok"),
)

# ───────────────────────── infrastruktura ─────────────────────────
# Profil = ustawienia na poziomie typu (zasada „minimum ustawień na pojedynczym urządzeniu”).
profile = Table(
    "profile", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), nullable=False, unique=True),
    Column("kinds", JSON, nullable=False, default=list),         # typy urządzeń, które dostają ten profil same
    Column("settings", JSON, nullable=False, default=dict),      # tylko klucze różne od domyślnych
    Column("builtin", Boolean, nullable=False, default=False),
)

device = Table(
    "device", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("role", String(300), nullable=False, default=""),     # do czego służy
    Column("criticality", Integer, nullable=False, default=3),   # 1 = P1 (krytyczne) … 3 = P3
    Column("parent_id", Integer, ForeignKey("device.id", ondelete="SET NULL"), index=True),  # na czym działa
    Column("profile_id", Integer, ForeignKey("profile.id", ondelete="SET NULL")),  # NULL = profil wg typu
    Column("place", String(200), nullable=False, default=""),
    Column("serial", String(100), nullable=False, default=""),
    Column("notes_md", Text, nullable=False, default=""),
    Column("icon", String(64), nullable=False, default=""),
    Column("app_url", String(500), nullable=False, default=""),
    Column("pulpit_group", String(16), nullable=False, default="brak"),  # codzien | zarzadzanie | brak
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

device_address = Table(
    "device_address", metadata,
    Column("id", Integer, primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("kind", String(16), nullable=False),                  # ip | mac | hostname
    Column("value", String(255), nullable=False),
    Column("source", String(16), nullable=False, default="manual"),  # manual | integracja
    Column("is_primary", Boolean, nullable=False, default=False),
    UniqueConstraint("device_id", "kind", "value"),
)

dependency = Table(
    "dependency", metadata,
    Column("id", Integer, primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("depends_on_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("type", String(16), nullable=False),                  # zasilanie | siec | dane | usluga
    Column("hard", Boolean, nullable=False, default=True),       # twarda: bez tamtego nie działa; miękka: działa gorzej
    Column("note", String(300), nullable=False, default=""),
    Column("source", String(16), nullable=False, default="manual"),
    UniqueConstraint("device_id", "depends_on_id", "type"),
)

# Kabel: strona A to strona bliżej routera (switch, router), strona B to wpięte urządzenie.
cable = Table(
    "cable", metadata,
    Column("id", Integer, primary_key=True),
    Column("a_device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("a_port", String(40), nullable=False, default=""),
    Column("b_device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("b_port", String(40), nullable=False, default=""),
    Column("label", String(200), nullable=False, default=""),
)

# Szafa (rack) i co w niej stoi. Pozycja może być urządzeniem albo samym opisem (patch panel, półka, zaślepka).
rack = Table(
    "rack", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), nullable=False, unique=True),
    Column("units", Integer, nullable=False, default=42),       # wysokość w U
    Column("place", String(200), nullable=False, default=""),
    Column("notes", String(500), nullable=False, default=""),
)

rack_item = Table(
    "rack_item", metadata,
    Column("id", Integer, primary_key=True),
    Column("rack_id", Integer, ForeignKey("rack.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), unique=True),  # NULL = sam opis
    Column("label", String(200), nullable=False, default=""),  # opis pozycji bez urządzenia albo dopisek
    Column("u", Integer, nullable=False),                       # najniższe zajęte U (1 = dół szafy)
    Column("height", Integer, nullable=False, default=1),       # ile U zajmuje
    Column("back", Boolean, nullable=False, default=False),     # montaż z tyłu (np. listwa, switch od tyłu)
    Column("shelf", Boolean, nullable=False, default=False),    # półka: zajmuje U, stoją na niej inne pozycje
    Column("shelf_id", Integer, ForeignKey("rack_item.id", ondelete="CASCADE"), index=True),  # stoi na tej półce
)

device_override = Table(
    "device_override", metadata,
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), primary_key=True),
    Column("key", String(64), primary_key=True),
    Column("value", JSON, nullable=False),
)

# ───────────────────────── integracje ─────────────────────────
integration = Table(
    "integration", metadata,
    Column("id", Integer, primary_key=True),
    Column("type", String(32), nullable=False),                  # proxmox | pbs | truenas | komodo | openwrt | tplink | agent | checks
    Column("name", String(100), nullable=False, unique=True),
    Column("config", JSON, nullable=False, default=dict),        # bez sekretów
    Column("secret_ct", Text),                                   # sekrety, zaszyfrowane
    Column("enabled", Boolean, nullable=False, default=True),
    Column("poll_seconds", Integer, nullable=False, default=60),
    Column("last_ok_at", Float),
    Column("last_attempt_at", Float),
    Column("last_error", String(500), nullable=False, default=""),
)

# Obiekt z integracji (VM, stack, udział, klient sieci) przypięty do urządzenia. Nowe czekają na zatwierdzenie.
external_ref = Table(
    "external_ref", metadata,
    Column("id", Integer, primary_key=True),
    Column("integration_id", Integer, ForeignKey("integration.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(200), nullable=False),          # np. qemu/107, stack/immich, mac/aa:bb:…
    Column("device_id", Integer, ForeignKey("device.id", ondelete="SET NULL"), index=True),
    Column("status", String(16), nullable=False, default="nowy"),  # nowy | zatwierdzony | ignorowany
    Column("proposed", JSON, nullable=False, default=dict),      # co integracja proponuje: nazwa, typ, adresy, rodzic…
    Column("first_seen", Float, nullable=False),
    Column("last_seen", Float, nullable=False),
    UniqueConstraint("integration_id", "external_id"),
)

# ───────────────────────── dokumentacja ─────────────────────────
runbook = Table(
    "runbook", metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(200), nullable=False),
    Column("body_md", Text, nullable=False, default=""),
    Column("updated_at", Float, nullable=False),
    Column("updated_by", String(64), nullable=False, default=""),
)

runbook_device = Table(
    "runbook_device", metadata,
    Column("runbook_id", Integer, ForeignKey("runbook.id", ondelete="CASCADE"), primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), primary_key=True, index=True),
)

# Procedura = instrukcja krok po kroku na awarię („padł pve”) albo na panel („jak odtworzyć Denali”).
procedure = Table(
    "procedure", metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(200), nullable=False),
    Column("scope", String(16), nullable=False, default="infrastruktura"),  # infrastruktura | panel
    Column("intro_md", Text, nullable=False, default=""),
    Column("trigger", String(100), nullable=False, default=""),  # device_down:<nazwa> | device_down:typ:<typ>
    Column("last_tested_at", Float),
    Column("updated_at", Float, nullable=False),
    Column("updated_by", String(64), nullable=False, default=""),
)

procedure_step = Table(
    "procedure_step", metadata,
    Column("id", Integer, primary_key=True),
    Column("procedure_id", Integer, ForeignKey("procedure.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("title", String(200), nullable=False),
    Column("body_md", Text, nullable=False, default=""),
)

procedure_device = Table(
    "procedure_device", metadata,
    Column("procedure_id", Integer, ForeignKey("procedure.id", ondelete="CASCADE"), primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), primary_key=True, index=True),
)

contact = Table(
    "contact", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("phone", String(40), nullable=False, default=""),
    Column("note", String(300), nullable=False, default=""),
)

procedure_contact = Table(
    "procedure_contact", metadata,
    Column("procedure_id", Integer, ForeignKey("procedure.id", ondelete="CASCADE"), primary_key=True),
    Column("contact_id", Integer, ForeignKey("contact.id", ondelete="CASCADE"), primary_key=True),
)

# Plik leży na dysku pod losową nazwą (data_dir/files/…); w bazie tylko opis.
file = Table(
    "file", metadata,
    Column("id", Integer, primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("mime", String(100), nullable=False),
    Column("size", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("path", String(100), nullable=False, unique=True),
    Column("uploaded_at", Float, nullable=False),
    Column("uploaded_by", String(64), nullable=False, default=""),
)

# Skarbiec: tylko admin; hasło zaszyfrowane, odsłonięcie po ponownym potwierdzeniu i z wpisem w audycie.
vault_secret = Table(
    "vault_secret", metadata,
    Column("id", Integer, primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), index=True),
    Column("label", String(200), nullable=False),
    Column("username", String(200), nullable=False, default=""),
    Column("ciphertext", Text, nullable=False),
    Column("note", String(500), nullable=False, default=""),
    Column("updated_at", Float, nullable=False),
    Column("updated_by", String(64), nullable=False, default=""),
)

# ───────────────────────── incydenty (log problemów) ─────────────────────────
incident = Table(
    "incident", metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(200), nullable=False),
    Column("root_device_id", Integer, ForeignKey("device.id", ondelete="SET NULL"), index=True),
    Column("status", String(16), nullable=False, default="trwa"),     # trwa | potwierdzony | zamkniety
    Column("severity", String(16), nullable=False, default="ostrzezenie"),  # krytyczny | ostrzezenie | info
    Column("source", String(16), nullable=False, default="reczny"),   # auto | reczny | hlc
    Column("started_at", Float, nullable=False, index=True),
    Column("ended_at", Float),
    Column("acked_by", String(64), nullable=False, default=""),
    Column("cause_md", Text, nullable=False, default=""),
    Column("fix_md", Text, nullable=False, default=""),
    Column("notified_at", Float),                                     # powiadomienie o awarii wysłane (albo pominięte)
    Column("closed_notified_at", Float),                              # powiadomienie „rozwiązane” wysłane
)

incident_device = Table(
    "incident_device", metadata,
    Column("incident_id", Integer, ForeignKey("incident.id", ondelete="CASCADE"), primary_key=True),
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), primary_key=True, index=True),
    Column("role", String(16), nullable=False),                       # przyczyna | skutek | gorzej
)

incident_event = Table(
    "incident_event", metadata,
    Column("id", Integer, primary_key=True),
    Column("incident_id", Integer, ForeignKey("incident.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("ts", Float, nullable=False),
    Column("kind", String(32), nullable=False),     # otwarty | dowod | skutek | notatka | potwierdzony | zamkniety | wznowiony
    Column("text", Text, nullable=False),
    Column("user", String(64), nullable=False, default=""),
)

# Ostatni znany stan obiektu z integracji (bez historii — tę mają same systemy źródłowe).
observed_state = Table(
    "observed_state", metadata,
    Column("integration_id", Integer, ForeignKey("integration.id", ondelete="CASCADE"), primary_key=True),
    Column("external_id", String(200), primary_key=True),
    Column("state", String(16), nullable=False),            # ok | warn | down | off | unknown
    Column("summary", String(300), nullable=False, default=""),
    Column("data", JSON, nullable=False, default=dict),
    Column("observed_at", Float, nullable=False),
)

# Sprawdzenia z LAN wyliczane same z adresów i linków urządzeń (bez konfiguracji na urządzeniu).
check_state = Table(
    "check_state", metadata,
    Column("device_id", Integer, ForeignKey("device.id", ondelete="CASCADE"), primary_key=True),
    Column("kind", String(16), primary_key=True),             # ping | http
    Column("target", String(500), primary_key=True),
    Column("ok", Boolean, nullable=False),
    Column("summary", String(300), nullable=False, default=""),
    Column("ms", Float),
    Column("cert_expires_at", Float),
    Column("first_fail_at", Float),                          # od kiedy nie odpowiada (do „po ilu sekundach awaria”)
    Column("last_ok_at", Float),
    Column("checked_at", Float, nullable=False),
)

# Kanały powiadomień (na razie Discord). Adres webhooka to sekret — zaszyfrowany.
notification_channel = Table(
    "notification_channel", metadata,
    Column("id", Integer, primary_key=True),
    Column("type", String(16), nullable=False),                 # discord
    Column("name", String(100), nullable=False),
    Column("config_ct", Text, nullable=False),
    Column("min_severity", String(16), nullable=False, default="ostrzezenie"),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("last_sent_at", Float),
    Column("last_error", String(300), nullable=False, default=""),
)

# Dostępne aktualizacje: obiekt z integracji (VM przez agenta, węzeł Proxmoxa, stack Komodo) → co można zaktualizować.
# Każdy odczyt źródła zastępuje jego listę; detected_at zostaje, dopóki wersja docelowa się nie zmieni.
update_item = Table(
    "update_item", metadata,
    Column("integration_id", Integer, ForeignKey("integration.id", ondelete="CASCADE"), primary_key=True),
    Column("external_id", String(200), primary_key=True),
    Column("component", String(200), primary_key=True),          # pakiet, usługa stacka, system
    Column("kind", String(16), nullable=False, default="pakiet"),  # pakiet | obraz | system
    Column("current", String(100), nullable=False, default=""),
    Column("available", String(100), nullable=False, default=""),
    Column("security", Boolean, nullable=False, default=False),
    Column("detected_at", Float, nullable=False),
)
