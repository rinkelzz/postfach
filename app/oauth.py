"""Microsoft 365: Anmeldung per OAuth2 mit Gerätecode (RFC 8628) und Zugriff per XOAUTH2 auf IMAP/SMTP.

Ablauf beim Verbinden:
1. Postfach fragt bei Microsoft einen Code an und zeigt ihn an („Gehe zu microsoft.com/devicelogin, Code ABCD-EFGH“).
2. Der Benutzer meldet sich dort (auf jedem Gerät) mit seinem Microsoft-Konto an und erlaubt den Zugriff.
3. Postfach fragt im Hintergrund regelmäßig nach, bis die Anmeldung fertig ist, und speichert die Tokens
   verschlüsselt (wie Passwörter, siehe crypto.py) – nie in Sicherungen, nie im Browser.
4. Der Zugriffstoken (gut 1 Stunde gültig) wird vor jedem Abruf bei Bedarf mit dem Erneuerungstoken erneuert.

Voraussetzung: Die Firma registriert in Entra ID eine App (öffentlicher Client, ohne Geheimnis) mit den delegierten
Berechtigungen IMAP.AccessAsUser.All und SMTP.Send und trägt deren Client-ID unter Verwaltung → Einstellungen ein.
Dazu braucht es keine https-Adresse und keine Weiterleitung zurück zur NAS.
"""
import base64
import json
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

from . import crypto
from .db import get_setting

AUTHORITY = "https://login.microsoftonline.com"
SCOPES = ("openid email offline_access https://outlook.office.com/IMAP.AccessAsUser.All "
          "https://outlook.office.com/SMTP.Send")
IMAP = ("outlook.office365.com", 993, "ssl")
SMTP = ("smtp.office365.com", 587, "starttls")
TENANT_RE = re.compile(r"^[A-Za-z0-9.\-]{1,100}$")
CLIENT_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
REFRESH_BEFORE = 300   # Sekunden vor Ablauf erneuern


class OAuthError(Exception):
    pass


def config(conn: sqlite3.Connection) -> Dict[str, str]:
    return {"client_id": (get_setting(conn, "ms_client_id") or "").strip(),
            "tenant": (get_setting(conn, "ms_tenant") or "organizations").strip() or "organizations"}


