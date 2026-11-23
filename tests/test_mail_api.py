"""Konten, Abgleich, Lesen, Versand – gegen einen nachgebauten IMAP-Server (mailfake.FakeServer)."""
import base64
import os
import smtplib

import pytest
from fastapi.testclient import TestClient

from app import crypto, sync
from app.db import connect
from app.main import app

from .fresh import fresh_db
from .mailfake import PNG, FakeClient, FakeServer, mail

PW = os.environ["PF_ADMIN_PASSWORD"]


class FakeSMTP:
    sent = []
    fail_auth = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def has_extn(self, name):
        return True

    def login(self, user, pw):
        if FakeSMTP.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"nope")
        self.user, self.pw = user, pw

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSMTP.sent.append({"msg": msg, "from": from_addr, "to": list(to_addrs), "login": (self.user, self.pw)})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(sync, "open_client", lambda account, password=None: FakeClient(server, account["imap_user"]))
    monkeypatch.setattr(sync, "start_sync", lambda account_id: bool(sync.sync_account(account_id)) or True)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    FakeSMTP.sent = []
    FakeSMTP.fail_auth = False
    with fresh_db(tmp_path):
        crypto._cache.clear()
        with TestClient(app) as admin:
            assert admin.post("/api/v1/auth/login", json={"username": "admin", "password": PW}).status_code == 200
            for name in ("anna", "ben"):
                assert admin.post("/api/v1/users", json={"username": name, "display_name": name.title(),
                                                         "password": "passwort-" + name}).status_code == 201
            users = {}
            for name in ("anna", "ben"):
                c = TestClient(app)
                assert c.post("/api/v1/auth/login", json={"username": name, "password": "passwort-" + name}).status_code == 200
                users[name] = c
            yield {"server": server, "admin": admin, **users}
    crypto._cache.clear()


ACC = {"name": "Praxis", "email": "info@praxis.de", "from_name": "Praxis Henze", "imap_host": "imap.praxis.de",
       "imap_password": "imap-geheim", "smtp_host": "smtp.praxis.de", "smtp_port": 587, "smtp_security": "starttls"}


def _uid(server, folder="INBOX"):
    return max(server.folders[folder]["msgs"])


def test_account_crud_and_secret_never_returned(env):
    anna = env["anna"]
    r = anna.post("/api/v1/accounts", json=ACC)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["password_set"] and a["imap_user"] == "info@praxis.de" and a["imap_port"] == 993
    assert "secret" not in a and "imap-geheim" not in r.text
    with connect() as conn:
        secret = conn.execute("SELECT secret FROM accounts WHERE id = ?", (a["id"],)).fetchone()[0]
    assert "imap-geheim" not in secret and crypto.unpack_secrets(secret)["imap"] == "imap-geheim"
    # Passwort leer lassen = unverändert, neues Passwort = neu verschlüsselt
    assert anna.patch(f"/api/v1/accounts/{a['id']}", json={"name": "Büro", "imap_password": ""}).json()["name"] == "Büro"
    anna.patch(f"/api/v1/accounts/{a['id']}", json={"smtp_password": "smtp-geheim"})
    with connect() as conn:
        s = crypto.unpack_secrets(conn.execute("SELECT secret FROM accounts WHERE id = ?", (a["id"],)).fetchone()[0])
    assert s == {"imap": "imap-geheim", "smtp": "smtp-geheim"}
    # Pflichtfelder und Prüfungen
    assert anna.post("/api/v1/accounts", json={**ACC, "imap_password": ""}).status_code == 400
    assert anna.post("/api/v1/accounts", json={**ACC, "imap_host": "böser host; rm"}).status_code == 422
    assert anna.post("/api/v1/accounts", json={**ACC, "email": "kaputt"}).status_code == 422
    # Ben sieht Annas Konto nicht und darf es nicht ändern
    assert env["ben"].get("/api/v1/accounts").json() == []
    assert env["ben"].patch(f"/api/v1/accounts/{a['id']}", json={"name": "x"}).status_code == 404
    assert env["ben"].delete(f"/api/v1/accounts/{a['id']}").status_code == 404
    assert anna.delete(f"/api/v1/accounts/{a['id']}").status_code == 200
    assert anna.get("/api/v1/accounts").json() == []


