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
    for box in ("INBOX", "Sent", "Gesch&AOQ-ftlich"):
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
