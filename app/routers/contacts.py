"""Adressbuch je Benutzer – auch Admins sehen fremde Kontakte nicht."""
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import contacts as C
from ..db import get_db
from ..mailer import valid_email
from ..security import current_user
from ..timeutil import local_iso, now_utc_str

router = APIRouter(prefix="/api/v1/contacts", tags=["Adressbuch"])


def out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "email": r["email"], "name": r["name"], "company": r["company"], "phone": r["phone"],
            "notes": r["notes"], "manual": bool(r["manual"]), "sent_count": r["sent_count"],
            "received_count": r["received_count"], "last_used": local_iso(r["last_used"]) if r["last_used"] else None}


class ContactIn(BaseModel):
    email: Optional[str] = Field(None, max_length=200)
    name: Optional[str] = Field(None, max_length=120)
    company: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=60)
    notes: Optional[str] = Field(None, max_length=2000)


def _get(conn, user, cid: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM contacts WHERE id = ? AND owner_id = ? AND hidden = 0", (cid, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Kontakt nicht gefunden")
    return r


@router.get("")
def list_contacts(q: Optional[str] = Query(None, max_length=100), filter: str = Query("all", pattern="^(all|manual|auto)$"),
                  offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
                  user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    where, args = ["owner_id = ?", "hidden = 0"], [user["id"]]
    if filter == "manual":
        where.append("manual = 1")
    elif filter == "auto":
        where.append("manual = 0")
    if q and q.strip():
        like = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(email LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\' OR lower(company) LIKE ? ESCAPE '\\' "
                     "OR phone LIKE ? ESCAPE '\\')")
        args += [like] * 4
    total = conn.execute(f"SELECT COUNT(*) FROM contacts WHERE {' AND '.join(where)}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM contacts WHERE {' AND '.join(where)} "
                        "ORDER BY CASE WHEN name = '' THEN email ELSE lower(name) END LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    return {"total": total, "items": [out(r) for r in rows]}


@router.get("/suggest")
def suggest(q: str = Query("", max_length=100), user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Vorschläge beim Tippen von Empfängern (häufig angeschriebene zuerst)."""
    return C.suggest(conn, user["id"], q)


@router.post("", status_code=201)
def create(data: ContactIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    email = (data.email or "").strip().lower()
    if not valid_email(email):
        raise HTTPException(400, "Bitte eine gültige E-Mail-Adresse eintragen")
    old = conn.execute("SELECT * FROM contacts WHERE owner_id = ? AND email = ?", (user["id"], email)).fetchone()
    fields = {k: (getattr(data, k) or "").strip() for k in ("name", "company", "phone", "notes")}
    if old and not old["hidden"]:
        raise HTTPException(409, "Diese Adresse steht schon im Adressbuch")
    if old:  # war ausgeblendet → wieder aufnehmen
        conn.execute("UPDATE contacts SET hidden = 0, manual = 1, name = ?, company = ?, phone = ?, notes = ? WHERE id = ?",
                     (fields["name"], fields["company"], fields["phone"], fields["notes"], old["id"]))
        return out(_get(conn, user, old["id"]))
    cid = conn.execute("INSERT INTO contacts(owner_id, email, name, company, phone, notes, manual, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, 1, ?)", (user["id"], email, fields["name"], fields["company"],
                                                         fields["phone"], fields["notes"], now_utc_str())).lastrowid
    return out(_get(conn, user, cid))


@router.patch("/{cid}")
def update(cid: int, data: ContactIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    row = _get(conn, user, cid)
    ch = data.model_dump(exclude_unset=True)
    fields = {k: (v or "").strip() for k, v in ch.items() if k in ("name", "company", "phone", "notes")}
    if "email" in ch:
        email = (data.email or "").strip().lower()
        if not valid_email(email):
            raise HTTPException(400, "Bitte eine gültige E-Mail-Adresse eintragen")
        clash = conn.execute("SELECT id, hidden FROM contacts WHERE owner_id = ? AND email = ? AND id != ?",
                             (user["id"], email, cid)).fetchone()
        if clash and not clash["hidden"]:
            raise HTTPException(409, "Diese Adresse steht schon im Adressbuch")
        if clash:
            conn.execute("DELETE FROM contacts WHERE id = ?", (clash["id"],))
        fields["email"] = email
    fields["manual"] = 1  # von Hand bearbeitet → Name nicht mehr automatisch überschreiben
    conn.execute(f"UPDATE contacts SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", list(fields.values()) + [row["id"]])
    return out(_get(conn, user, cid))


@router.delete("/{cid}")
def delete(cid: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Kontakt entfernen. Er bleibt ausgeblendet gemerkt, damit er nicht automatisch wiederkommt."""
    row = _get(conn, user, cid)
    conn.execute("UPDATE contacts SET hidden = 1, name = '', company = '', phone = '', notes = '' WHERE id = ?", (row["id"],))
    return {"ok": True}
