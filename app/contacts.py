"""Adressbuch: Kontakte je Benutzer – von Hand gepflegt und automatisch aus Mails gesammelt.

Automatisch aufgenommen werden
- Empfänger eigener Mails (beim Senden aus Postfach und aus dem „Gesendet“-Ordner) – zählen am meisten,
- Absender von Mails im Posteingang.
Nicht aufgenommen werden technische Absender (noreply, mailer-daemon, bounce …), die eigenen Adressen und gelöschte
Kontakte (die bleiben als „ausgeblendet“ gemerkt, damit sie nicht wiederkommen). Von Hand gepflegte Namen werden nie
automatisch überschrieben.
"""
import re
import sqlite3
from email.utils import getaddresses
from typing import Iterable, List, Tuple

from .mailer import valid_email
from .timeutil import now_utc_str

TECHNICAL = re.compile(r"(^|[._+-])(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounces?|notifications?|"
                       r"newsletter|news|info-?mail|automated|auto-?confirm)([._+-]|@|$)", re.I)


def is_technical(addr: str) -> bool:
    local = addr.split("@")[0]
    return bool(TECHNICAL.search(local + "@")) or local.lower() in ("noreply", "no-reply", "mailer-daemon", "postmaster")


def _own(conn: sqlite3.Connection, owner_id: int) -> set:
    return {r[0].lower() for r in conn.execute("SELECT email FROM accounts WHERE owner_id = ?", (owner_id,))}


def collect(conn: sqlite3.Connection, owner_id: int, pairs: Iterable[Tuple[str, str]], kind: str, when: str = "") -> int:
    """Adressen aufnehmen bzw. zählen. kind: sent | received. Liefert die Zahl neu angelegter Kontakte."""
    own = _own(conn, owner_id)
    col = "sent_count" if kind == "sent" else "received_count"
    when = when or now_utc_str()
    added = 0
    seen = set()
    for name, addr in pairs:
        addr = (addr or "").strip().lower()
        if not valid_email(addr) or addr in own or addr in seen or is_technical(addr):
            continue
        seen.add(addr)
        name = re.sub(r"[\r\n\t\"]+", " ", name or "").strip()[:120]
        if name.lower() == addr:
            name = ""
        cur = conn.execute(f"UPDATE contacts SET {col} = {col} + 1, last_used = MAX(COALESCE(last_used, ''), ?), "
                           "name = CASE WHEN manual = 0 AND ? != '' THEN ? ELSE name END "
                           "WHERE owner_id = ? AND email = ? AND hidden = 0", (when, name, name, owner_id, addr))
        if cur.rowcount:
            continue
        if conn.execute("SELECT 1 FROM contacts WHERE owner_id = ? AND email = ?", (owner_id, addr)).fetchone():
            continue  # ausgeblendet
        conn.execute(f"INSERT INTO contacts(owner_id, email, name, {col}, last_used, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                     (owner_id, addr, name, when, now_utc_str()))
        added += 1
    return added


def parse(value: str) -> List[Tuple[str, str]]:
    return [(n, a) for n, a in getaddresses([value or ""]) if a]


def suggest(conn: sqlite3.Connection, owner_id: int, q: str, limit: int = 8) -> List[dict]:
    q = (q or "").strip().lower()
    if not q:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    start = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        "SELECT email, name, company, manual FROM contacts WHERE owner_id = ? AND hidden = 0 AND "
        "(email LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\' OR lower(company) LIKE ? ESCAPE '\\') "
        "ORDER BY (email LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\') DESC, manual DESC, "
        "sent_count * 3 + received_count DESC, last_used DESC LIMIT ?",
        (owner_id, like, like, like, start, start, limit)).fetchall()
    return [dict(r) for r in rows]