def test_admin_sets_up_account_but_cannot_read(env):
    admin, anna, server = env["admin"], env["anna"], env["server"]
    server.add("INBOX", mail(subject="Vertraulich", text="Befund für Patientin"))
    anna_id = next(u["id"] for u in admin.get("/api/v1/users").json() if u["username"] == "anna")
    r = admin.post("/api/v1/accounts", json={**ACC, "owner_id": anna_id})
    assert r.status_code == 201 and r.json()["owner"] == "Anna"
    acc_id = r.json()["id"]
    # Admin sieht das Konto in der Verwaltung, aber weder Ordner noch Mails
    assert [x["email"] for x in admin.get("/api/v1/accounts?all=1").json()] == ["info@praxis.de"]
    assert admin.get("/api/v1/accounts").json() == []
    assert admin.get("/api/v1/folders").json()["accounts"] == []
    items = anna.get("/api/v1/messages").json()["items"]
    assert [i["subject"] for i in items] == ["Vertraulich"]
    mid = items[0]["id"]
    assert admin.get(f"/api/v1/messages/{mid}").status_code == 404
    assert admin.get(f"/api/v1/messages?q=Befund").json()["items"] == []
    assert admin.get(f"/api/v1/messages/{mid}/draft?mode=forward").status_code == 404
    assert admin.post("/api/v1/send", json={"account_id": acc_id, "to": "x@y.de"}).status_code == 404
    # Admin darf das Konto aber ändern (z. B. neues Passwort) und abrufen lassen
    assert admin.patch(f"/api/v1/accounts/{acc_id}", json={"imap_password": "neu"}).status_code == 200
    assert admin.post(f"/api/v1/accounts/{acc_id}/sync").status_code == 200
    # Mitarbeiter dürfen keine Konten für andere anlegen
    assert anna.post("/api/v1/accounts", json={**ACC, "owner_id": 1}).status_code == 403


def test_users_may_not_add_when_switched_off(env):
    env["admin"].patch("/api/v1/settings", json={"users_add_accounts": False})
    assert env["anna"].post("/api/v1/accounts", json=ACC).status_code == 403
    assert env["anna"].post("/api/v1/accounts/test", json=ACC).status_code == 403
    assert env["anna"].get("/api/v1/me").json()["users_add_accounts"] is False
    assert env["admin"].post("/api/v1/accounts", json=ACC).status_code == 201


