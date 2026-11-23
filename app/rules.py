"""Regeln für neue Mails im Posteingang (wie Outlook-Regeln, aber auf der NAS).

- Gelten nur für Mails, die NEU im Posteingang ankommen. Beim allerersten Abruf eines Kontos (und wenn ein Ordner neu
  aufgebaut wird) werden keine Regeln angewendet – sonst würde z. B. eine Weiterleitungsregel 90 Tage Post verschicken.
  Für bereits vorhandene Mails gibt es „Jetzt anwenden“ (ohne Weiterleiten).
- Bedingungen: Absender, Empfänger (An/Kopie), Betreff, Text, hat Anhang – mit „enthält“, „enthält nicht“, „ist gleich“,
  „endet auf“ (z. B. „@kunde.de“). Groß-/Kleinschreibung zählt nicht. Alle oder eine der Bedingungen.
- Aktionen: weiterleiten (an eine Adresse, über den Postausgang des Kontos), verschieben (in einen Ordner des Kontos),
  in den Papierkorb. Weiterleiten kommt immer zuerst, dann Verschieben bzw. Papierkorb.
- Reihenfolge: Regeln laufen nach Position; „danach keine weiteren Regeln“ beendet die Auswertung für diese Mail.
  Ist eine Mail verschoben oder gelöscht, laufen keine weiteren Regeln mehr.
- Schutz vor Mail-Schleifen: Automatische Mails (Auto-Submitted, Abwesenheitsnotizen, Rückläufer, Listen-Bounces)
  und schon einmal von Postfach weitergeleitete Mails werden nie weitergeleitet, nie an die eigene Adresse,
  höchstens FORWARD_LIMIT Weiterleitungen pro Konto und Stunde.
"""
import email
import email.policy
import json
import re
import sqlite3
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional

from . import compose, mailparse
from .mailer import valid_email
from .timeutil import now_utc_str

FIELDS = {"from": "Absender", "to": "Empfänger (An/Kopie)", "subject": "Betreff", "body": "Text", "attachment": "hat Anhang"}
OPS = {"contains": "enthält", "not_contains": "enthält nicht", "is": "ist gleich", "ends_with": "endet auf"}
ACTIONS = {"move": "Verschieben nach", "forward": "Weiterleiten an", "trash": "In den Papierkorb"}
MAX_CONDITIONS, MAX_ACTIONS = 10, 5
FORWARD_LIMIT = 50                    # pro Konto und Stunde
FORWARD_HEADER = "X-Postfach-Forwarded"

_forwards: Dict[int, deque] = defaultdict(deque)


class RuleError(ValueError):
    pass


# --- Prüfen beim Speichern -----------------------------------------------------

def validate(conditions: List[Dict], actions: List[Dict], account_id: Optional[int], own_addresses: set) -> None:
    if not conditions:
        raise RuleError("Bitte mindestens eine Bedingung angeben")
    if len(conditions) > MAX_CONDITIONS:
        raise RuleError(f"Höchstens {MAX_CONDITIONS} Bedingungen")
    for c in conditions:
        if c.get("field") not in FIELDS:
            raise RuleError("Unbekanntes Feld in einer Bedingung")
        if c["field"] == "attachment":
            continue
        if c.get("op") not in OPS:
            raise RuleError("Unbekannte Bedingung")
        if not str(c.get("value") or "").strip():
            raise RuleError(f"Bitte bei „{FIELDS[c['field']]}“ einen Wert eintragen")
        if len(str(c["value"])) > 200:
            raise RuleError("Wert zu lang (höchstens 200 Zeichen)")
    if not actions:
        raise RuleError("Bitte mindestens eine Aktion angeben")
    if len(actions) > MAX_ACTIONS:
        raise RuleError(f"Höchstens {MAX_ACTIONS} Aktionen")
    kinds = [a.get("type") for a in actions]
    if any(k not in ACTIONS for k in kinds):
        raise RuleError("Unbekannte Aktion")
    if kinds.count("move") + kinds.count("trash") > 1:
        raise RuleError("Eine Mail kann nur einmal verschoben werden – entweder in einen Ordner oder in den Papierkorb")
    for a in actions:
        if a["type"] == "move":
            if account_id is None:
                raise RuleError("Verschieben geht nur bei einer Regel für ein bestimmtes Konto (Ordner gehören zu einem Konto)")
            if not str(a.get("folder") or "").strip():
                raise RuleError("Bitte den Zielordner wählen")
        if a["type"] == "forward":
            to = str(a.get("to") or "").strip()
            if not valid_email(to):
                raise RuleError("Bitte eine gültige Adresse zum Weiterleiten eintragen")
            if to.lower() in own_addresses:
                raise RuleError("An die eigene Adresse weiterleiten würde eine Schleife erzeugen")


# --- Auswerten -------------------------------------------------------------

