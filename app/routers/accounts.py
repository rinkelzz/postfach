"""Mailkonten: anlegen, testen, ändern, löschen, abrufen.

Rechte:
- Jeder Benutzer verwaltet und liest seine eigenen Konten (abschaltbar: „Benutzer dürfen selbst Konten anlegen“).
- Admins dürfen Konten für andere Benutzer einrichten, ändern und löschen – die Mails darin lesen dürfen sie nicht.
  Die Nachrichten-Schnittstelle (messages.py) prüft immer, dass das Konto der angemeldeten Person gehört.
"""
import re
import time
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import compose, crypto, idle, imapclient, oauth, providers, sync
from ..db import audit, get_db, get_setting
from ..mailer import valid_email
from ..security import current_user
from ..timeutil import local_iso, now_utc_str

router = APIRouter(prefix="/api/v1", tags=["Mailkonten"])

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-.]{0,251}[A-Za-z0-9])?$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _host(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip().lower()
    if v and not HOST_RE.match(v):
        raise ValueError("Ungültiger Servername")
    return v


class AccountBase(BaseModel):
    name: Optional[str] = Field(None, max_length=60, description="Anzeigename, z. B. „Praxis“")
    email: Optional[str] = Field(None, max_length=200)
    from_name: Optional[str] = Field(None, max_length=100, description="Absendername")
    imap_host: Optional[str] = Field(None, max_length=253)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    imap_security: Optional[str] = Field(None, pattern="^(ssl|starttls)$")
    imap_user: Optional[str] = Field(None, max_length=200)
    imap_password: Optional[str] = Field(None, max_length=500, description="leer = unverändert")
    smtp_host: Optional[str] = Field(None, max_length=253)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_security: Optional[str] = Field(None, pattern="^(ssl|starttls|none)$")
    smtp_user: Optional[str] = Field(None, max_length=200, description="leer = wie IMAP")
    smtp_password: Optional[str] = Field(None, max_length=500, description="leer = wie IMAP")
    signature: Optional[str] = Field(None, max_length=5000)
    sync_days: Optional[int] = Field(None, ge=7, le=3650)
    color: Optional[str] = Field(None, max_length=7)
    active: Optional[bool] = None
    owner_id: Optional[int] = Field(None, description="nur Admins: Konto für diesen Benutzer")

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def _hosts(cls, v):
        return _host(v)

    @field_validator("email")
    @classmethod
    def _mail(cls, v):
        if v is not None:
            v = v.strip()
            if not valid_email(v):
                raise ValueError("Ungültige E-Mail-Adresse")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        if v is not None and not COLOR_RE.match(v):
            raise ValueError("Farbe als #rrggbb")
        return v


class AccountTest(AccountBase):
    account_id: Optional[int] = Field(None, description="beim Bearbeiten: gespeicherte Passwörter verwenden")


def account_out(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    owner = conn.execute("SELECT display_name, username FROM users WHERE id = ?", (row["owner_id"],)).fetchone()
    return {
        "id": row["id"], "owner_id": row["owner_id"], "owner": owner["display_name"] if owner else "",
        "name": row["name"], "email": row["email"], "from_name": row["from_name"],
        "imap_host": row["imap_host"], "imap_port": row["imap_port"], "imap_security": row["imap_security"],
        "imap_user": row["imap_user"], "smtp_host": row["smtp_host"], "smtp_port": row["smtp_port"],
        "smtp_security": row["smtp_security"], "smtp_user": row["smtp_user"],
        "password_set": bool(row["secret"]), "auth_type": row["auth_type"], "signature": row["signature"], "sync_days": row["sync_days"],
        "color": row["color"], "active": bool(row["active"]), "sort": row["sort"],
        "last_sync": local_iso(row["last_sync"]), "last_error": row["last_error"],
        "syncing": sync.is_running(row["id"]),
        "push": idle.state(row["id"]),   # Zustand des IDLE-Wächters (None = läuft nicht)
    }


def get_account(conn: sqlite3.Connection, user, account_id: int, manage: bool = False) -> sqlite3.Row:
    """Eigenes Konto – oder (nur zum Verwalten, manage=True) jedes Konto für Admins. Sonst 404."""
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row or (row["owner_id"] != user["id"] and not (manage and user["is_admin"])):
        raise HTTPException(404, "Konto nicht gefunden")
    return row


def _may_add(conn, user) -> None:
    if not user["is_admin"] and get_setting(conn, "users_add_accounts") != "1":
        raise HTTPException(403, "Mailkonten richtet bei euch der Administrator ein")


@router.get("/providers")
def provider_hint(email: str, user=Depends(current_user)):
    """Servervorschlag zur Mailadresse (bekannte Anbieter, sonst imap./smtp.<domain>)."""
    s = providers.suggest(email)
    if not s:
        raise HTTPException(400, "Ungültige E-Mail-Adresse")
    return s


@router.get("/accounts")
def list_accounts(all: bool = False, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Eigene Konten. Admins mit ?all=1: alle Konten aller Benutzer (nur Einstellungen, keine Mails)."""
    if all and user["is_admin"]:
        rows = conn.execute("SELECT a.* FROM accounts a JOIN users u ON u.id = a.owner_id "
                            "ORDER BY u.display_name COLLATE NOCASE, a.sort, a.id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM accounts WHERE owner_id = ? ORDER BY sort, id", (user["id"],)).fetchall()
    return [account_out(r, conn) for r in rows]


def _owner_for(conn, user, owner_id: Optional[int]) -> int:
    if owner_id is None or owner_id == user["id"]:
        return user["id"]
    if not user["is_admin"]:
        raise HTTPException(403, "Nur Administratoren richten Konten für andere ein")
    row = conn.execute("SELECT id FROM users WHERE id = ? AND active = 1", (owner_id,)).fetchone()
    if not row:
        raise HTTPException(400, "Benutzer nicht gefunden oder deaktiviert")
    return owner_id


REQUIRED = ("email", "imap_host", "imap_password")


@router.post("/accounts", status_code=201)
def create_account(data: AccountBase, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    _may_add(conn, user)
    missing = [k for k in REQUIRED if not getattr(data, k)]
    if missing:
        raise HTTPException(400, "Bitte ausfüllen: " + ", ".join({"email": "E-Mail-Adresse", "imap_host": "Posteingangsserver",
                                                                   "imap_password": "Passwort"}[k] for k in missing))
    owner = _owner_for(conn, user, data.owner_id)
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM accounts WHERE owner_id = ?", (owner,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO accounts(owner_id, name, email, from_name, imap_host, imap_port, imap_security, imap_user, smtp_host, "
        "smtp_port, smtp_security, smtp_user, secret, signature, sync_days, color, active, sort, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (owner, (data.name or data.email.split("@")[0]).strip(), data.email, (data.from_name or "").strip(),
         data.imap_host, data.imap_port or 993, data.imap_security or "ssl", (data.imap_user or data.email).strip(),
         data.smtp_host or "", data.smtp_port or 587, data.smtp_security or "starttls", (data.smtp_user or "").strip(),
         crypto.pack_secrets(data.imap_password, data.smtp_password), data.signature or "", data.sync_days or 90,
         data.color or "#0f766e", int(data.active if data.active is not None else True), sort, user["id"], now_utc_str()))
    audit(conn, user["id"], "account_created", f"user:{owner}", f"{data.email} ({data.imap_host})")
    sync.start_sync(cur.lastrowid)
    idle.kick()
    return account_out(conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone(), conn)


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, data: AccountBase, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    row = get_account(conn, user, account_id, manage=True)
    if row["owner_id"] == user["id"] and not user["is_admin"]:
        _may_add(conn, user)
    changes = data.model_dump(exclude_unset=True)
    fields = {}
    for k in ("name", "email", "from_name", "imap_host", "imap_port", "imap_security", "imap_user", "smtp_host",
              "smtp_port", "smtp_security", "smtp_user", "signature", "sync_days", "color"):
        if k in changes and changes[k] is not None:
            v = changes[k]
            fields[k] = v.strip() if isinstance(v, str) and k != "signature" else v
    if "active" in changes and data.active is not None:
        fields["active"] = int(data.active)
    if "owner_id" in changes and data.owner_id is not None and data.owner_id != row["owner_id"]:
        fields["owner_id"] = _owner_for(conn, user, data.owner_id)
        if fields["owner_id"] == user["id"] and not user["is_admin"]:
            raise HTTPException(403, "Nicht erlaubt")
    if (data.imap_password or data.smtp_password) and row["auth_type"] == "oauth_ms":
        raise HTTPException(400, "Dieses Konto meldet sich über Microsoft an – zum Erneuern „Neu verbinden“ nutzen")
    if data.imap_password or data.smtp_password:
        old = crypto.unpack_secrets(row["secret"]) if row["secret"] else {"imap": "", "smtp": ""}
        imap_pw = data.imap_password or old["imap"]
        if not imap_pw:
            raise HTTPException(400, "Bitte das Passwort für den Posteingang eintragen")
        smtp_pw = data.smtp_password if data.smtp_password else (old["smtp"] if old["smtp"] != old["imap"] else None)
        fields["secret"] = crypto.pack_secrets(imap_pw, smtp_pw)
        audit(conn, user["id"], "account_password", f"user:{row['owner_id']}", row["email"])
    if fields:
        conn.execute(f"UPDATE accounts SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                     list(fields.values()) + [account_id])
        server_changed = any(k in fields for k in ("imap_host", "imap_port", "imap_user", "secret", "sync_days"))
        if server_changed and row["imap_host"] != fields.get("imap_host", row["imap_host"]):
            conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))  # anderer Server → neu aufbauen
        logged = {k: v for k, v in changes.items() if "password" not in k and k != "signature"}
        audit(conn, user["id"], "account_updated", f"user:{row['owner_id']}", f"{row['email']}: {logged}"[:500])
        idle.kick(account_id)
        if server_changed or fields.get("active") == 1:
            conn.execute("UPDATE accounts SET last_error = NULL WHERE id = ?", (account_id,))
            sync.start_sync(account_id)
    return account_out(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone(), conn)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Konto aus Postfach entfernen. Die Mails am Server bleiben unberührt."""
    row = get_account(conn, user, account_id, manage=True)
    if row["owner_id"] == user["id"] and not user["is_admin"]:
        _may_add(conn, user)
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    idle.kick(account_id)
    audit(conn, user["id"], "account_deleted", f"user:{row['owner_id']}", row["email"])
    return {"ok": True}


@router.post("/accounts/test")
def test_account(data: AccountTest, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Anmeldung an Posteingang (IMAP) und Postausgang (SMTP) prüfen – ohne etwas zu speichern."""
    _may_add(conn, user)  # verbindet sich mit beliebigen Servern → nur wer Konten einrichten darf
    base = dict(get_account(conn, user, data.account_id, manage=True)) if data.account_id else {}
    if base.get("auth_type") == "oauth_ms":
        return _test_oauth(conn, base)
    old = crypto.unpack_secrets(base["secret"]) if base.get("secret") else {"imap": "", "smtp": ""}
    vals = {**base, **{k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}}
    vals.setdefault("imap_port", 993)
    vals.setdefault("imap_security", "ssl")
    vals.setdefault("smtp_port", 587)
    vals.setdefault("smtp_security", "starttls")
    vals.setdefault("smtp_host", "")
    vals["imap_user"] = vals.get("imap_user") or vals.get("email") or ""
    vals["smtp_user"] = vals.get("smtp_user") or ""
    if not vals.get("imap_host"):
        raise HTTPException(400, "Bitte den Posteingangsserver eintragen")
    imap_pw = data.imap_password or old["imap"]
    smtp_pw = data.smtp_password or (old["smtp"] if not data.imap_password else "") or imap_pw
    result = {"imap_ok": False, "imap_error": None, "folders": 0, "smtp_ok": None, "smtp_error": None}
    try:
        with imapclient.Client(vals["imap_host"], vals["imap_port"], vals["imap_security"], vals["imap_user"], imap_pw) as c:
            result["folders"] = len(c.list_folders())
        result["imap_ok"] = True
    except Exception as e:  # noqa: BLE001
        result["imap_error"] = imapclient.friendly_error(e)
    if vals["smtp_host"]:
        try:
            compose.test_smtp(vals, smtp_pw)
            result["smtp_ok"] = True
        except compose.SendError as e:
            result["smtp_ok"] = False
            result["smtp_error"] = str(e)
    return result


@router.post("/accounts/{account_id}/sync")
def sync_now(account_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Abruf sofort starten (läuft im Hintergrund)."""
    row = get_account(conn, user, account_id, manage=True)
    if not row["active"]:
        raise HTTPException(400, "Das Konto ist ausgeschaltet")
    started = sync.start_sync(account_id)
    return {"started": started, "running": True}


@router.post("/sync")
def sync_all(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Alle eigenen Konten jetzt abrufen."""
    ids = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE owner_id = ? AND active = 1", (user["id"],))]
    return {"started": [i for i in ids if sync.start_sync(i)]}


def _test_oauth(conn, account: dict) -> dict:
    result = {"imap_ok": False, "imap_error": None, "folders": 0, "smtp_ok": None, "smtp_error": None}
    try:
        token = oauth.access_token(conn, account)
    except oauth.OAuthError as e:
        result["imap_error"] = str(e)
        return result
    try:
        with imapclient.Client(account["imap_host"], account["imap_port"], account["imap_security"], account["imap_user"],
                               "", oauth_token=token) as c:
            result["folders"] = len(c.list_folders())
        result["imap_ok"] = True
    except Exception as e:  # noqa: BLE001
        result["imap_error"] = imapclient.friendly_error(e)
    if account.get("smtp_host"):
        try:
            compose.smtp_send(account, None, [], oauth_token=token)
            result["smtp_ok"] = True
        except compose.SendError as e:
            result["smtp_ok"] = False
            result["smtp_error"] = str(e) + (" – in Exchange muss für das Postfach „Authentifiziertes SMTP“ "
                                             "eingeschaltet sein" if "Anmeldung" in str(e) else "")
    return result


# --- Microsoft 365: verbinden mit Code ----------------------------------------

class MsStartIn(BaseModel):
    account_id: Optional[int] = Field(None, description="bestehendes Konto neu verbinden")
    owner_id: Optional[int] = Field(None, description="nur Admins: neues Konto für diesen Benutzer")
    name: Optional[str] = Field(None, max_length=60)
    color: Optional[str] = Field(None, max_length=7)


@router.post("/accounts/microsoft/start")
def ms_start(data: MsStartIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Anmeldung bei Microsoft starten: liefert einen Code für microsoft.com/devicelogin."""
    if data.account_id:
        row = get_account(conn, user, data.account_id, manage=True)
        if row["auth_type"] != "oauth_ms":
            raise HTTPException(400, "Dieses Konto meldet sich mit Passwort an")
        payload = {"account_id": row["id"]}
    else:
        _may_add(conn, user)
        if data.color and not COLOR_RE.match(data.color):
            raise HTTPException(422, "Farbe als #rrggbb")
        payload = {"owner_id": _owner_for(conn, user, data.owner_id), "name": (data.name or "").strip(),
                   "color": data.color or "#0078d4"}
    try:
        return oauth.start_flow(conn, user["id"], payload)
    except oauth.OAuthError as e:
        raise HTTPException(400, str(e))


@router.get("/accounts/microsoft/{flow_id}")
def ms_status(flow_id: str, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Stand der Anmeldung. Ist sie fertig, wird das Konto angelegt bzw. aktualisiert."""
    flow = oauth.get_flow(flow_id, user["id"])
    if not flow:
        raise HTTPException(404, "Anmeldung nicht gefunden oder abgelaufen")
    if flow["status"] == "pending":
        return {"status": "pending", "expires_in": max(0, int(flow["expires"] - time.time()))}
    if flow["status"] == "error":
        oauth.finish_flow(flow_id)
        return {"status": "error", "error": flow["error"]}
    if flow.get("account_id_done"):
        return {"status": "ok", "account": account_out(conn.execute("SELECT * FROM accounts WHERE id = ?",
                                                                    (flow["account_id_done"],)).fetchone(), conn)}
    email = flow["email"]
    if not valid_email(email):
        oauth.finish_flow(flow_id)
        return {"status": "error", "error": "Microsoft hat keine E-Mail-Adresse geliefert – Konto bitte mit Passwort einrichten."}
    secret = oauth.pack(flow["tokens"])
    p = flow["payload"]
    if p.get("account_id"):
        row = get_account(conn, user, p["account_id"], manage=True)
        if row["email"].lower() != email.lower():
            oauth.finish_flow(flow_id)
            return {"status": "error", "error": f"Angemeldet wurde {email}, das Konto gehört aber zu {row['email']}."}
        conn.execute("UPDATE accounts SET secret = ?, last_error = NULL WHERE id = ?", (secret, row["id"]))
        audit(conn, user["id"], "account_password", f"user:{row['owner_id']}", f"{email} (Microsoft neu verbunden)")
        acc_id = row["id"]
        idle.kick(acc_id)
    else:
        sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM accounts WHERE owner_id = ?", (p["owner_id"],)).fetchone()[0]
        acc_id = conn.execute(
            "INSERT INTO accounts(owner_id, name, email, from_name, imap_host, imap_port, imap_security, imap_user, smtp_host, "
            "smtp_port, smtp_security, smtp_user, secret, auth_type, color, sort, created_by, created_at) "
            "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'oauth_ms', ?, ?, ?, ?)",
            (p["owner_id"], p.get("name") or email.split("@")[1], email, *oauth.IMAP, email, *oauth.SMTP, email, secret,
             p.get("color") or "#0078d4", sort, user["id"], now_utc_str())).lastrowid
        audit(conn, user["id"], "account_created", f"user:{p['owner_id']}", f"{email} (Microsoft 365)")
        idle.kick()
    flow["account_id_done"] = acc_id
    flow["tokens"] = None  # nicht länger als nötig im Speicher
    sync.start_sync(acc_id)
    return {"status": "ok", "account": account_out(conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone(), conn)}
