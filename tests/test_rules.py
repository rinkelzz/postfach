"""Regeln: Prüfen, Auswerten beim Abruf (nur neue Mails), Verschieben, Papierkorb, Weiterleiten, Schleifenschutz."""
import pytest

from app import rules as R
from app import sync

from .mailfake import mail
from .test_mail_api import ACC, FakeSMTP, env  # noqa: F401 – Fixture wird wiederverwendet


def _setup(anna, server):
    acc = anna.post("/api/v1/accounts", json=ACC).json()   # erster Abruf: keine Regeln auf Vorhandenes
    folders = {f["role"] or f["name"]: f for f in anna.get("/api/v1/folders").json()["accounts"][0]["folders"]}
    return acc, folders


def _inbox(server):
    return sorted(m["raw"].split(b"Subject: ")[1].split(b"\r\n")[0].decode() for m in server.folders["INBOX"]["msgs"].values())


def test_validation(env):
    anna = env["anna"]
    acc, folders = _setup(anna, env["server"])
    ok = {"name": "N", "account_id": acc["id"], "conditions": [{"field": "from", "op": "contains", "value": "news"}],
          "actions": [{"type": "move", "folder": "Gesch&AOQ-ftlich"}]}
    assert anna.post("/api/v1/rules", json=ok).status_code == 201
    bad = [
        {**ok, "conditions": []},
        {**ok, "conditions": [{"field": "from", "op": "contains", "value": " "}]},
        {**ok, "actions": []},
        {**ok, "account_id": None},                                                    # Verschieben braucht ein Konto
        {**ok, "actions": [{"type": "move", "folder": "GibtsNicht"}]},
        {**ok, "actions": [{"type": "move", "folder": "Trash"}, {"type": "trash"}]},    # zweimal verschieben
        {**ok, "actions": [{"type": "forward", "to": "kaputt"}]},
        {**ok, "actions": [{"type": "forward", "to": "INFO@praxis.de"}]},               # Schleife an sich selbst
    ]
    for b in bad:
        assert anna.post("/api/v1/rules", json=b).status_code == 400, b
    assert anna.post("/api/v1/rules", json={**ok, "conditions": [{"field": "nix", "value": "x"}]}).status_code == 422
    # fremde Konten und Regeln
    assert env["ben"].post("/api/v1/rules", json=ok).status_code == 404
    rid = anna.get("/api/v1/rules").json()["rules"][0]["id"]
    assert env["ben"].get("/api/v1/rules").json()["rules"] == []
    assert env["ben"].patch(f"/api/v1/rules/{rid}", json={"name": "x"}).status_code == 404
    assert env["ben"].delete(f"/api/v1/rules/{rid}").status_code == 404
    assert env["admin"].get("/api/v1/rules").json()["rules"] == []                    # auch der Admin nicht


def test_only_new_mails_move_and_trash(env):
    anna, server = env["anna"], env["server"]
    server.add("INBOX", mail(subject="Alter Newsletter", frm=("News", "news@shop.example")))
    acc, folders = _setup(anna, server)
    anna.post("/api/v1/rules", json={"name": "Newsletter", "account_id": acc["id"],
                                      "conditions": [{"field": "from", "op": "ends_with", "value": "@shop.example"}],
                                      "actions": [{"type": "move", "folder": "Gesch&AOQ-ftlich"}]})
    anna.post("/api/v1/rules", json={"name": "Spam", "match_all": False,
                                      "conditions": [{"field": "subject", "op": "contains", "value": "GEWINN"},
                                                     {"field": "body", "op": "contains", "value": "casino"}],
                                      "actions": [{"type": "trash"}]})
    server.add("INBOX", mail(subject="Neuer Newsletter", frm=("News", "news@shop.example")))
    server.add("INBOX", mail(subject="Sie haben gewonnen", text="Jetzt im Casino spielen"))
    server.add("INBOX", mail(subject="Termin morgen", frm=("Kunde", "k@example.de")))
    r = sync.sync_account(acc["id"])
    assert r["rules"] == {"checked": 3, "moved": 1, "trashed": 1, "forwarded": 0, "errors": 0}
    assert _inbox(server) == ["Alter Newsletter", "Termin morgen"]          # Altes bleibt unberührt
    assert len(server.folders["Gesch&AOQ-ftlich"]["msgs"]) == 1 and len(server.folders["Trash"]["msgs"]) == 1
    rules = {x["name"]: x for x in anna.get("/api/v1/rules").json()["rules"]}
    assert rules["Newsletter"]["hits"] == 1 and rules["Spam"]["hits"] == 1
    assert rules["Newsletter"]["summary"] == "Wenn Absender endet auf „@shop.example“ → verschieben nach „Geschäftlich“"
    # zweiter Abruf: nichts wird doppelt angewendet
    assert sync.sync_account(acc["id"]).get("rules") is None
    # „Jetzt anwenden“: erst Vorschau, dann wirklich
    rid = rules["Newsletter"]["id"]
    d = anna.post(f"/api/v1/rules/{rid}/apply", json={"dry_run": True}).json()
    assert d["matches"] == 1 and d["sample"][0]["subject"] == "Alter Newsletter" and not d["applied"]
    assert _inbox(server) == ["Alter Newsletter", "Termin morgen"]
    d = anna.post(f"/api/v1/rules/{rid}/apply", json={"dry_run": False}).json()
    assert d["moved"] == 1 and _inbox(server) == ["Termin morgen"]