def test_sync_list_read_flags(env):
    anna, server = env["anna"], env["server"]
    server.add("INBOX", mail(subject="Alt", days_ago=200))                       # außerhalb von 90 Tagen
    server.add("INBOX", mail(subject="Gelesen", days_ago=2), flags=["\\Seen"])
    server.add("INBOX", mail(subject="Neu mit Anhang", text="Siehe PDF",
                             attachments=[("Befund.pdf", b"%PDF-1", "application/pdf")]))
    server.add("Gesch&AOQ-ftlich", mail(subject="Angebot", days_ago=1))
    acc = anna.post("/api/v1/accounts", json=ACC).json()
    f = anna.get("/api/v1/folders").json()
    assert f["unified_unread"] == 1
    folders = {x["name"]: x for x in f["accounts"][0]["folders"]}
    assert set(folders) == {"Posteingang", "Sent", "Geschäftlich", "Trash"}
    assert folders["Posteingang"]["role"] == "inbox" and folders["Sent"]["role"] == "sent"
    assert folders["Posteingang"]["total"] == 2
    items = anna.get("/api/v1/messages").json()["items"]
    assert [i["subject"] for i in items] == ["Neu mit Anhang", "Gelesen"]
    assert items[0]["has_attachments"] and not items[0]["seen"] and items[0]["snippet"] == "Siehe PDF"
    # Ordner-Ansicht und Suche (auch im Text)
    assert [i["subject"] for i in anna.get(f"/api/v1/messages?folder_id={folders['Geschäftlich']['id']}").json()["items"]] == ["Angebot"]
    assert [i["subject"] for i in anna.get("/api/v1/messages?q=siehe pdf").json()["items"]] == ["Neu mit Anhang"]
    assert [i["subject"] for i in anna.get("/api/v1/messages?q=Angebot").json()["items"]] == ["Angebot"]
    assert anna.get("/api/v1/messages?q=100%25").json()["items"] == []
    # Lesen markiert als gelesen – lokal sofort, am Server im Hintergrund
    m = anna.get(f"/api/v1/messages/{items[0]['id']}").json()
    assert m["text"].strip() == "Siehe PDF" and m["seen"] and [a["filename"] for a in m["attachments"]] == ["Befund.pdf"]
    att = m["attachments"][0]
    r = anna.get(f"/api/v1/messages/{m['id']}/attachments/{att['id']}")
    assert r.status_code == 200 and r.content == b"%PDF-1" and r.headers["content-type"] == "application/octet-stream"
    assert "Befund.pdf" in r.headers["content-disposition"]
    import time
    for _ in range(50):
        if "\\seen" in server.folders["INBOX"]["msgs"][_uid(server)]["flags"]:
            break
        time.sleep(0.05)
    assert "\\seen" in server.folders["INBOX"]["msgs"][_uid(server)]["flags"]
    # Markieren (Stern) und wieder ungelesen
    assert anna.post(f"/api/v1/messages/{m['id']}/flags", json={"flagged": True, "seen": False}).json() == \
        {"seen": False, "flagged": True, "answered": False}
    assert server.folders["INBOX"]["msgs"][_uid(server)]["flags"] == {"\\flagged"}
    # Änderungen am Server kommen beim nächsten Abruf an; gelöschte verschwinden
    server.folders["INBOX"]["msgs"][_uid(server)]["flags"] = {"\\seen", "\\answered"}
    del server.folders["INBOX"]["msgs"][2]
    sync.sync_account(acc["id"])
    items = anna.get("/api/v1/messages").json()["items"]
    assert [(i["subject"], i["seen"], i["answered"], i["flagged"]) for i in items] == [("Neu mit Anhang", True, True, False)]
    # UIDVALIDITY ändert sich → Ordner wird neu aufgebaut
    server.folders["INBOX"]["uidvalidity"] = 2
    sync.sync_account(acc["id"])
    assert [i["subject"] for i in anna.get("/api/v1/messages").json()["items"]] == ["Neu mit Anhang"]
    # Ben sieht nichts davon
    assert env["ben"].get("/api/v1/messages").json()["items"] == []
    assert env["ben"].get(f"/api/v1/messages/{items[0]['id']}").status_code == 404


def test_html_mail_is_sanitized_and_cid_inlined(env):
    anna, server = env["anna"], env["server"]
    server.add("INBOX", mail(subject="Newsletter", text="t",
                             html='<p onclick="x()">Hallo<img src="cid:logo"><img src="https://track.er/p.gif"></p>'
                                  '<script>alert(1)</script>', inline=[("logo", PNG)]))
    anna.post("/api/v1/accounts", json=ACC)
    mid = anna.get("/api/v1/messages").json()["items"][0]["id"]
    m = anna.get(f"/api/v1/messages/{mid}").json()
    assert "<script" not in m["html"] and "onclick" not in m["html"] and "track.er" not in m["html"]
    assert "data:image/png;base64," in m["html"] and m["remote_blocked"] is True
    assert "img-src data:;" in m["csp"] and m["attachments"] == []   # eingebettetes Logo ist kein Anhang
    m = anna.get(f"/api/v1/messages/{mid}?images=1").json()
    assert "https://track.er/p.gif" in m["html"] and "https:" in m["csp"]


