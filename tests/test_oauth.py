"""Microsoft 365: Gerätecode-Anmeldung, Konto anlegen/neu verbinden, Token erneuern – gegen nachgebautes Microsoft."""
import base64
import json
import time

import pytest

from app import oauth
from app.db import connect
from app.sync import open_client as REAL_OPEN_CLIENT  # vor dem Ersetzen im env-Fixture gemerkt

from .test_mail_api import ACC, env  # noqa: F401 – Fixture wird wiederverwendet

CID = "11111111-2222-3333-4444-555555555555"


def _idtoken(email):
    body = base64.urlsafe_b64encode(json.dumps({"preferred_username": email}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}."


class FakeMicrosoft:
    def __init__(self):
        self.calls = []
        self.pending = 1
        self.email = "anna@praxis.onmicrosoft.com"
        self.token_error = None
        self.refresh_error = None

    def post(self, url, form, timeout=20):
        self.calls.append((url, dict(form)))
        if url.endswith("/devicecode"):
            assert form["client_id"] == CID and "IMAP.AccessAsUser.All" in form["scope"] and "offline_access" in form["scope"]
            return {"device_code": "DEV", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900, "interval": 5}
        if form.get("grant_type") == "refresh_token":
            if self.refresh_error:
                return {"error": self.refresh_error, "error_description": "AADSTS70008: expired"}
            return {"access_token": "AT-neu", "refresh_token": "RT-neu", "expires_in": 3600}
        if self.token_error:
            return self.token_error
        if self.pending:
            self.pending -= 1
            return {"error": "authorization_pending"}
        return {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600, "id_token": _idtoken(self.email)}


@pytest.fixture()
def ms(env, monkeypatch):
    fake = FakeMicrosoft()
    monkeypatch.setattr(oauth, "_post", fake.post)
    monkeypatch.setattr(oauth.time, "sleep", lambda s: None)
    assert env["admin"].patch("/api/v1/settings", json={"ms_client_id": CID}).status_code == 200
    return fake


def _wait(client, flow_id):
    for _ in range(100):
        r = client.get(f"/api/v1/accounts/microsoft/{flow_id}").json()
        if r["status"] != "pending":
            return r
        time.sleep(0.02)
    raise AssertionError("Anmeldung nicht fertig")


def test_settings_validation(env):
    admin = env["admin"]
    assert admin.patch("/api/v1/settings", json={"ms_client_id": "kaputt"}).status_code == 400
    assert admin.patch("/api/v1/settings", json={"ms_tenant": "böse/../x"}).status_code == 400
    s = admin.patch("/api/v1/settings", json={"ms_client_id": CID, "ms_tenant": "praxis.onmicrosoft.com"}).json()
    assert s["ms_client_id"] == CID and s["ms_tenant"] == "praxis.onmicrosoft.com" and s["ms_ready"]
    user_view = env["anna"].get("/api/v1/settings").json()
    assert user_view["ms_ready"] is True and "ms_client_id" not in user_view


def test_not_configured(env):
    r = env["anna"].post("/api/v1/accounts/microsoft/start", json={})
    assert r.status_code == 400 and "Client-ID" in r.json()["detail"]


def test_connect_new_account(env, ms):
    anna = env["anna"]
    r = anna.post("/api/v1/accounts/microsoft/start", json={"name": "Praxis M365"}).json()
    assert r["user_code"] == "ABCD-EFGH" and r["verification_uri"] == "https://microsoft.com/devicelogin"
    done = _wait(anna, r["flow_id"])
    assert done["status"] == "ok", done
    a = done["account"]
    assert a["email"] == "anna@praxis.onmicrosoft.com" and a["auth_type"] == "oauth_ms" and a["name"] == "Praxis M365"
    assert (a["imap_host"], a["imap_port"], a["smtp_host"], a["smtp_port"]) == ("outlook.office365.com", 993, "smtp.office365.com", 587)
    with connect() as conn:
        secret = conn.execute("SELECT secret FROM accounts WHERE id = ?", (a["id"],)).fetchone()[0]
    assert "RT-1" not in secret and oauth.unpack(secret)["refresh_token"] == "RT-1"
    # zweiter Abruf des Status legt kein zweites Konto an
    assert _wait(anna, r["flow_id"])["account"]["id"] == a["id"]
    assert len(anna.get("/api/v1/accounts").json()) == 1
    # fremde Anmeldung nicht abrufbar
    assert env["ben"].get(f"/api/v1/accounts/microsoft/{r['flow_id']}").status_code == 404
    # Passwort setzen geht bei Microsoft-Konten nicht
    assert anna.patch(f"/api/v1/accounts/{a['id']}", json={"imap_password": "x"}).status_code == 400


def test_errors_are_explained(env, ms):
    ms.token_error = {"error": "invalid_client", "error_description": "AADSTS7000218: The request body must contain client_secret",
                      "error_codes": [7000218]}
    r = env["anna"].post("/api/v1/accounts/microsoft/start", json={}).json()
    done = _wait(env["anna"], r["flow_id"])
    assert done["status"] == "error" and "Öffentliche Clientflows" in done["error"]
    ms.token_error = {"error": "access_denied"}
    r = env["anna"].post("/api/v1/accounts/microsoft/start", json={}).json()
    assert "abgelehnt" in _wait(env["anna"], r["flow_id"])["error"]


def test_reconnect_and_refresh(env, ms):
    anna = env["anna"]
    a = _wait(anna, anna.post("/api/v1/accounts/microsoft/start", json={}).json()["flow_id"])["account"]
    with connect() as conn:
        # Token läuft gleich ab → wird erneuert und gespeichert
        conn.execute("UPDATE accounts SET secret = ? WHERE id = ?",
                     (oauth.pack({"access_token": "alt", "refresh_token": "RT-1", "expires_at": time.time() + 10}), a["id"]))
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (a["id"],)).fetchone()
        assert oauth.access_token(conn, row) == "AT-neu"
        assert oauth.unpack(conn.execute("SELECT secret FROM accounts WHERE id = ?", (a["id"],)).fetchone()[0])["refresh_token"] == "RT-neu"
        assert oauth.access_token(conn, row) == "AT-neu"  # noch gültig → kein neuer Aufruf
        assert sum(1 for _, f in ms.calls if f.get("grant_type") == "refresh_token") == 1
        # widerrufen → verständlicher Fehler beim Abruf
        conn.execute("UPDATE accounts SET secret = ? WHERE id = ?",
                     (oauth.pack({"access_token": "alt", "refresh_token": "RT-x", "expires_at": 0}), a["id"]))
    ms.refresh_error = "invalid_grant"
    with connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (a["id"],)).fetchone()
        with pytest.raises(oauth.OAuthError) as e:
            oauth.access_token(conn, row)
    assert "neu verbinden" in str(e.value)
    # Neu verbinden mit falschem Microsoft-Konto wird abgelehnt, mit richtigem übernommen
    ms.refresh_error = None
    ms.email = "jemand.anders@praxis.onmicrosoft.com"
    r = _wait(anna, anna.post("/api/v1/accounts/microsoft/start", json={"account_id": a["id"]}).json()["flow_id"])
    assert r["status"] == "error" and "gehört aber zu" in r["error"]
    ms.email = a["email"]
    r = _wait(anna, anna.post("/api/v1/accounts/microsoft/start", json={"account_id": a["id"]}).json()["flow_id"])
    assert r["status"] == "ok" and r["account"]["id"] == a["id"] and r["account"]["last_error"] is None
    # Passwort-Konten lassen sich nicht „neu verbinden“
    pw = anna.post("/api/v1/accounts", json={**ACC, "email": "pw@praxis.de"}).json()
    assert anna.post("/api/v1/accounts/microsoft/start", json={"account_id": pw["id"]}).status_code == 400


def test_open_client_uses_xoauth2(env, monkeypatch, ms):
    """open_client holt den Token und übergibt ihn an den IMAP-Client (statt eines Passworts)."""
    from app import imapclient
    seen = {}

    class Spy:
        def __init__(self, host, port, sec, user, pw, oauth_token=None):
            seen.update(host=host, user=user, pw=pw, token=oauth_token)
    monkeypatch.setattr(imapclient, "Client", Spy)
    with connect() as conn:
        acc_id = conn.execute("INSERT INTO accounts(owner_id, name, email, imap_host, imap_port, imap_security, imap_user, "
                              "secret, auth_type, created_at) VALUES (1, 'M', 'm@x.onmicrosoft.com', 'outlook.office365.com', "
                              "993, 'ssl', 'm@x.onmicrosoft.com', ?, 'oauth_ms', '2026-01-01T00:00:00+00:00')",
                              (oauth.pack({"access_token": "AT-gut", "refresh_token": "R", "expires_at": time.time() + 3600}),)).lastrowid
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
    REAL_OPEN_CLIENT(row)
    assert seen == {"host": "outlook.office365.com", "user": "m@x.onmicrosoft.com", "pw": "", "token": "AT-gut"}