def test_stop_and_inactive_and_order(env):
    anna, server = env["anna"], env["server"]
    acc, _ = _setup(anna, server)
    a = anna.post("/api/v1/rules", json={"name": "A", "stop": True, "conditions": [{"field": "subject", "value": "x"}],
                                          "actions": [{"type": "forward", "to": "a@example.de"}]}).json()
    b = anna.post("/api/v1/rules", json={"name": "B", "conditions": [{"field": "subject", "value": "x"}],
                                          "actions": [{"type": "forward", "to": "b@example.de"}]}).json()
    server.add("INBOX", mail(subject="x1"))
    sync.sync_account(acc["id"])
    assert [s["to"] for s in FakeSMTP.sent] == [["a@example.de"]]                  # A stoppt, B läuft nicht
    anna.post("/api/v1/rules/order", json={"ids": [b["id"], a["id"]]})
    anna.patch(f"/api/v1/rules/{a['id']}", json={"active": False})
    server.add("INBOX", mail(subject="x2"))
    sync.sync_account(acc["id"])
    assert [s["to"] for s in FakeSMTP.sent][-1] == ["b@example.de"] and len(FakeSMTP.sent) == 2
    assert anna.post("/api/v1/rules/order", json={"ids": [a["id"]]}).status_code == 400


def test_forward_and_loop_protection(env):
    anna, server = env["anna"], env["server"]
    acc, _ = _setup(anna, server)
    anna.post("/api/v1/rules", json={"name": "Rechnungen", "account_id": acc["id"],
                                      "conditions": [{"field": "subject", "op": "contains", "value": "rechnung"},
                                                     {"field": "attachment"}],
                                      "actions": [{"type": "forward", "to": "buchhaltung@example.de"}]})
    server.add("INBOX", mail(subject="Rechnung 42", frm=("Lieferant", "l@example.de"), text="Anbei",
                             attachments=[("r.pdf", b"%PDF", "application/pdf")]))
    server.add("INBOX", mail(subject="Rechnung ohne Anhang"))
    auto = mail(subject="Rechnung Abwesend", attachments=[("r.pdf", b"%PDF", "application/pdf")])
    server.add("INBOX", auto.replace(b"Subject:", b"Auto-Submitted: auto-replied\r\nSubject:", 1))
    again = mail(subject="Rechnung schon weitergeleitet", attachments=[("r.pdf", b"%PDF", "application/pdf")])
    server.add("INBOX", again.replace(b"Subject:", b"X-Postfach-Forwarded: 1\r\nSubject:", 1))
    bounce = mail(subject="Rechnung unzustellbar", frm=("", "MAILER-DAEMON@example.de"),
                  attachments=[("r.pdf", b"%PDF", "application/pdf")])
    server.add("INBOX", bounce)
    r = sync.sync_account(acc["id"])
    assert r["rules"]["forwarded"] == 1 and len(FakeSMTP.sent) == 1
    sent = FakeSMTP.sent[0]
    msg = sent["msg"]
    assert sent["to"] == ["buchhaltung@example.de"] and msg["Subject"] == "WG: Rechnung 42"
    assert msg["Auto-Submitted"] == "auto-forwarded" and msg["X-Postfach-Forwarded"] == "1"
    assert msg["Reply-To"] == "Lieferant <l@example.de>"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["r.pdf"]
    assert len(server.folders["INBOX"]["msgs"]) == 5                               # Weiterleiten lässt die Mail liegen
    # „Jetzt anwenden“ leitet nie nachträglich weiter
    rid = anna.get("/api/v1/rules").json()["rules"][0]["id"]
    assert anna.post(f"/api/v1/rules/{rid}/apply", json={"dry_run": True}).status_code == 400


def test_forward_rate_limit(env, monkeypatch):
    anna, server = env["anna"], env["server"]
    monkeypatch.setattr(R, "FORWARD_LIMIT", 2)
    R._forwards.clear()
    acc, _ = _setup(anna, server)
    anna.post("/api/v1/rules", json={"name": "Alles", "conditions": [{"field": "subject", "value": "m"}],
                                      "actions": [{"type": "forward", "to": "x@example.de"}]})
    for i in range(4):
        server.add("INBOX", mail(subject=f"m{i}"))
    sync.sync_account(acc["id"])
    assert len(FakeSMTP.sent) == 2
    rule = anna.get("/api/v1/rules").json()["rules"][0]
    assert "pro Stunde" in rule["last_error"]


def test_rule_with_deleted_folder_records_error(env):
    anna, server = env["anna"], env["server"]
    acc, _ = _setup(anna, server)
    anna.post("/api/v1/rules", json={"name": "Weg", "account_id": acc["id"], "conditions": [{"field": "subject", "value": "y"}],
                                      "actions": [{"type": "move", "folder": "Gesch&AOQ-ftlich"}]})
    del server.folders["Gesch&AOQ-ftlich"]
    server.add("INBOX", mail(subject="y"))
    sync.sync_account(acc["id"])
    rule = anna.get("/api/v1/rules").json()["rules"][0]
    assert "gibt es nicht mehr" in rule["last_error"] and _inbox(server) == ["y"]


@pytest.mark.parametrize("op,value,expect", [("contains", "MÜLLER", True), ("not_contains", "müller", False),
                                             ("is", "mueller@kanzlei.de", True), ("ends_with", "@kanzlei.de", True),
                                             ("ends_with", "kanzlei", False)])
def test_matching(op, value, expect):
    m = {"from_name": "Jürgen Müller", "from_addr": "mueller@kanzlei.de", "to_addrs": "", "cc_addrs": "", "subject": "",
         "search_text": "", "body_text": "", "snippet": "", "has_attachments": 0}
    rule = {"conditions": f'[{{"field": "from", "op": "{op}", "value": "{value}"}}]', "match_all": 1}
    assert R.matches(rule, m) is expect
