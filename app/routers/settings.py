"""Einstellungen, Port und Änderungsprotokoll."""
import csv
import io
import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import portcfg
from ..config import THEMES, settings as app_settings
from ..csvsafe import row as safe_row
from ..db import audit, get_db, get_setting, set_setting
from ..dsm import write_ui_config
from ..security import admin_user, current_user
from ..timeutil import day_start_utc, local_iso, parse_utc, to_local, to_utc_str

router = APIRouter(prefix="/api/v1", tags=["Einstellungen"])


class SettingsIn(BaseModel):
    theme_default: Optional[str] = Field(None, description="Standard-Design für alle (classic, dsm, dark, warm)")
    public_url: Optional[str] = Field(None, max_length=200, description="z. B. https://mail.meinedomain.de – leer = automatisch")
    dsm_login: Optional[bool] = None
    dsm_auto_create: Optional[bool] = None
    dsm_admins: Optional[bool] = None
    dsm_url: Optional[str] = Field(None, max_length=200)
    sync_interval: Optional[int] = Field(None, ge=1, le=60, description="Minuten zwischen zwei Abrufen")
    users_add_accounts: Optional[bool] = Field(None, description="Benutzer dürfen selbst Mailkonten anlegen")
    ms_client_id: Optional[str] = Field(None, max_length=60, description="Microsoft 365: Client-ID der App-Registrierung")
    ms_tenant: Optional[str] = Field(None, max_length=100, description="Microsoft 365: organizations, Mandanten-ID oder Domain")


ADMIN_ONLY = ("dsm_url", "public_url_env", "ms_client_id", "ms_tenant")


def settings_out(conn) -> dict:
    return {
        "company_name": get_setting(conn, "company_name"),
        "theme_default": get_setting(conn, "theme_default") or "classic",
        "themes": THEMES,
        "public_url": get_setting(conn, "public_url"),
        "public_url_env": app_settings.public_url,
        "port": app_settings.port,
        "dsm_login": get_setting(conn, "dsm_login") == "1",
        "dsm_auto_create": get_setting(conn, "dsm_auto_create") == "1",
        "dsm_admins": get_setting(conn, "dsm_admins") == "1",
        "dsm_url": get_setting(conn, "dsm_url"),
        "sync_interval": int(get_setting(conn, "sync_interval") or 5),
        "users_add_accounts": get_setting(conn, "users_add_accounts") == "1",
        "ms_client_id": get_setting(conn, "ms_client_id"),
        "ms_tenant": get_setting(conn, "ms_tenant") or "organizations",
        "ms_ready": bool(get_setting(conn, "ms_client_id")),
    }


