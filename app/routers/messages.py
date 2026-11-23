"""Ordner, Nachrichtenliste, Lesen, Markieren, Anhänge, Antworten/Weiterleiten und Versand.

Alles hier gilt nur für die eigenen Konten der angemeldeten Person – auch Admins sehen keine fremden Mails.
"""
import sqlite3
import threading
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import compose, crypto, htmlsafe, imapclient, mailparse, sync
from ..db import audit, connect, get_db
from ..security import current_user
from ..timeutil import local_iso
from .accounts import get_account

router = APIRouter(prefix="/api/v1", tags=["Mails"])
PAGE = 50


def _own_message(conn: sqlite3.Connection, user, message_id: int):
    m = conn.execute("SELECT m.* FROM messages m JOIN accounts a ON a.id = m.account_id "
                     "WHERE m.id = ? AND a.owner_id = ?", (message_id, user["id"])).fetchone()
    if not m:
        raise HTTPException(404, "Nachricht nicht gefunden")
    return m, conn.execute("SELECT * FROM accounts WHERE id = ?", (m["account_id"],)).fetchone()


def _imap_fail(e: Exception):
    if isinstance(e, HTTPException):
        raise e
    if isinstance(e, (sync.SyncError, crypto.CryptoError)):
        raise HTTPException(502, str(e))
    raise HTTPException(502, imapclient.friendly_error(e))


# --- Ordner ------------------------------------------------------------------