def _post(url: str, form: Dict[str, str], timeout: float = 20) -> Dict:
    """POST an Microsoft. Fehlerantworten (400) enthalten JSON mit error/error_description."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json",
                                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 – feste https-Adresse
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"error": f"http_{e.code}", "error_description": str(e)}
    except (OSError, ValueError) as e:
        raise OAuthError(f"Microsoft nicht erreichbar: {e}")


def _describe(resp: Dict) -> str:
    err = resp.get("error", "")
    desc = (resp.get("error_description") or "").split("\r\n")[0]
    codes = resp.get("error_codes") or []
    if 7000218 in codes or "AADSTS7000218" in desc:
        return ("In der App-Registrierung ist „Öffentliche Clientflows zulassen“ nicht eingeschaltet "
                "(Entra ID → App → Authentifizierung).")
    if 700016 in codes or "AADSTS700016" in desc:
        return "Die Client-ID ist bei Microsoft nicht bekannt – bitte unter Verwaltung → Einstellungen prüfen."
    if 65001 in codes or "AADSTS65001" in desc or err == "consent_required":
        return "Der Zugriff wurde noch nicht erlaubt – ein Administrator muss in Entra ID die Berechtigungen bestätigen."
    if err == "invalid_grant":
        return "Die Anmeldung bei Microsoft ist abgelaufen oder wurde widerrufen – bitte das Konto neu verbinden."
    if err == "access_denied":
        return "Die Anmeldung wurde bei Microsoft abgelehnt."
    if err == "expired_token":
        return "Der Code ist abgelaufen – bitte noch einmal verbinden."
    return f"Microsoft meldet: {desc or err or 'unbekannter Fehler'}"[:300]


def _id_email(id_token: str) -> str:
    """E-Mail-Adresse aus dem ID-Token (nur zur Anzeige und Vorbelegung – der Token kommt direkt per TLS von Microsoft)."""
    try:
        payload = id_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return ""
    return (data.get("email") or data.get("preferred_username") or data.get("upn") or "").strip()


# --- Gerätecode-Ablauf -------------------------------------------------------

_flows: Dict[str, Dict] = {}
_flows_lock = threading.Lock()


def start_flow(conn: sqlite3.Connection, user_id: int, payload: Dict) -> Dict:
    """Neuen Code bei Microsoft anfragen und im Hintergrund auf die Anmeldung warten."""
    cfg = config(conn)
    if not cfg["client_id"]:
        raise OAuthError("Microsoft 365 ist noch nicht eingerichtet – der Administrator trägt unter "
                         "Verwaltung → Einstellungen die Client-ID der App-Registrierung ein.")
    resp = _post(f"{AUTHORITY}/{cfg['tenant']}/oauth2/v2.0/devicecode", {"client_id": cfg["client_id"], "scope": SCOPES})
    if "device_code" not in resp:
        raise OAuthError(_describe(resp))
    flow_id = secrets.token_urlsafe(24)
    flow = {"id": flow_id, "user_id": user_id, "status": "pending", "error": None, "email": "", "tokens": None,
            "user_code": resp["user_code"], "verification_uri": resp.get("verification_uri", "https://microsoft.com/devicelogin"),
            "expires": time.time() + int(resp.get("expires_in", 900)), "interval": max(int(resp.get("interval", 5)), 2),
            "device_code": resp["device_code"], "cfg": cfg, "payload": payload, "created": time.time()}
    with _flows_lock:
        for k in [k for k, f in _flows.items() if time.time() - f["created"] > 3600]:
            del _flows[k]
        _flows[flow_id] = flow
    threading.Thread(target=_poll, args=(flow,), name="postfach-oauth", daemon=True).start()
    return {"flow_id": flow_id, "user_code": flow["user_code"], "verification_uri": flow["verification_uri"],
            "expires_in": int(flow["expires"] - time.time())}


def _poll(flow: Dict) -> None:
    cfg = flow["cfg"]
    url = f"{AUTHORITY}/{cfg['tenant']}/oauth2/v2.0/token"
    interval = flow["interval"]
    while flow["status"] == "pending":
        time.sleep(interval)
        if time.time() > flow["expires"]:
            flow.update(status="error", error="Der Code ist abgelaufen – bitte noch einmal verbinden.")
            return
        try:
            resp = _post(url, {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                               "client_id": cfg["client_id"], "device_code": flow["device_code"]})
        except OAuthError:
            continue  # kurzer Netzaussetzer – weiter versuchen, bis der Code abläuft
        err = resp.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err or "access_token" not in resp:
            flow.update(status="error", error=_describe(resp))
            return
        if "refresh_token" not in resp:
            flow.update(status="error", error="Microsoft hat keinen Erneuerungstoken geliefert (Berechtigung offline_access fehlt).")
            return
        flow.update(status="ok", email=_id_email(resp.get("id_token", "")), tokens=_tokens(resp))


def get_flow(flow_id: str, user_id: int) -> Optional[Dict]:
    f = _flows.get(flow_id)
    return f if f and f["user_id"] == user_id else None


def finish_flow(flow_id: str) -> None:
    with _flows_lock:
        _flows.pop(flow_id, None)


# --- Tokens -------------------------------------------------------------------

def _tokens(resp: Dict, old_refresh: str = "") -> Dict:
    return {"access_token": resp["access_token"], "refresh_token": resp.get("refresh_token") or old_refresh,
            "expires_at": time.time() + int(resp.get("expires_in", 3600))}


def pack(tokens: Dict) -> str:
    return crypto.encrypt(json.dumps({"oauth": tokens}))


def unpack(secret: str) -> Dict:
    if not secret:
        raise OAuthError("Für dieses Konto ist keine Anmeldung bei Microsoft gespeichert – bitte neu verbinden.")
    data = json.loads(crypto.decrypt(secret))
    if "oauth" not in data:
        raise OAuthError("Für dieses Konto ist keine Anmeldung bei Microsoft gespeichert – bitte neu verbinden.")
    return data["oauth"]


_refresh_lock = threading.Lock()


def access_token(conn: sqlite3.Connection, account: sqlite3.Row) -> str:
    """Gültiger Zugriffstoken – bei Bedarf erneuert (der neue Erneuerungstoken wird gleich gespeichert)."""
    with _refresh_lock:  # nicht zwei Abrufe gleichzeitig erneuern lassen
        row = conn.execute("SELECT secret FROM accounts WHERE id = ?", (account["id"],)).fetchone()
        tokens = unpack(row["secret"] if row else account["secret"])
        if tokens["expires_at"] - time.time() > REFRESH_BEFORE:
            return tokens["access_token"]
        cfg = config(conn)
        if not cfg["client_id"]:
            raise OAuthError("Microsoft 365 ist nicht mehr eingerichtet (Client-ID fehlt unter Verwaltung → Einstellungen).")
        resp = _post(f"{AUTHORITY}/{cfg['tenant']}/oauth2/v2.0/token",
                     {"grant_type": "refresh_token", "client_id": cfg["client_id"], "refresh_token": tokens["refresh_token"],
                      "scope": SCOPES})
        if "access_token" not in resp:
            raise OAuthError(_describe(resp))
        new = _tokens(resp, tokens["refresh_token"])
        conn.execute("UPDATE accounts SET secret = ? WHERE id = ?", (pack(new), account["id"]))
        return new["access_token"]


def xoauth2(user: str, token: str) -> str:
    """SASL-XOAUTH2-Zeichenkette (ohne Base64 – das erledigen imaplib bzw. smtplib)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"
