"""Gegen einen echten IMAP-Server (z. B. Dovecot) – nur wenn PF_TEST_IMAP gesetzt ist:

    PF_TEST_IMAP="localhost:10993:ssl:tim@test.de:geheim123" pytest tests/test_imap_real.py

Das Postfach des Testbenutzers wird dabei geleert und neu befüllt.
"""
import imaplib
import os
import ssl
import time

import pytest

from app import compose, crypto, imapclient, sync
from app.db import connect
from app.timeutil import now_utc_str

from .fresh import fresh_db
from .mailfake import PNG, mail

CFG = os.environ.get("PF_TEST_IMAP", "")
pytestmark = pytest.mark.skipif(not CFG, reason="PF_TEST_IMAP nicht gesetzt (kein Test-IMAP-Server)")


def _cfg():
    host, port, sec, user, pw = CFG.split(":", 4)
    return host, int(port), sec, user, pw


def _reset_and_fill():
    host, port, sec, user, pw = _cfg()
    c = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context()) if sec == "ssl" else imaplib.IMAP4(host, port)
    c.login(user, pw)
    for box in ("INBOX", "Sent", "Gesch&AOQ-ftlich", "Trash", "Drafts"):
        typ, data = c.select(box)
        if int(data[0]):
            c.store("1:*", "+FLAGS.SILENT", "(\\Deleted)")
            c.expunge()
    now = imaplib.Time2Internaldate(time.time())
    c.append("INBOX", "(\\Seen)", now, mail(subject="Grüße aus Köln", text="Schon gelesen"))
    c.append("INBOX", "()", now, mail(subject="Rechnung", text="Anbei",
                                      attachments=[("Rechnung März.pdf", b"%PDF-1.4 real", "application/pdf")]))
    c.append("INBOX", "()", now, mail(subject="Newsletter", text="t", html='<p>Hi<img src="cid:l"></p>', inline=[("l", PNG)]))
    c.append('"Gesch&AOQ-ftlich"', "()", now, mail(subject="Angebot"))
    c.logout()


def test_real_server_roundtrip(tmp_path):
    host, port, sec, user, pw = _cfg()
    _reset_and_fill()
    with fresh_db(tmp_path):
        crypto._cache.clear()
        with connect() as conn:
            acc_id = conn.execute(
                "INSERT INTO accounts(owner_id, name, email, imap_host, imap_port, imap_security, imap_user, smtp_host, "
                "secret, created_at) VALUES (1, 'Test', ?, ?, ?, ?, ?, '', ?, ?)",
                (user, host, port, sec, user, crypto.pack_secrets(pw), now_utc_str())).lastrowid
        r = sync.sync_account(acc_id)
        assert r["error"] is None, r
        with connect() as conn:
            folders = {f["name"]: f for f in conn.execute("SELECT * FROM folders WHERE account_id = ?", (acc_id,))}
            assert {"Posteingang", "Sent", "Geschäftlich"} <= set(folders)
            assert folders["Sent"]["role"] == "sent" and folders["Posteingang"]["unread"] == 2
            msgs = {m["subject"]: m for m in conn.execute("SELECT * FROM messages WHERE account_id = ?", (acc_id,))}
            assert set(msgs) == {"Grüße aus Köln", "Rechnung", "Newsletter", "Angebot"}
            assert all(m["body_text"] is not None for m in msgs.values())   # Inhalte im selben Durchlauf nachgeladen
            rech = msgs["Rechnung"]
            att = conn.execute("SELECT * FROM attachments WHERE message_id = ?", (rech["id"],)).fetchone()
            assert att["filename"] == "Rechnung März.pdf" and rech["has_attachments"] == 1
            assert msgs["Newsletter"]["has_attachments"] == 0
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
            # Markierungen am echten Server setzen und wieder abgleichen
            sync.set_flags(conn, account, rech, {"seen": True, "flagged": True})
            conn.execute("UPDATE messages SET seen = 0, flagged = 0 WHERE id = ?", (rech["id"],))
        sync.sync_account(acc_id)
        with connect() as conn:
            rech = conn.execute("SELECT * FROM messages WHERE id = ?", (rech["id"],)).fetchone()
            assert rech["seen"] == 1 and rech["flagged"] == 1
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
            raw = sync.fetch_raw(conn, account, rech)
            from app import mailparse
            assert mailparse.attachment_data(raw, att["idx"])[2] == b"%PDF-1.4 real"
            # Gesendete Mail ablegen (APPEND) – mit Umlauten im Ordner und Text
            msg, _ = compose.build(account, "a@b.de", "", "", "Testversand äöü", "Inhalt", [])
            assert compose.save_sent(conn, account, msg, sync.open_client) is None
        sync.sync_account(acc_id)
        with connect() as conn:
            sent = conn.execute("SELECT m.* FROM messages m JOIN folders f ON f.id = m.folder_id "
                                "WHERE f.role = 'sent' AND m.account_id = ?", (acc_id,)).fetchall()
            assert [m["subject"] for m in sent] == ["Testversand äöü"] and sent[0]["seen"] == 1
        # falsches Passwort → verständliche Meldung
        with pytest.raises(Exception) as e:
            imapclient.Client(host, port, sec, user, "falsch")
        assert "Anmeldung" in imapclient.friendly_error(e.value)
    crypto._cache.clear()