@router.get("/folders")
def folders(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Eigene Konten mit Ordnern und Zählern, dazu ungelesene im gemeinsamen Posteingang."""
    accounts = conn.execute("SELECT * FROM accounts WHERE owner_id = ? ORDER BY sort, id", (user["id"],)).fetchall()
    out = []
    for a in accounts:
        fl = conn.execute("SELECT id, name, path, role, unread, total, delimiter FROM folders WHERE account_id = ?",
                          (a["id"],)).fetchall()
        fl = sorted(fl, key=lambda r: (imapclient.ROLE_ORDER.get(r["role"], 9), r["name"].lower()))
        out.append({"id": a["id"], "name": a["name"], "email": a["email"], "color": a["color"], "active": bool(a["active"]),
                    "last_sync": local_iso(a["last_sync"]), "last_error": a["last_error"], "syncing": sync.is_running(a["id"]),
                    "folders": [dict(r) for r in fl]})
    unread = conn.execute("SELECT COALESCE(SUM(f.unread), 0) FROM folders f JOIN accounts a ON a.id = f.account_id "
                          "WHERE a.owner_id = ? AND a.active = 1 AND f.role = 'inbox'", (user["id"],)).fetchone()[0]
    return {"accounts": out, "unified_unread": unread}


# --- Liste -------------------------------------------------------------------

def _list_item(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "account_id": r["account_id"], "folder_id": r["folder_id"], "subject": r["subject"],
            "from_name": r["from_name"], "from_addr": r["from_addr"], "to_addrs": r["to_addrs"],
            "date": local_iso(r["date_utc"]), "seen": bool(r["seen"]), "flagged": bool(r["flagged"]),
            "answered": bool(r["answered"]), "has_attachments": bool(r["has_attachments"]), "snippet": r["snippet"],
            "account_color": r["color"], "account_name": r["account_name"], "folder_role": r["role"]}


@router.get("/messages")
def list_messages(
    folder_id: Optional[int] = Query(None, description="Ordner; ohne = gemeinsamer Posteingang aller Konten"),
    q: Optional[str] = Query(None, max_length=200, description="Suche in Betreff, Absender, Empfänger und Text"),
    unread: bool = False,
    flagged: bool = False,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(PAGE, ge=1, le=200),
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    where = ["a.owner_id = ?", "a.active = 1"]
    args: list = [user["id"]]
    if folder_id is not None:
        folder = conn.execute("SELECT f.id FROM folders f JOIN accounts a ON a.id = f.account_id "
                              "WHERE f.id = ? AND a.owner_id = ?", (folder_id, user["id"])).fetchone()
        if not folder:
            raise HTTPException(404, "Ordner nicht gefunden")
        where.append("m.folder_id = ?")
        args.append(folder_id)
    elif not q:
        where.append("f.role = 'inbox'")
    if q and q.strip():
        for word in q.split()[:6]:
            like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(m.subject LIKE ? ESCAPE '\\' OR m.from_name LIKE ? ESCAPE '\\' OR m.from_addr LIKE ? ESCAPE '\\' "
                         "OR m.to_addrs LIKE ? ESCAPE '\\' OR m.cc_addrs LIKE ? ESCAPE '\\' OR m.snippet LIKE ? ESCAPE '\\' "
                         "OR m.search_text LIKE ? ESCAPE '\\')")
            args += [like] * 7
    if unread:
        where.append("m.seen = 0")
    if flagged:
        where.append("m.flagged = 1")
    sql = ("SELECT m.id, m.account_id, m.folder_id, m.subject, m.from_name, m.from_addr, m.to_addrs, m.date_utc, m.seen, "
           "m.flagged, m.answered, m.has_attachments, m.snippet, a.color, a.name AS account_name, f.role "
           "FROM messages m JOIN accounts a ON a.id = m.account_id JOIN folders f ON f.id = m.folder_id "
           f"WHERE {' AND '.join(where)} ORDER BY m.date_utc DESC, m.id DESC LIMIT ? OFFSET ?")
    rows = conn.execute(sql, args + [limit + 1, offset]).fetchall()
    return {"items": [_list_item(r) for r in rows[:limit]], "more": len(rows) > limit, "offset": offset}


# --- Lesen -------------------------------------------------------------------

def _mark_seen_later(account_id: int, message_id: int) -> None:
    """Gelesen-Markierung im Hintergrund an den Server schicken (die Anzeige wartet nicht darauf)."""
    def run():
        conn = connect()
        try:
            m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            a = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if m and a:
                sync.set_flags(conn, a, m, {"seen": True}, force=True)
        except Exception as e:  # noqa: BLE001
            print(f"[postfach] Gelesen-Markierung fehlgeschlagen: {imapclient.friendly_error(e)}", flush=True)
        finally:
            conn.close()
    threading.Thread(target=run, daemon=True).start()


@router.get("/messages/{message_id}")
def read_message(message_id: int, images: bool = Query(False, description="externe Bilder laden"),
                 peek: bool = Query(False, description="nicht als gelesen markieren"),
                 user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    m, account = _own_message(conn, user, message_id)
    raw = None
    if m["body_text"] is None or (m["body_html"] and "cid:" in (m["body_html"] or "").lower()):
        try:
            raw = sync.load_body(conn, account, m, mark_seen=not peek) if m["body_text"] is None else sync.fetch_raw(conn, account, m)
        except Exception as e:  # noqa: BLE001
            _imap_fail(e)
        if raw is None:
            raise HTTPException(404, "Die Nachricht gibt es am Server nicht mehr")
        m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    elif not peek and not m["seen"]:
        conn.execute("UPDATE messages SET seen = 1 WHERE id = ?", (message_id,))
        conn.execute("UPDATE folders SET unread = MAX(unread - 1, 0) WHERE id = ?", (m["folder_id"],))
        _mark_seen_later(account["id"], message_id)
        m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    html_out, blocked = "", False
    if m["body_html"]:
        inline = mailparse.inline_images(raw) if raw else {}
        html_out, blocked = htmlsafe.sanitize(m["body_html"], allow_remote=images, inline_images=inline)
    atts = [dict(r) for r in conn.execute(
        "SELECT id, filename, mime, size, inline, content_id FROM attachments WHERE message_id = ? ORDER BY idx", (message_id,))]
    folder = conn.execute("SELECT id, name, role FROM folders WHERE id = ?", (m["folder_id"],)).fetchone()
    return {
        "id": m["id"], "account_id": m["account_id"], "account_email": account["email"], "account_name": account["name"],
        "folder": dict(folder) if folder else None,
        "subject": m["subject"], "from_name": m["from_name"], "from_addr": m["from_addr"], "to_addrs": m["to_addrs"],
        "cc_addrs": m["cc_addrs"], "reply_to": m["reply_to"], "date": local_iso(m["date_utc"]),
        "seen": bool(m["seen"]), "flagged": bool(m["flagged"]), "answered": bool(m["answered"]),
        "text": m["body_text"] or "", "html": html_out, "csp": htmlsafe.csp(images), "remote_blocked": blocked,
        "attachments": [a for a in atts if not (a["inline"] and a["content_id"] and a["mime"].startswith("image/") and m["body_html"])],
    }


class FlagsIn(BaseModel):
    seen: Optional[bool] = None
    flagged: Optional[bool] = None


@router.post("/messages/{message_id}/flags")
def set_flags(message_id: int, data: FlagsIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    m, account = _own_message(conn, user, message_id)
    try:
        sync.set_flags(conn, account, m, data.model_dump(exclude_none=True))
    except Exception as e:  # noqa: BLE001
        _imap_fail(e)
    r = conn.execute("SELECT seen, flagged, answered FROM messages WHERE id = ?", (message_id,)).fetchone()
    return {k: bool(r[k]) for k in ("seen", "flagged", "answered")}


@router.get("/messages/{message_id}/attachments/{attachment_id}")
def attachment(message_id: int, attachment_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Anhang herunterladen – wird dafür frisch vom Server geholt."""
    m, account = _own_message(conn, user, message_id)
    att = conn.execute("SELECT * FROM attachments WHERE id = ? AND message_id = ?", (attachment_id, message_id)).fetchone()
    if not att:
        raise HTTPException(404, "Anhang nicht gefunden")
    try:
        raw = sync.fetch_raw(conn, account, m)
    except Exception as e:  # noqa: BLE001
        _imap_fail(e)
    got = mailparse.attachment_data(raw, att["idx"]) if raw else None
    if not got:
        raise HTTPException(404, "Anhang am Server nicht mehr vorhanden")
    name, _mime, data = got
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "anhang"
    # Immer als Download (nie im Browser darstellen) – sonst könnte ein HTML-Anhang Code auf unserer Adresse ausführen
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff"})


@router.get("/messages/{message_id}/draft")
def reply_draft(message_id: int, mode: str = Query(..., pattern="^(reply|replyall|forward)$"),
                user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Vorbefülltes Formular zum Antworten, Allen antworten oder Weiterleiten."""
    m, account = _own_message(conn, user, message_id)
    if m["body_text"] is None:
        try:
            sync.load_body(conn, account, m)
        except Exception as e:  # noqa: BLE001
            _imap_fail(e)
        m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    own = {r[0].lower() for r in conn.execute("SELECT email FROM accounts WHERE owner_id = ?", (user["id"],))}
    d = compose.draft(conn, account, m, mode, own)
    d["reply_to_id"] = message_id if mode != "forward" else None
    d["forward_of"] = message_id if mode == "forward" else None
    return d


# --- Versand -----------------------------------------------------------------

class Upload(BaseModel):
    name: str = Field(..., max_length=200)
    mime: Optional[str] = Field(None, max_length=100)
    data_b64: str = Field(..., max_length=36_000_000)


class SendIn(BaseModel):
    account_id: int
    to: str = Field("", max_length=5000)
    cc: str = Field("", max_length=5000)
    bcc: str = Field("", max_length=5000)
    subject: str = Field("", max_length=500)
    body: str = Field("", max_length=1_000_000)
    attachments: List[Upload] = Field(default_factory=list, max_length=50)
    reply_to_id: Optional[int] = Field(None, description="beantwortete Nachricht (wird als beantwortet markiert)")
    forward_of: Optional[int] = Field(None, description="weitergeleitete Nachricht")
    forward_attachment_ids: List[int] = Field(default_factory=list, max_length=100,
                                              description="Anhänge der weitergeleiteten Nachricht, die mitgehen")
    in_reply_to: str = Field("", max_length=1000)
    references: str = Field("", max_length=8000)


@router.post("/send")
def send(data: SendIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    account = get_account(conn, user, data.account_id)  # nur eigene Konten
    if not account["active"]:
        raise HTTPException(400, "Das Konto ist ausgeschaltet")
    try:
        files = compose.decode_upload(data.attachments)
    except compose.SendError as e:
        raise HTTPException(400, str(e))
    if data.forward_of and data.forward_attachment_ids:
        fm, faccount = _own_message(conn, user, data.forward_of)
        wanted = {r["idx"]: r for r in conn.execute(
            f"SELECT * FROM attachments WHERE message_id = ? AND id IN ({','.join('?' * len(data.forward_attachment_ids))})",
            [fm["id"]] + list(data.forward_attachment_ids))}
        if wanted:
            try:
                raw = sync.fetch_raw(conn, faccount, fm)
            except Exception as e:  # noqa: BLE001
                _imap_fail(e)
            for idx in sorted(wanted):
                got = mailparse.attachment_data(raw, idx) if raw else None
                if got:
                    files.append(got)
    try:
        msg, rcpts = compose.build(account, data.to, data.cc, data.bcc, data.subject, data.body, files,
                                   data.in_reply_to, data.references)
        compose.smtp_send(account, msg, rcpts)
    except compose.SendError as e:
        raise HTTPException(400 if "Adresse" in str(e) or "Empfänger angeben" in str(e) or "MB" in str(e) else 502, str(e))
    except crypto.CryptoError as e:
        raise HTTPException(502, str(e))
    audit(conn, user["id"], "mail_sent", f"account:{account['id']}",
          f"{account['email']} → {len(rcpts)} Empfänger{', ' + str(len(files)) + ' Anhänge' if files else ''}")
    warning = compose.save_sent(conn, account, msg, sync.open_client)
    if data.reply_to_id:
        try:
            rm, raccount = _own_message(conn, user, data.reply_to_id)
            sync.set_flags(conn, raccount, rm, {"answered": True})
        except Exception:  # noqa: BLE001 – Markierung ist nicht wichtig genug für eine Fehlermeldung
            pass
    sync.start_sync(account["id"])
    return {"ok": True, "warning": warning, "message_id": msg["Message-ID"]}
