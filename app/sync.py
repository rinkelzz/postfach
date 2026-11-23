"""Abgleich der Mailkonten mit dem Server (IMAP) – der Server bleibt führend.

Ablauf je Konto:
1. Ordnerliste holen, neue Ordner anlegen, verschwundene entfernen, Rollen (Posteingang, Gesendet …) bestimmen.
2. Je Ordner die Nachrichten der letzten `sync_days` Tage suchen. Neue: Kopfzeilen holen. Bekannte: Markierungen
   (gelesen, markiert, beantwortet) abgleichen. Am Server gelöschte oder zu alte: lokal entfernen.
   Ändert sich die UIDVALIDITY eines Ordners, wird er lokal komplett neu aufgebaut.
3. Inhalte (Text/HTML, Anhangsliste) im Hintergrund nachladen – neueste zuerst, zeitlich begrenzt pro Durchlauf.
   Öffnet jemand eine Mail, deren Inhalt noch fehlt, wird er sofort geholt (load_body).

Pro Konto läuft immer nur ein Abgleich gleichzeitig. Fehler landen in accounts.last_error und stoppen nie die App.
"""
import sqlite3
import threading
import time
from datetime import timedelta
from typing import Dict, List, Optional

from . import crypto, imapclient, mailparse
from .db import connect, get_setting
from .timeutil import now_utc, now_utc_str, parse_utc, to_utc_str

BODY_TIME_BUDGET = 90        # Sekunden pro Durchlauf zum Nachladen von Inhalten
BODY_MAX_SIZE = 25 * 1024 * 1024  # größere Mails werden erst beim Öffnen geholt
MAX_PARALLEL = 3

_locks: Dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()
_running: Dict[int, float] = {}
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


class SyncError(Exception):
    pass


def _lock_for(account_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(account_id, threading.Lock())


def is_running(account_id: int) -> bool:
    return account_id in _running


# --- Verbindung --------------------------------------------------------------

def open_client(account: sqlite3.Row, password: Optional[str] = None) -> imapclient.Client:
    """Angemeldete IMAP-Verbindung für ein Konto (in Tests ersetzbar)."""
    if password is None:
        password = crypto.unpack_secrets(account["secret"])["imap"]
    if not password:
        raise SyncError("Für dieses Konto ist kein Passwort gespeichert (z. B. nach dem Wiederherstellen einer "
                        "Sicherung) – bitte unter Konten neu eintragen.")
    return imapclient.Client(account["imap_host"], account["imap_port"], account["imap_security"],
                             account["imap_user"], password)


# --- Ordner ------------------------------------------------------------------

def _sync_folders(conn: sqlite3.Connection, account_id: int, client) -> List[sqlite3.Row]:
    remote = client.list_folders()
    imapclient.assign_roles(remote)
    paths = set()
    for f in remote:
        paths.add(f["path"])
        conn.execute(
            "INSERT INTO folders(account_id, path, name, delimiter, role) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, path) DO UPDATE SET name = excluded.name, delimiter = excluded.delimiter, "
            "role = excluded.role",
            (account_id, f["path"], f["full_name"] if f["path"].upper() != "INBOX" else "Posteingang",
             f["delimiter"], f["role"]))
    for row in conn.execute("SELECT id, path FROM folders WHERE account_id = ?", (account_id,)).fetchall():
        if row["path"] not in paths:
            conn.execute("DELETE FROM folders WHERE id = ?", (row["id"],))
    rows = conn.execute("SELECT * FROM folders WHERE account_id = ?", (account_id,)).fetchall()
    return sorted(rows, key=lambda r: (imapclient.ROLE_ORDER.get(r["role"], 9), r["path"].lower()))


def _flags(flags: set) -> Dict[str, int]:
    return {"seen": int("\\seen" in flags), "flagged": int("\\flagged" in flags), "answered": int("\\answered" in flags)}


def _insert_headers(conn: sqlite3.Connection, account_id: int, folder_id: int, rec: Dict) -> None:
    lit = rec["literals"][0] if rec["literals"] else b""
    h = mailparse.parse_headers(lit, rec.get("internaldate", ""))
    f = _flags(rec["flags"])
    conn.execute(
        "INSERT OR IGNORE INTO messages(account_id, folder_id, uid, message_id, in_reply_to, refs, subject, from_name, "
        "from_addr, to_addrs, cc_addrs, reply_to, date_utc, size, seen, flagged, answered, has_attachments) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, folder_id, rec["uid"], h["message_id"], h["in_reply_to"], h["refs"], h["subject"][:500],
         h["from_name"][:200], h["from_addr"][:200], h["to_addrs"][:2000], h["cc_addrs"][:2000], h["reply_to"][:500],
         to_utc_str(h["date"]), rec["size"], f["seen"], f["flagged"], f["answered"], h["has_attachments"]))