def test_real_server_idle_move_delete_drafts(tmp_path):
    from app import idle
    host, port, sec, user, pw = _cfg()
    _reset_and_fill()
    with fresh_db(tmp_path):
        crypto._cache.clear()
        with connect() as conn:
            acc_id = conn.execute(
                "INSERT INTO accounts(owner_id, name, email, imap_host, imap_port, imap_security, imap_user, smtp_host, "
                "secret, created_at) VALUES (1, 'Test', ?, ?, ?, ?, ?, '', ?, ?)",
                (user, host, port, sec, user, crypto.pack_secrets(pw), now_utc_str())).lastrowid
        assert sync.sync_account(acc_id)["error"] is None
        # IDLE-Wächter: neue Mail erscheint ohne Abruf von Hand
        sig = idle._signature(connect().execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone())
        w = idle.Watcher(acc_id, sig)
        w.start()
        try:
            for _ in range(50):
                if w.state == "wartet":
                    break
                time.sleep(0.1)
            assert w.state == "wartet"
            c = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context()) if sec == "ssl" else imaplib.IMAP4(host, port)
            c.login(user, pw)
            c.append("INBOX", "()", imaplib.Time2Internaldate(time.time()), mail(subject="Per IDLE", message_id="<idle@x>"))
            c.logout()
            found = None
            for _ in range(100):
                with connect() as conn:
                    found = conn.execute("SELECT * FROM messages WHERE subject = 'Per IDLE'").fetchone()
                if found:
                    break
                time.sleep(0.1)
            assert found, "IDLE hat die neue Mail nicht gemeldet"
        finally:
            w.stop()
            w.join(5)
        assert not w.is_alive()
        while sync.is_running(acc_id):
            time.sleep(0.1)
        with connect() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
            folders = {f["role"] or f["name"]: f for f in conn.execute("SELECT * FROM folders WHERE account_id = ?", (acc_id,))}
            m = conn.execute("SELECT * FROM messages WHERE subject = 'Per IDLE'").fetchone()
            # in den Papierkorb, dann endgültig
            assert sync.delete_message(conn, account, m) == "trash"
        while sync.is_running(acc_id):
            time.sleep(0.1)
        sync.sync_account(acc_id)
        with connect() as conn:
            t = conn.execute("SELECT * FROM messages WHERE subject = 'Per IDLE'").fetchone()
            assert t and t["folder_id"] == folders["trash"]["id"]
            assert sync.delete_message(conn, account, t) == "deleted"
        # Verschieben ohne MOVE (COPY + Löschen) und ohne UIDPLUS
        real_open = sync.open_client

        def old_server(account, password=None):
            c = real_open(account, password)
            c.capabilities -= {"MOVE", "UIDPLUS"}
            return c
        sync.open_client = old_server
        try:
            with connect() as conn:
                m = conn.execute("SELECT * FROM messages WHERE subject = 'Rechnung'").fetchone()
                sync.move_message(conn, account, m, folders["Geschäftlich"])
        finally:
            sync.open_client = real_open
        while sync.is_running(acc_id):
            time.sleep(0.1)
        sync.sync_account(acc_id)
        with connect() as conn:
            r = conn.execute("SELECT * FROM messages WHERE subject = 'Rechnung'").fetchall()
            assert [x["folder_id"] for x in r] == [folders["Geschäftlich"]["id"]]
            # Entwurf speichern, ersetzen, löschen
            if "drafts" in folders:
                msg, _ = compose.build(account, "a@b.de", "", "", "Entwurf", "eins", [], draft_id="<d1@test.de>")
                sync.save_draft(conn, account, bytes(msg), "<d1@test.de>")
                msg, _ = compose.build(account, "a@b.de", "", "", "Entwurf", "zwei", [], draft_id="<d1@test.de>")
                sync.save_draft(conn, account, bytes(msg), "<d1@test.de>")
                with sync.open_client(account) as c:
                    c.select(folders["drafts"]["path"])
                    assert len(c.uids_by_message_id("<d1@test.de>")) == 1
                sync.delete_draft(conn, account, "<d1@test.de>")
                with sync.open_client(account) as c:
                    c.select(folders["drafts"]["path"])
                    assert c.uids_by_message_id("<d1@test.de>") == []
    crypto._cache.clear()