def _values(m: sqlite3.Row, field: str) -> List[str]:
    if field == "from":
        return [f"{m['from_name']} <{m['from_addr']}>", m["from_addr"]]
    if field == "to":
        from email.utils import getaddresses
        return [m["to_addrs"], m["cc_addrs"]] + [a for _, a in getaddresses([m["to_addrs"], m["cc_addrs"]]) if a]
    if field == "subject":
        return [m["subject"]]
    if field == "body":
        return [m["search_text"] or m["body_text"] or m["snippet"] or ""]
    return []


def _check(m: sqlite3.Row, c: Dict) -> bool:
    if c["field"] == "attachment":
        return bool(m["has_attachments"])
    value = str(c["value"]).strip().lower()
    texts = [(t or "").lower() for t in _values(m, c["field"])]
    op = c["op"]
    if op == "contains":
        return any(value in t for t in texts)
    if op == "not_contains":
        return not any(value in t for t in texts)
    if op == "is":
        return any(t.strip() == value for t in texts)
    if op == "ends_with":
        return any(t.strip().rstrip(">").endswith(value) for t in texts)
    return False


def matches(rule: sqlite3.Row, m: sqlite3.Row) -> bool:
    conds = json.loads(rule["conditions"])
    results = (_check(m, c) for c in conds)
    return all(results) if rule["match_all"] else any(results)


def needs_body(rules: List[sqlite3.Row]) -> bool:
    for r in rules:
        if any(c.get("field") in ("body", "attachment") for c in json.loads(r["conditions"])):
            return True
        if any(a.get("type") == "forward" for a in json.loads(r["actions"])):
            return True
    return False


def rules_for(conn: sqlite3.Connection, account: sqlite3.Row) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM rules WHERE owner_id = ? AND active = 1 AND (account_id IS NULL OR account_id = ?) "
                        "ORDER BY position, id", (account["owner_id"], account["id"])).fetchall()


# --- Weiterleiten ------------------------------------------------------------