def _update_counts(conn: sqlite3.Connection, folder_id: int) -> None:
    conn.execute("UPDATE folders SET total = (SELECT COUNT(*) FROM messages WHERE folder_id = ?), "
                 "unread = (SELECT COUNT(*) FROM messages WHERE folder_id = ? AND seen = 0), updated_at = ? WHERE id = ?",
                 (folder_id, folder_id, now_utc_str(), folder_id))


def sync_folder(conn: sqlite3.Connection, account: sqlite3.Row, folder: sqlite3.Row, client) -> Dict:
    uidvalidity, _exists = client.select(folder["path"], readonly=True)
    if folder["uidvalidity"] is not None and uidvalidity and folder["uidvalidity"] != uidvalidity:
        conn.execute("DELETE FROM messages WHERE folder_id = ?", (folder["id"],))  # Nummern gelten nicht mehr
    conn.execute("UPDATE folders SET uidvalidity = ? WHERE id = ?", (uidvalidity or None, folder["id"]))
    since = (now_utc() - timedelta(days=int(account["sync_days"] or 90))).date()
    remote = set(client.uids_since(since))
    local = {r[0] for r in conn.execute("SELECT uid FROM messages WHERE folder_id = ?", (folder["id"],))}
    gone = local - remote
    new = sorted(remote - local, reverse=True)  # neueste zuerst
    keep = sorted(local & remote)
    if gone:
        for chunk in imapclient._chunks(sorted(gone), 500):
            conn.execute(f"DELETE FROM messages WHERE folder_id = ? AND uid IN ({','.join('?' * len(chunk))})",
                         [folder["id"]] + chunk)
    if keep:
        flags = client.fetch_flags(keep)
        conn.execute("BEGIN")
        try:
            for uid, fl in flags.items():
                f = _flags(fl)
                conn.execute("UPDATE messages SET seen = ?, flagged = ?, answered = ? WHERE folder_id = ? AND uid = ? "
                             "AND (seen != ? OR flagged != ? OR answered != ?)",
                             (f["seen"], f["flagged"], f["answered"], folder["id"], uid,
                              f["seen"], f["flagged"], f["answered"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if new:
        for chunk in imapclient._chunks(new, imapclient.CHUNK):
            recs = client.fetch_headers(chunk)
            conn.execute("BEGIN")
            try:
                for rec in recs:
                    _insert_headers(conn, account["id"], folder["id"], rec)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    _update_counts(conn, folder["id"])
    return {"new": len(new), "gone": len(gone)}


# --- Inhalte -----------------------------------------------------------------

def store_body(conn: sqlite3.Connection, message_id: int, raw: bytes) -> Dict:
    body = mailparse.parse_body(raw)
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        for a in body["attachments"]:
            conn.execute("INSERT INTO attachments(message_id, idx, filename, mime, size, content_id, inline) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (message_id, a["idx"], a["filename"], a["mime"], a["size"], a["content_id"], a["inline"]))
        conn.execute("UPDATE messages SET body_text = ?, body_html = ?, search_text = ?, snippet = ?, has_attachments = ? "
                     "WHERE id = ?", (body["text"], body["html"] or None, body["search_text"][:200000], body["snippet"],
                                      body["has_attachments"], message_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return body


def _load_bodies(conn: sqlite3.Connection, account: sqlite3.Row, client, deadline: float) -> int:
    done = 0
    folders = conn.execute(
        "SELECT f.id, f.path, MAX(m.date_utc) AS newest FROM folders f JOIN messages m ON m.folder_id = f.id "
        "WHERE f.account_id = ? AND m.body_text IS NULL AND m.size <= ? "
        # Spam, Papierkorb und Gmails „Alle Nachrichten“ (Doppel) erst beim Öffnen laden
        "AND f.role NOT IN ('junk', 'trash', 'all') GROUP BY f.id ORDER BY "
        "CASE f.role WHEN 'inbox' THEN 0 WHEN 'sent' THEN 1 ELSE 2 END, newest DESC",
        (account["id"], BODY_MAX_SIZE)).fetchall()
    for folder in folders:
        if time.time() > deadline:
            break
        client.select(folder["path"], readonly=True)
        rows = conn.execute("SELECT id, uid FROM messages WHERE folder_id = ? AND body_text IS NULL AND size <= ? "
                            "ORDER BY date_utc DESC", (folder["id"], BODY_MAX_SIZE)).fetchall()
        for row in rows:
            if time.time() > deadline:
                break
            raw = client.fetch_message(row["uid"])
            if raw is None:
                conn.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                continue
            store_body(conn, row["id"], raw)
            done += 1
    return done


# --- Ganzes Konto ------------------------------------------------------------

def sync_account(account_id: int, bodies: bool = True, budget: float = BODY_TIME_BUDGET) -> Dict:
    """Ein Konto abgleichen. Läuft schon ein Abgleich für das Konto, passiert nichts."""
    lock = _lock_for(account_id)
    if not lock.acquire(blocking=False):
        return {"skipped": True}
    _running[account_id] = time.time()
    conn = connect()
    result = {"new": 0, "gone": 0, "bodies": 0, "error": None}
    try:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account or not account["active"]:
            return {"skipped": True}
        try:
            with open_client(account) as client:
                for folder in _sync_folders(conn, account_id, client):
                    try:
                        r = sync_folder(conn, account, folder, client)
                    except imapclient.ImapError as e:  # einzelner kaputter Ordner stoppt nicht das ganze Konto
                        print(f"[postfach] Konto {account_id}, Ordner {folder['path']}: {e}", flush=True)
                        continue
                    result["new"] += r["new"]
                    result["gone"] += r["gone"]
                if bodies:
                    result["bodies"] = _load_bodies(conn, account, client, time.time() + budget)
            conn.execute("UPDATE accounts SET last_sync = ?, last_error = NULL WHERE id = ?", (now_utc_str(), account_id))
        except Exception as e:  # noqa: BLE001 – Fehler gehören in die Anzeige, nicht in einen Absturz
            msg = imapclient.friendly_error(e) if not isinstance(e, (SyncError, crypto.CryptoError)) else str(e)
            result["error"] = msg
            conn.execute("UPDATE accounts SET last_error = ?, last_sync = ? WHERE id = ?",
                         (msg[:500], now_utc_str(), account_id))
        return result
    finally:
        conn.close()
        _running.pop(account_id, None)
        lock.release()


def start_sync(account_id: int) -> bool:
    """Abgleich im Hintergrund starten. False, wenn er schon läuft."""
    if is_running(account_id):
        return False

    def run():
        with _slots:
            sync_account(account_id)
    threading.Thread(target=run, name=f"postfach-sync-{account_id}", daemon=True).start()
    return True


def run_due() -> List[int]:
    """Vom Zeitplan aufgerufen: alle Konten abgleichen, deren letzter Abruf länger als das Intervall her ist."""
    conn = connect()
    try:
        minutes = int(get_setting(conn, "sync_interval") or 5)
        limit = now_utc() - timedelta(minutes=minutes) + timedelta(seconds=20)
        due = [r["id"] for r in conn.execute("SELECT id, last_sync FROM accounts WHERE active = 1 AND secret != ''")
               if not r["last_sync"] or parse_utc(r["last_sync"]) <= limit]
    finally:
        conn.close()
    started = []
    for account_id in due:
        if start_sync(account_id):
            started.append(account_id)
    return started


# --- Einzelne Nachricht (beim Öffnen) ----------------------------------------

def load_body(conn: sqlite3.Connection, account: sqlite3.Row, message: sqlite3.Row,
              mark_seen: bool = False) -> Optional[bytes]:
    """Inhalt sofort vom Server holen (und auf Wunsch gleich als gelesen markieren). Liefert die Rohdaten."""
    folder = conn.execute("SELECT * FROM folders WHERE id = ?", (message["folder_id"],)).fetchone()
    with open_client(account) as client:
        client.select(folder["path"], readonly=not mark_seen)
        raw = client.fetch_message(message["uid"])
        if raw is not None and mark_seen and not message["seen"]:
            client.store_flags([message["uid"]], "\\Seen", True)
    if raw is None:
        conn.execute("DELETE FROM messages WHERE id = ?", (message["id"],))
        _update_counts(conn, folder["id"])
        return None
    store_body(conn, message["id"], raw)
    if mark_seen and not message["seen"]:
        conn.execute("UPDATE messages SET seen = 1 WHERE id = ?", (message["id"],))
        _update_counts(conn, folder["id"])
    return raw


def fetch_raw(conn: sqlite3.Connection, account: sqlite3.Row, message: sqlite3.Row) -> Optional[bytes]:
    folder = conn.execute("SELECT path FROM folders WHERE id = ?", (message["folder_id"],)).fetchone()
    with open_client(account) as client:
        client.select(folder["path"], readonly=True)
        return client.fetch_message(message["uid"])


def set_flags(conn: sqlite3.Connection, account: sqlite3.Row, message: sqlite3.Row, changes: Dict[str, bool],
              force: bool = False) -> None:
    """Markierungen am Server und lokal setzen (seen, flagged, answered).
    force: auch senden, wenn lokal schon so markiert (z. B. lokal vorab als gelesen gesetzt)."""
    names = {"seen": "\\Seen", "flagged": "\\Flagged", "answered": "\\Answered"}
    folder = conn.execute("SELECT * FROM folders WHERE id = ?", (message["folder_id"],)).fetchone()
    todo = {k: v for k, v in changes.items() if k in names and (force or bool(message[k]) != bool(v))}
    if not todo:
        return
    with open_client(account) as client:
        client.select(folder["path"], readonly=False)
        for k, v in todo.items():
            client.store_flags([message["uid"]], names[k], v)
    for k, v in todo.items():
        conn.execute(f"UPDATE messages SET {k} = ? WHERE id = ?", (int(v), message["id"]))
    _update_counts(conn, folder["id"])
