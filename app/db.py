"""SQLite-Datenbank: Schema, schrittweise Migrationen (PRAGMA user_version) und Verbindungen.

Neue Tabellen oder Spalten kommen IMMER als neuer Migrationsschritt dazu – bestehende Schritte
werden nie verändert. So kommt jede Datenbank (auch eine alte Sicherung) Schritt für Schritt
auf den aktuellen Stand, ohne dass Daten verloren gehen.
"""
import secrets
import sqlite3
from typing import Callable, Dict, Iterator

from .config import settings
from .timeutil import now_utc_str


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _run(conn: sqlite3.Connection, script: str) -> None:
    """Mehrere Anweisungen einzeln ausführen (executescript würde die laufende Transaktion beenden)."""
    for stmt in script.split(";\n"):
        if stmt.strip():
            conn.execute(stmt)


def _m1_basis(conn: sqlite3.Connection) -> None:
    """Version 0.1: Benutzer, Sitzungen, Änderungsprotokoll, Einstellungen."""
    _run(conn, """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    email         TEXT,
    password_hash TEXT,                       -- NULL = Anmeldung nur mit DSM-Konto
    is_admin      INTEGER NOT NULL DEFAULT 0, -- Admin: Benutzer, Einstellungen, Konten für andere einrichten
    active        INTEGER NOT NULL DEFAULT 1,
    theme         TEXT,                       -- eigenes Design, NULL = Standard der Firma
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor_id  INTEGER,
    action    TEXT NOT NULL,
    target    TEXT,
    details   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
""")


def _m2_mail(conn: sqlite3.Connection) -> None:
    """Version 0.1: Mailkonten, Ordner, Nachrichten (Zwischenspeicher) und Anhänge (nur Beschreibung)."""
    _run(conn, """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,                 -- Anzeigename des Kontos, z. B. „Praxis“
    email          TEXT NOT NULL,
    from_name      TEXT NOT NULL DEFAULT '',      -- Absendername
    imap_host      TEXT NOT NULL,
    imap_port      INTEGER NOT NULL DEFAULT 993,
    imap_security  TEXT NOT NULL DEFAULT 'ssl',   -- ssl | starttls
    imap_user      TEXT NOT NULL,
    smtp_host      TEXT NOT NULL DEFAULT '',
    smtp_port      INTEGER NOT NULL DEFAULT 587,
    smtp_security  TEXT NOT NULL DEFAULT 'starttls', -- starttls | ssl | none
    smtp_user      TEXT NOT NULL DEFAULT '',      -- leer = wie IMAP
    secret         TEXT NOT NULL DEFAULT '',      -- verschlüsselte Passwörter (siehe crypto.py), nie in Sicherungen
    signature      TEXT NOT NULL DEFAULT '',
    sync_days      INTEGER NOT NULL DEFAULT 90,   -- so viele Tage werden zwischengespeichert
    color          TEXT NOT NULL DEFAULT '#0f766e',
    active         INTEGER NOT NULL DEFAULT 1,
    sort           INTEGER NOT NULL DEFAULT 0,
    created_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL,
    last_sync      TEXT,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_id);

CREATE TABLE IF NOT EXISTS folders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,                  -- Name am Server (modified UTF-7)
    name         TEXT NOT NULL,                  -- lesbarer Name
    delimiter    TEXT NOT NULL DEFAULT '/',
    role         TEXT NOT NULL DEFAULT '',       -- inbox | sent | drafts | trash | junk | archive | ''
    uidvalidity  INTEGER,
    unread       INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT,
    UNIQUE (account_id, path)
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    folder_id        INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    uid              INTEGER NOT NULL,
    message_id       TEXT NOT NULL DEFAULT '',
    in_reply_to      TEXT NOT NULL DEFAULT '',
    refs             TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    from_name        TEXT NOT NULL DEFAULT '',
    from_addr        TEXT NOT NULL DEFAULT '',
    to_addrs         TEXT NOT NULL DEFAULT '',   -- „Name <adresse>, …“
    cc_addrs         TEXT NOT NULL DEFAULT '',
    reply_to         TEXT NOT NULL DEFAULT '',
    date_utc         TEXT NOT NULL,
    size             INTEGER NOT NULL DEFAULT 0,
    seen             INTEGER NOT NULL DEFAULT 0,
    flagged          INTEGER NOT NULL DEFAULT 0,
    answered         INTEGER NOT NULL DEFAULT 0,
    has_attachments  INTEGER NOT NULL DEFAULT 0,
    snippet          TEXT NOT NULL DEFAULT '',
    body_text        TEXT,                       -- NULL = Inhalt noch nicht geladen
    body_html        TEXT,
    search_text      TEXT NOT NULL DEFAULT '',   -- Klartext für die Suche (auch bei reinen HTML-Mails)
    UNIQUE (folder_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_folder_date ON messages(folder_id, date_utc);
CREATE INDEX IF NOT EXISTS idx_messages_account_date ON messages(account_id, date_utc);
CREATE INDEX IF NOT EXISTS idx_messages_nobody ON messages(account_id, body_text) WHERE body_text IS NULL;

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,               -- Reihenfolge in der Mail (Inhalt wird beim Herunterladen vom Server geholt)
    filename    TEXT NOT NULL DEFAULT '',
    mime        TEXT NOT NULL DEFAULT 'application/octet-stream',
    size        INTEGER NOT NULL DEFAULT 0,
    content_id  TEXT NOT NULL DEFAULT '',
    inline      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
""")