def test_real_server_rules(tmp_path):
    import json
    host, port, sec, user, pw = _cfg()
    _reset_and_fill()
    with fresh_db(tmp_path):
        crypto._cache.clear()
        with connect() as conn:
            acc_id = conn.execute(
                "INSERT INTO accounts(owner_id, name, email, imap_host, imap_port, imap_security, imap_user, smtp_host, "
                "secret, created_at) VALUES (1, 'Test', ?, ?, ?, ?, ?, '', ?, ?)",
                (user, host, port, sec, user, crypto.pack_secrets(pw), now_utc_str())).lastrowid
        assert sync.sync_account(acc_id)["error"] is None
        with connect() as conn:
            for name, conds, acts in (
                    ("Angebote", [{"field": "subject", "op": "contains", "value": "angebot"}],
                     [{"type": "move", "folder": "Gesch&AOQ-ftlich"}]),
                    ("Spam", [{"field": "body", "op": "contains", "value": "casino"}], [{"type": "trash"}])):
                conn.execute("INSERT INTO rules(owner_id, account_id, name, conditions, actions, created_at) "
                             "VALUES (1, ?, ?, ?, ?, ?)", (acc_id, name, json.dumps(conds), json.dumps(acts), now_utc_str()))
        c = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context()) if sec == "ssl" else imaplib.IMAP4(host, port)
        c.login(user, pw)
        now = imaplib.Time2Internaldate(time.time())
        c.append("INBOX", "()", now, mail(subject="Neues Angebot Server"))
        c.append("INBOX", "()", now, mail(subject="Gewinn", text="Besuchen Sie unser Casino"))
        c.append("INBOX", "()", now, mail(subject="Normal"))
        c.logout()
        r = sync.sync_account(acc_id)
        assert r["rules"]["moved"] == 1 and r["rules"]["trashed"] == 1, r
        while sync.is_running(acc_id):
            time.sleep(0.1)
        sync.sync_account(acc_id)
        with connect() as conn:
            where = {m["subject"]: m["role"] or m["name"] for m in conn.execute(
                "SELECT m.subject, f.role, f.name FROM messages m JOIN folders f ON f.id = m.folder_id")}
        assert where["Neues Angebot Server"] == "Geschäftlich" and where["Gewinn"] == "trash"
        assert where["Normal"] == "inbox" and where["Rechnung"] == "inbox"   # vorhandene Mails bleiben
    crypto._cache.clear()


@pytest.mark.skipif(not os.environ.get("PF_TEST_XOAUTH2_TOKEN"), reason="PF_TEST_XOAUTH2_TOKEN nicht gesetzt")
def test_real_server_xoauth2():
    """Anmeldung per XOAUTH2 wie bei Microsoft 365 (Testserver muss den Token kennen, z. B. Dovecot mit oauth2-passdb)."""
    host, port, sec, user, _pw = _cfg()
    with imapclient.Client(host, port, sec, user, "", oauth_token=os.environ["PF_TEST_XOAUTH2_TOKEN"]) as c:
        assert any(f["path"] == "INBOX" for f in c.list_folders())
    with pytest.raises(imapclient.ImapError) as e:
        imapclient.Client(host, port, sec, user, "", oauth_token="falscher-token")
    assert "Microsoft hat die Anmeldung" in str(e.value)