def test_send_reply_forward(env):
    anna, server = env["anna"], env["server"]
    server.add("INBOX", mail(subject="Termin", frm=("Kunde", "kunde@example.de"), to="info@praxis.de",
                             cc="kollege@example.de, info@praxis.de", text="Passt Dienstag?",
                             attachments=[("plan.pdf", b"%PDF-plan", "application/pdf")], message_id="<t1@example.de>"))
    acc = anna.post("/api/v1/accounts", json={**ACC, "signature": "Viele Grüße\nPraxis Henze"}).json()
    mid = anna.get("/api/v1/messages").json()["items"][0]["id"]
    d = anna.get(f"/api/v1/messages/{mid}/draft?mode=replyall").json()
    assert d["to"] == '"Kunde" <kunde@example.de>' and d["cc"] == "kollege@example.de"  # eigene Adresse nicht in Kopie
    assert d["subject"] == "AW: Termin" and d["in_reply_to"] == "<t1@example.de>"
    assert "> Passt Dienstag?" in d["body"] and "-- \nViele Grüße" in d["body"]
    assert anna.get(f"/api/v1/messages/{mid}/draft?mode=reply").json()["cc"] == ""
    # Antwort verschicken
    r = anna.post("/api/v1/send", json={"account_id": acc["id"], "to": d["to"], "cc": d["cc"], "bcc": "chef@praxis.de",
                                        "subject": d["subject"], "body": "Ja, passt.\n" + d["body"],
                                        "in_reply_to": d["in_reply_to"], "references": d["references"],
                                        "reply_to_id": mid,
                                        "attachments": [{"name": "foto.png", "mime": "image/png",
                                                         "data_b64": base64.b64encode(PNG).decode()}]})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None
    sent = FakeSMTP.sent[-1]
    assert sent["to"] == ["kunde@example.de", "kollege@example.de", "chef@praxis.de"]
    assert sent["from"] == "info@praxis.de" and sent["login"] == ("info@praxis.de", "imap-geheim")
    msg = sent["msg"]
    assert msg["In-Reply-To"] == "<t1@example.de>" and "Bcc" not in msg and msg["From"] == "Praxis Henze <info@praxis.de>"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["foto.png"]
    # in „Gesendet“ abgelegt, Original als beantwortet markiert
    assert len(server.folders["Sent"]["msgs"]) == 1 and b"Ja, passt." in list(server.folders["Sent"]["msgs"].values())[0]["raw"]
    assert "\\answered" in server.folders["INBOX"]["msgs"][1]["flags"]
    # Weiterleiten mit Original-Anhang
    d = anna.get(f"/api/v1/messages/{mid}/draft?mode=forward").json()
    assert d["subject"] == "WG: Termin" and "Weitergeleitete Nachricht" in d["body"]
    att_ids = [a["id"] for a in d["forward_attachments"]]
    assert [a["filename"] for a in d["forward_attachments"]] == ["plan.pdf"]
    r = anna.post("/api/v1/send", json={"account_id": acc["id"], "to": "ziel@example.de", "subject": d["subject"],
                                        "body": d["body"], "forward_of": mid, "forward_attachment_ids": att_ids})
    assert r.status_code == 200
    fwd = FakeSMTP.sent[-1]["msg"]
    assert [(p.get_filename(), p.get_content()) for p in fwd.iter_attachments()] == [("plan.pdf", b"%PDF-plan")]
    # Fehlerfälle
    assert anna.post("/api/v1/send", json={"account_id": acc["id"], "to": ""}).status_code == 400
    assert anna.post("/api/v1/send", json={"account_id": acc["id"], "to": "kein-at"}).status_code == 400
    FakeSMTP.fail_auth = True
    r = anna.post("/api/v1/send", json={"account_id": acc["id"], "to": "a@b.de", "subject": "x"})
    assert r.status_code == 502 and "Anmeldung" in r.json()["detail"]
    # Ben kann nicht über Annas Konto senden
    assert env["ben"].post("/api/v1/send", json={"account_id": acc["id"], "to": "a@b.de"}).status_code == 404


def test_sync_error_is_shown_not_raised(env):
    anna, server = env["anna"], env["server"]
    server.fail_login = True
    acc = anna.post("/api/v1/accounts", json=ACC).json()
    assert "Anmeldung" in acc["last_error"]
    server.fail_login = False
    sync.sync_account(acc["id"])
    assert anna.get("/api/v1/accounts").json()[0]["last_error"] is None


def test_account_test_endpoint(env, monkeypatch):
    from app import imapclient
    server = env["server"]
    monkeypatch.setattr(imapclient, "Client", lambda host, port, sec, user, pw: FakeClient(server, user))
    r = env["anna"].post("/api/v1/accounts/test", json=ACC).json()
    assert r == {"imap_ok": True, "imap_error": None, "folders": 4, "smtp_ok": True, "smtp_error": None}
    server.fail_login = True
    FakeSMTP.fail_auth = True
    r = env["anna"].post("/api/v1/accounts/test", json=ACC).json()
    assert not r["imap_ok"] and "Anmeldung" in r["imap_error"] and r["smtp_ok"] is False