def is_automatic(raw: bytes) -> bool:
    """Automatische Mails nie weiterleiten (Schleifen, Rückläufer, Abwesenheitsnotizen)."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    auto = (mailparse.header(msg, "Auto-Submitted") or "no").lower()
    if auto != "no" or mailparse.header(msg, FORWARD_HEADER):
        return True
    if mailparse.header(msg, "X-Autoreply") or mailparse.header(msg, "X-Autorespond"):
        return True
    if (mailparse.header(msg, "Precedence") or "").lower() in ("bulk", "junk", "auto_reply"):
        return True
    sender = (mailparse.header(msg, "Return-Path") or "").strip().strip("<>").lower()
    frm = (mailparse.parse_headers(raw)["from_addr"] or "").lower()
    if sender == "" and mailparse.header(msg, "Return-Path"):
        return True  # <> = Rückläufer
    return frm.startswith(("mailer-daemon@", "postmaster@")) or frm.split("@")[0] in ("mailer-daemon", "postmaster")


def _forward_allowed(account_id: int) -> bool:
    q = _forwards[account_id]
    now = time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= FORWARD_LIMIT:
        return False
    q.append(now)
    return True


def forward(account: sqlite3.Row, m: sqlite3.Row, raw: bytes, to: str, rule_name: str) -> None:
    body = mailparse.parse_body(raw)
    files = []
    for a in body["attachments"]:
        if a["inline"] and a["content_id"] and a["mime"].startswith("image/") and body["html"]:
            continue
        got = mailparse.attachment_data(raw, a["idx"])
        if got:
            files.append(got)
    text = body["text"] if body["text"].strip() else mailparse.html_to_text(body["html"])
    sender = mailparse.format_addresses([(m["from_name"], m["from_addr"])])
    head = (f"-------- Weitergeleitete Nachricht (Regel „{rule_name}“) --------\nBetreff: {m['subject']}\n"
            f"Von: {sender}\nAn: {m['to_addrs']}\n" + (f"Kopie: {m['cc_addrs']}\n" if m["cc_addrs"] else "") + "\n")
    subject = m["subject"] if re.match(r"^(wg|fwd?)\s*:", m["subject"] or "", re.I) else f"WG: {m['subject']}"
    msg, rcpts = compose.build(account, to, "", "", subject, head + text, files)
    msg["Auto-Submitted"] = "auto-forwarded"
    msg[FORWARD_HEADER] = "1"
    if m["from_addr"]:
        msg["Reply-To"] = sender  # Antworten gehen an den ursprünglichen Absender
    compose.smtp_send(account, msg, rcpts)


# --- Anwenden ----------------------------------------------------------------

def apply(conn: sqlite3.Connection, account: sqlite3.Row, client, inbox: sqlite3.Row, messages: List[sqlite3.Row],
          rules: List[sqlite3.Row], allow_forward: bool = True) -> Dict:
    """Regeln auf Nachrichten im Posteingang anwenden. client: angemeldete IMAP-Verbindung."""
    stats = {"checked": 0, "moved": 0, "trashed": 0, "forwarded": 0, "errors": 0}
    if not rules or not messages:
        return stats
    want_body = needs_body(rules)
    folders = {r["path"]: r for r in conn.execute("SELECT * FROM folders WHERE account_id = ?", (account["id"],))}
    trash = next((f for f in folders.values() if f["role"] == "trash"), None)
    client.select(inbox["path"], readonly=False)
    from . import sync  # spät importiert (sync importiert rules)
    for m in messages:
        stats["checked"] += 1
        raw = None
        if want_body:
            if m["body_text"] is None:
                raw = client.fetch_message(m["uid"])
                if raw is None:
                    conn.execute("DELETE FROM messages WHERE id = ?", (m["id"],))
                    continue
                sync.store_body(conn, m["id"], raw)
                m = conn.execute("SELECT * FROM messages WHERE id = ?", (m["id"],)).fetchone()
        gone = False
        for rule in rules:
            if not matches(rule, m):
                continue
            actions = json.loads(rule["actions"])
            error = None
            try:
                for a in sorted(actions, key=lambda a: 0 if a["type"] == "forward" else 1):
                    if a["type"] == "forward":
                        if not allow_forward:
                            continue
                        if raw is None:
                            raw = client.fetch_message(m["uid"])
                        if raw is None or is_automatic(raw):
                            continue
                        if not _forward_allowed(account["id"]):
                            error = f"mehr als {FORWARD_LIMIT} Weiterleitungen pro Stunde – angehalten"
                            continue
                        forward(account, m, raw, a["to"], rule["name"])
                        stats["forwarded"] += 1
                    elif a["type"] == "move":
                        dest = folders.get(a["folder"])
                        if not dest:
                            error = f"Ordner „{a['folder']}“ gibt es nicht mehr"
                            continue
                        if dest["id"] != inbox["id"]:
                            client.move([m["uid"]], dest["path"])
                            conn.execute("DELETE FROM messages WHERE id = ?", (m["id"],))
                            stats["moved"] += 1
                            gone = True
                    elif a["type"] == "trash":
                        if trash:
                            client.move([m["uid"]], trash["path"])
                        else:
                            client.delete([m["uid"]])
                        conn.execute("DELETE FROM messages WHERE id = ?", (m["id"],))
                        stats["trashed"] += 1
                        gone = True
            except Exception as e:  # noqa: BLE001 – eine kaputte Regel darf den Abruf nicht stoppen
                from .imapclient import friendly_error
                error = str(e) if isinstance(e, compose.SendError) else friendly_error(e)
                stats["errors"] += 1
            conn.execute("UPDATE rules SET hits = hits + 1, last_hit = ?, last_error = ? WHERE id = ?",
                         (now_utc_str(), error, rule["id"]))
            if gone or rule["stop"]:
                break
        if not gone:
            conn.execute("UPDATE messages SET rules_done = 1 WHERE id = ?", (m["id"],))
    return stats


def process_new(conn: sqlite3.Connection, account: sqlite3.Row, client) -> Optional[Dict]:
    """Beim Abruf: Regeln auf neue Mails im Posteingang anwenden."""
    inbox = conn.execute("SELECT * FROM folders WHERE account_id = ? AND role = 'inbox'", (account["id"],)).fetchone()
    if not inbox:
        return None
    pending = conn.execute("SELECT * FROM messages WHERE folder_id = ? AND rules_done = 0 ORDER BY uid",
                           (inbox["id"],)).fetchall()
    if not pending:
        return None
    rules = rules_for(conn, account)
    if not rules:
        conn.execute("UPDATE messages SET rules_done = 1 WHERE folder_id = ? AND rules_done = 0", (inbox["id"],))
        return None
    stats = apply(conn, account, client, inbox, pending, rules)
    from . import sync
    for f in conn.execute("SELECT id FROM folders WHERE account_id = ?", (account["id"],)):
        sync._update_counts(conn, f["id"])
    if stats["moved"] or stats["trashed"]:
        sync.start_sync(account["id"])  # Zielordner nachziehen
    return stats


def describe(rule: sqlite3.Row, folder_names: Dict[str, str]) -> str:
    conds = json.loads(rule["conditions"])
    parts = []
    for c in conds:
        if c["field"] == "attachment":
            parts.append("hat Anhang")
        else:
            parts.append(f"{FIELDS[c['field']]} {OPS[c['op']]} „{c['value']}“")
    joiner = " und " if rule["match_all"] else " oder "
    acts = []
    for a in json.loads(rule["actions"]):
        if a["type"] == "move":
            acts.append(f"verschieben nach „{folder_names.get(a['folder'], a['folder'])}“")
        elif a["type"] == "forward":
            acts.append(f"weiterleiten an {a['to']}")
        else:
            acts.append("in den Papierkorb")
    return "Wenn " + joiner.join(parts) + " → " + ", ".join(acts)