def _m3_regeln(conn: sqlite3.Connection) -> None:
    """Version 0.3: Regeln für neue Mails im Posteingang."""
    _run(conn, """
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,   -- NULL = alle eigenen Konten
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    match_all   INTEGER NOT NULL DEFAULT 1,     -- 1 = alle Bedingungen, 0 = eine reicht
    conditions  TEXT NOT NULL DEFAULT '[]',     -- JSON: [{field, op, value}]
    actions     TEXT NOT NULL DEFAULT '[]',     -- JSON: [{type: move|forward|trash, folder?, to?}]
    stop        INTEGER NOT NULL DEFAULT 0,     -- danach keine weiteren Regeln
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit    TEXT,
    last_error  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_owner ON rules(owner_id, position)
""")
    if "rules_done" not in _columns(conn, "messages"):
        # 1 = Regeln schon angewendet bzw. nicht nötig (alles, was vor dieser Version da war, und der erste Abruf)
        conn.execute("ALTER TABLE messages ADD COLUMN rules_done INTEGER NOT NULL DEFAULT 1")


# Version → Schritt. Nur anhängen, nie ändern!
MIGRATIONS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _m1_basis,
    2: _m2_mail,
    3: _m3_regeln,
}
SCHEMA_VERSION = max(MIGRATIONS)

DEFAULT_SETTINGS = {
    "company_name": "",
    "theme_default": "classic",
    # Öffentliche Adresse (Reverse Proxy) fürs DSM-Fenster; leer = automatisch
    "public_url": "",
    # Anmeldung mit DSM-Konten
    "dsm_login": "1",        # Login-Formular prüft auch DSM-Passwörter
    "dsm_auto_create": "1",  # unbekannte DSM-Benutzer beim ersten Login anlegen
    "dsm_admins": "1",       # DSM-Administratoren werden Admin
    "dsm_url": "http://127.0.0.1:5000",
    # Abruf
    "sync_interval": "5",    # Minuten zwischen zwei Abrufen aller Konten
    "users_add_accounts": "1",  # Benutzer dürfen selbst Konten anlegen (sonst nur der Admin)
    # Datensicherung: jede Nacht, lokal + optional in einen freigegebenen Ordner
    "backup_enabled": "1",
    "backup_hour": "2",
    "backup_keep_days": "30",
    "backup_dir": "",              # z. B. /volume1/backup/postfach
}


class MigrationError(RuntimeError):
    pass


def migrate(conn: sqlite3.Connection) -> int:
    """Bringt die Datenbank Schritt für Schritt auf SCHEMA_VERSION. Liefert die Ausgangsversion.

    Jeder Schritt läuft in einer eigenen Transaktion und setzt user_version erst am Ende – bricht
    ein Schritt ab, bleibt die Datenbank auf dem letzten vollständigen Stand."""
    start = conn.execute("PRAGMA user_version").fetchone()[0]
    if start > SCHEMA_VERSION:
        raise MigrationError(
            f"Die Datenbank stammt aus einer neueren Version (Schema {start}, diese App kennt {SCHEMA_VERSION}). "
            "Bitte die neueste Version des Pakets installieren.")
    for version in range(start + 1, SCHEMA_VERSION + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            MIGRATIONS[version](conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return start


def connect() -> sqlite3.Connection:
    # Autocommit: jede Änderung ist sofort gespeichert (FastAPI räumt get_db() erst nach dem Senden auf).
    # check_same_thread=False: FastAPI öffnet die Verbindung u. U. in einem anderen Thread als dem des
    # Endpunkts. Jede Verbindung gehört trotzdem nur einem Request.
    conn = sqlite3.connect(str(settings.db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI-Dependency: eine Verbindung pro Request (Autocommit)."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Legt Datenordner, Schema und (beim ersten Start) den Admin-Zugang an."""
    from .security import hash_password  # vermeidet Import-Zyklus

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        migrate(conn)
        first_start = not conn.execute("SELECT 1 FROM app_settings LIMIT 1").fetchone()
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)", (key, value))
        if first_start and settings.initial_company.strip():
            set_setting(conn, "company_name", settings.initial_company.strip()[:80])

        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            password = settings.initial_admin_password or secrets.token_urlsafe(12)
            conn.execute(
                "INSERT INTO users(username, display_name, password_hash, is_admin, created_at) "
                "VALUES ('admin', 'Administrator', ?, 1, ?)",
                (hash_password(password), now_utc_str()),
            )
            if not settings.initial_admin_password:
                cred_file = settings.data_dir / "ERSTES_ADMIN_PASSWORT.txt"
                cred_file.write_text(
                    "Benutzer: admin\nPasswort: " + password + "\n\n"
                    "Bitte nach dem ersten Login ändern und diese Datei löschen.\n",
                    encoding="utf-8",
                )
                print(f"[postfach] Admin angelegt, Passwort steht in {cred_file}", flush=True)
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def audit(conn: sqlite3.Connection, actor_id, action: str, target: str = "", details: str = "") -> None:
    """Eintrag ins Änderungsprotokoll. Einträge werden nie geändert oder gelöscht."""
    conn.execute(
        "INSERT INTO audit_log(ts, actor_id, action, target, details) VALUES (?, ?, ?, ?, ?)",
        (now_utc_str(), actor_id, action, target or "", (details or "")[:2000]),
    )