@router.get("/settings")
def get_settings(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    out = settings_out(conn)
    if not user["is_admin"]:  # interne Adressen gehen nur den Admin etwas an
        for k in [k for k in out if k.startswith(ADMIN_ONLY)]:
            out.pop(k)
    return out


@router.patch("/settings")
def update_settings(data: SettingsIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    changes = data.model_dump(exclude_unset=True)
    if changes.get("theme_default") is not None:
        if data.theme_default not in THEMES:
            raise HTTPException(400, "Unbekanntes Design")
        set_setting(conn, "theme_default", data.theme_default)
    for key in ("dsm_login", "dsm_auto_create", "dsm_admins", "users_add_accounts"):
        if changes.get(key) is not None:
            set_setting(conn, key, "1" if changes[key] else "0")
    if changes.get("dsm_url"):
        from ..dsm_auth import local_dsm_url
        url = local_dsm_url(data.dsm_url)
        if not url:
            raise HTTPException(400, "Die DSM-Adresse muss auf die NAS selbst zeigen, z. B. http://127.0.0.1:5000 "
                                     "oder https://localhost:5001 – andere Rechner sind aus Sicherheitsgründen nicht erlaubt.")
        set_setting(conn, "dsm_url", url)
    if "ms_client_id" in changes:
        from ..oauth import CLIENT_RE
        cid = (data.ms_client_id or "").strip()
        if cid and not CLIENT_RE.match(cid):
            raise HTTPException(400, "Die Client-ID hat das Format xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx "
                                     "(Entra ID → App-Registrierung → Übersicht → Anwendungs-ID)")
        set_setting(conn, "ms_client_id", cid)
    if "ms_tenant" in changes:
        from ..oauth import TENANT_RE
        tenant = (data.ms_tenant or "").strip() or "organizations"
        if not TENANT_RE.match(tenant):
            raise HTTPException(400, "Mandant: „organizations“, die Mandanten-ID oder die Domain (z. B. praxis.onmicrosoft.com)")
        set_setting(conn, "ms_tenant", tenant)
    if changes.get("sync_interval") is not None:
        set_setting(conn, "sync_interval", str(data.sync_interval))
    if "public_url" in changes:
        url = (data.public_url or "").strip().rstrip("/")
        if url and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", url):
            raise HTTPException(400, "Adresse muss mit http:// oder https:// beginnen, z. B. https://mail.meinedomain.de")
        set_setting(conn, "public_url", url)
        write_ui_config(conn)
    if changes:
        audit(conn, admin["id"], "settings_updated", "settings", str(changes))
    return settings_out(conn)


class PortIn(BaseModel):
    port: int = Field(..., ge=1, le=65535)


@router.post("/settings/port")
def change_port(data: PortIn, request: Request, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Port ändern. Die App startet sich danach selbst neu (dauert wenige Sekunden)."""
    old = app_settings.port
    if data.port == old:
        return {"ok": True, "port": old, "restart": False}
    try:
        portcfg.check_port(data.port)
    except portcfg.PortError as e:
        raise HTTPException(400, str(e))
    audit(conn, admin["id"], "port_changed", "settings", f"{old} → {data.port}")
    try:
        portcfg.apply(data.port)
    except (portcfg.PortError, OSError) as e:
        raise HTTPException(500, f"Port konnte nicht gespeichert werden: {e}")
    host = request.url.hostname or "localhost"
    return {"ok": True, "port": data.port, "restart": True,
            "direct_url": f"{request.url.scheme}://{host}:{data.port}{app_settings.root_path}/"}


# --- Änderungsprotokoll -------------------------------------------------------

AUDIT_CATEGORIES = {"konten": "Mailkonten", "mail": "Mails", "anmeldung": "Anmeldung",
                    "benutzer": "Benutzer", "einstellungen": "Einstellungen", "sicherung": "Datensicherung"}
AUDIT_ACTIONS = {
    "account_created": ("konten", "Mailkonto angelegt"),
    "account_updated": ("konten", "Mailkonto geändert"),
    "account_deleted": ("konten", "Mailkonto gelöscht"),
    "account_password": ("konten", "Kontopasswort geändert"),
    "mail_sent": ("mail", "Mail verschickt"),
    "rule_saved": ("mail", "Regel gespeichert"),
    "rule_deleted": ("mail", "Regel gelöscht"),
    "rule_applied": ("mail", "Regel nachträglich angewendet"),
    "login": ("anmeldung", "Anmeldung"),
    "password_changed": ("anmeldung", "Passwort geändert"),
    "profile_updated": ("benutzer", "Eigenes Profil geändert"),
    "user_created": ("benutzer", "Benutzer angelegt"),
    "user_created_dsm": ("benutzer", "Benutzer per DSM angelegt"),
    "user_admin_dsm": ("benutzer", "Admin-Rechte per DSM"),
    "user_updated": ("benutzer", "Benutzer geändert"),
    "settings_updated": ("einstellungen", "Einstellungen geändert"),
    "port_changed": ("einstellungen", "Port geändert"),
    "export_csv": ("einstellungen", "CSV-Export"),
    "backup_created": ("sicherung", "Sicherung erstellt"),
    "backup_restored": ("sicherung", "Sicherung wiederhergestellt"),
    "backup_deleted": ("sicherung", "Sicherungen gelöscht"),
    "backup_download": ("sicherung", "Sicherung heruntergeladen"),
}
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?\+00:00")


def _names(conn) -> dict:
    return {r["id"]: r["display_name"] for r in conn.execute("SELECT id, display_name FROM users")}


def _text(details: str, names: dict) -> str:
    """Zeitstempel in Ortszeit, „user:5“ als Name."""
    text = _ISO_UTC.sub(lambda m: to_local(parse_utc(m.group(0))).strftime("%d.%m.%Y %H:%M"), details or "")
    text = re.sub(r"\buser:(\d+)", lambda m: names.get(int(m.group(1)), "Benutzer " + m.group(1)), text)
    if text in ("local", "dsm"):
        text = {"local": "mit Passwort", "dsm": "mit DSM-Konto"}[text]
    return text


def _query(user_id, d_from, d_to, category, q):
    sql, args = " WHERE 1 = 1", []
    if user_id:
        sql += " AND (a.actor_id = ? OR a.target = ?)"
        args += [user_id, f"user:{user_id}"]
    if d_from:
        sql += " AND a.ts >= ?"
        args.append(to_utc_str(day_start_utc(d_from)))
    if d_to:
        sql += " AND a.ts < ?"
        args.append(to_utc_str(day_start_utc(d_to + timedelta(days=1))))
    if category:
        acts = [k for k, (c, _) in AUDIT_ACTIONS.items() if c == category]
        if not acts:
            raise HTTPException(400, "Unbekannter Bereich")
        sql += f" AND a.action IN ({','.join('?' * len(acts))})"
        args += acts
    if q:
        sql += " AND (a.details LIKE ? OR a.target LIKE ? OR a.action LIKE ?)"
        args += [f"%{q.strip()}%"] * 3
    return sql, args


def _rows(conn, where, args, limit, before_id=None):
    if before_id:
        where += " AND a.id < ?"
        args = args + [before_id]
    rows = conn.execute("SELECT a.*, u.display_name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id"
                        + where + " ORDER BY a.id DESC LIMIT ?", args + [limit]).fetchall()
    names = _names(conn)
    out = []
    for r in rows:
        cat, label = AUDIT_ACTIONS.get(r["action"], ("", r["action"]))
        target = r["target"] or ""
        subject = names.get(int(target[5:])) if target.startswith("user:") and target[5:].isdigit() else ""
        out.append({"id": r["id"], "ts_local": local_iso(r["ts"]), "actor": r["actor"] or "System",
                    "subject": subject or "", "category": cat, "label": label, "action": r["action"],
                    "text": _text(r["details"], names)})
    return out


@router.get("/audit")
def audit_log(
    user_id: Optional[int] = Query(None, description="Benutzer: handelnd oder betroffen"),
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    category: Optional[str] = Query(None, description=", ".join(AUDIT_CATEGORIES)),
    q: Optional[str] = Query(None, max_length=100, description="Suchtext"),
    before_id: Optional[int] = Query(None, description="zum Nachladen: nur Einträge älter als diese ID"),
    limit: int = Query(200, ge=1, le=1000),
    admin=Depends(admin_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Protokoll aller wichtigen Aktionen – neueste zuerst. Einträge lassen sich nicht ändern oder löschen."""
    where, args = _query(user_id, from_, to, category, q)
    return {"categories": AUDIT_CATEGORIES, "entries": _rows(conn, where, args, limit, before_id)}


@router.get("/audit/export.csv")
def audit_export(
    user_id: Optional[int] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    category: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=100),
    admin=Depends(admin_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Änderungsprotokoll als CSV (Semikolon, für Excel). Text wird gegen Formel-Injection entschärft."""
    where, args = _query(user_id, from_, to, category, q)
    rows = _rows(conn, where, args, 100000)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Zeitpunkt", "Wer", "Betrifft", "Bereich", "Aktion", "Details"])
    for r in rows:
        ts = r["ts_local"][:16].replace("T", " ") if r["ts_local"] else ""
        w.writerow(safe_row([ts, r["actor"], r["subject"], AUDIT_CATEGORIES.get(r["category"], ""), r["label"], r["text"]]))
    audit(conn, admin["id"], "export_csv", "audit", f"Änderungsprotokoll, {len(rows)} Einträge")
    return Response(content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="aenderungsprotokoll.csv"'})
