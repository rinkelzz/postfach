"""Adressbuch: automatisch sammeln (Posteingang, Gesendet, Versand), von Hand pflegen, Vorschläge, Trennung je Benutzer."""
from app import sync

from .mailfake import mail
from .test_mail_api import ACC, FakeSMTP, env  # noqa: F401


def test_collect_and_suggest(env):
    anna, server = env["anna"], env["server"]
    server.add("INBOX", mail(subject="a", frm=("Sabine Krüger", "S.Krueger@praxis-krueger.example")))
    server.add("INBOX", mail(subject="b", frm=("", "noreply@shop.example")))            # technisch → nicht aufnehmen
    server.add("INBOX", mail(subject="c", frm=("Info", "info@praxis.de")))                # eigene Adresse
    server.add("Sent", mail(subject="d", frm=("Praxis", "info@praxis.de"), to='"Markus Weber" <m.weber@kanzlei.example>',
                            cc="buchhaltung@praxis-krueger.example"))
    acc = anna.post("/api/v1/accounts", json=ACC).json()
    items = {c["email"]: c for c in anna.get("/api/v1/contacts").json()["items"]}
    assert set(items) == {"s.krueger@praxis-krueger.example", "m.weber@kanzlei.example", "buchhaltung@praxis-krueger.example"}
    assert items["s.krueger@praxis-krueger.example"]["name"] == "Sabine Krüger"
    assert items["s.krueger@praxis-krueger.example"]["received_count"] == 1 and items["m.weber@kanzlei.example"]["sent_count"] == 1
    # Versand aus Postfach zählt als „angeschrieben“ → steht bei Vorschlägen vorn
    anna.post("/api/v1/send", json={"account_id": acc["id"], "to": "buchhaltung@praxis-krueger.example", "subject": "x", "body": "y"})
    anna.post("/api/v1/send", json={"account_id": acc["id"], "to": "Neu Kunde <neu@kunde.example>", "subject": "x", "body": "y"})
    sug = [c["email"] for c in anna.get("/api/v1/contacts/suggest?q=krue").json()]
    assert sug == ["buchhaltung@praxis-krueger.example", "s.krueger@praxis-krueger.example"]
    assert [c["name"] for c in anna.get("/api/v1/contacts/suggest?q=neu").json()] == ["Neu Kunde"]
    assert anna.get("/api/v1/contacts/suggest?q=").json() == []
    assert anna.get("/api/v1/contacts/suggest?q=%25").json() == []
    # andere Benutzer und der Admin sehen nichts
    assert env["ben"].get("/api/v1/contacts").json()["total"] == 0
    assert env["admin"].get("/api/v1/contacts/suggest?q=krue").json() == []
    assert len(FakeSMTP.sent) == 2


def test_manual_edit_delete(env):
    anna, server = env["anna"], env["server"]
    acc = anna.post("/api/v1/accounts", json=ACC).json()
    r = anna.post("/api/v1/contacts", json={"email": "Dr.Hoffmann@Example.de", "name": "Dr. Hoffmann", "company": "Labor Nord",
                                            "phone": "05371 1234"})
    assert r.status_code == 201 and r.json()["email"] == "dr.hoffmann@example.de" and r.json()["manual"]
    cid = r.json()["id"]
    assert anna.post("/api/v1/contacts", json={"email": "dr.hoffmann@example.de"}).status_code == 409
    assert anna.post("/api/v1/contacts", json={"email": "kaputt"}).status_code == 400
    # Von Hand gepflegter Name wird durch eingehende Mails nicht überschrieben
    server.add("INBOX", mail(subject="x", frm=("hoffmann", "dr.hoffmann@example.de")))
    sync.sync_account(acc["id"])
    c = next(x for x in anna.get("/api/v1/contacts?q=labor").json()["items"])
    assert c["name"] == "Dr. Hoffmann" and c["received_count"] == 1
    assert anna.patch(f"/api/v1/contacts/{cid}", json={"phone": "0170 999"}).json()["phone"] == "0170 999"
    assert env["ben"].patch(f"/api/v1/contacts/{cid}", json={"name": "x"}).status_code == 404
    assert env["ben"].delete(f"/api/v1/contacts/{cid}").status_code == 404
    # Gelöschte Kontakte kommen nicht automatisch wieder …
    assert anna.delete(f"/api/v1/contacts/{cid}").status_code == 200
    server.add("INBOX", mail(subject="y", frm=("Hoffmann", "dr.hoffmann@example.de")))
    sync.sync_account(acc["id"])
    assert anna.get("/api/v1/contacts?q=hoffmann").json()["total"] == 0
    assert anna.get("/api/v1/contacts/suggest?q=hoff").json() == []
    # … lassen sich aber von Hand wieder anlegen
    assert anna.post("/api/v1/contacts", json={"email": "dr.hoffmann@example.de", "name": "Hoffmann"}).status_code == 201
    assert anna.get("/api/v1/contacts?filter=manual").json()["total"] == 1
    assert anna.get("/api/v1/contacts?filter=auto").json()["total"] == 0
