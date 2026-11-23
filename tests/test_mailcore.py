"""Mail-Kern ohne Server: Verschlüsselung, Zerlegen, HTML-Filter, IMAP-Antworten, Anbieter-Vorschläge."""
import stat

import pytest

from app import crypto, htmlsafe, imapclient, mailparse, providers
from app.config import settings

from .mailfake import PNG, mail


# --- Verschlüsselung --------------------------------------------------------

def test_crypto_roundtrip_and_tamper():
    tok = crypto.encrypt("Pässwört 123 🔒")
    assert tok.startswith("v1:") and "Pässwört" not in tok
    assert crypto.decrypt(tok) == "Pässwört 123 🔒"
    assert crypto.encrypt("x") != crypto.encrypt("x")  # jedes Mal neue Nonce
    raw = bytearray(tok.encode())
    raw[10] = ord("A") if raw[10] != ord("A") else ord("B")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(raw.decode())
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt("klartext")
    assert stat.S_IMODE(crypto.key_path().stat().st_mode) == 0o600
    assert crypto.key_path().parent == settings.data_dir


def test_crypto_secrets_fallback():
    s = crypto.unpack_secrets(crypto.pack_secrets("imap-pw"))
    assert s == {"imap": "imap-pw", "smtp": "imap-pw"}
    s = crypto.unpack_secrets(crypto.pack_secrets("imap-pw", "smtp-pw"))
    assert s == {"imap": "imap-pw", "smtp": "smtp-pw"}
    assert crypto.unpack_secrets("") == {"imap": "", "smtp": ""}


def test_crypto_broken_keyfile_is_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    crypto._cache.clear()
    try:
        (tmp_path / "secret.key").write_text("kaputt")
        with pytest.raises(crypto.CryptoError):
            crypto.encrypt("x")
        assert (tmp_path / "secret.key").read_text() == "kaputt"
    finally:
        crypto._cache.clear()


# --- Mails zerlegen ---------------------------------------------------------

def test_parse_headers_encoded():
    raw = (b"From: =?utf-8?q?J=C3=BCrgen_M=C3=BCller?= <juergen@example.de>\r\n"
           b"To: tim@test.de, \"Anna, B.\" <anna@example.de>\r\n"
           b"Subject: =?iso-8859-1?q?Gr=FC=DFe_aus_K=F6ln?=\r\n"
           b"Date: Mon, 23 Nov 2026 10:00:00 +0100\r\nMessage-ID: <abc@x>\r\nIn-Reply-To: <vorher@x>\r\n"
           b"References: <eins@x> <vorher@x>\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n")
    h = mailparse.parse_headers(raw)
    assert h["subject"] == "Grüße aus Köln"
    assert (h["from_name"], h["from_addr"]) == ("Jürgen Müller", "juergen@example.de")
    assert h["to_addrs"] == 'tim@test.de, "Anna, B." <anna@example.de>'
    assert h["message_id"] == "<abc@x>" and h["in_reply_to"] == "<vorher@x>" and h["refs"] == "<eins@x> <vorher@x>"
    assert h["date"].isoformat() == "2026-11-23T09:00:00+00:00"
    assert h["has_attachments"] == 1


def test_parse_headers_broken_never_raises():
    h = mailparse.parse_headers(b"From: \xff\xfe kaputt\r\nSubject: =?unbekannt?q?x?=\r\nDate: gestern\r\n\r\n",
                                "17-Jul-2026 02:44:25 -0700")
    assert h["date"].isoformat() == "2026-07-17T09:44:25+00:00"  # Rückfall auf INTERNALDATE
    assert isinstance(h["subject"], str)


def test_parse_body_alternative_inline_and_attachments():
    raw = mail(text="Nur Text", html='<p>Hallo <img src="cid:logo1"></p>', inline=[("logo1", PNG)],
               attachments=[("Rechnung März.pdf", b"%PDF-1.4 test", "application/pdf")])
    b = mailparse.parse_body(raw)
    assert b["text"].strip() == "Nur Text" and "cid:logo1" in b["html"]
    names = [(a["filename"], a["inline"], a["content_id"]) for a in b["attachments"]]
    assert ("Rechnung März.pdf", 0, "") in names and any(cid == "logo1" for _, _, cid in names)
    assert b["has_attachments"] == 1
    idx = next(a["idx"] for a in b["attachments"] if a["filename"] == "Rechnung März.pdf")
    assert mailparse.attachment_data(raw, idx) == ("Rechnung März.pdf", "application/pdf", b"%PDF-1.4 test")
    assert mailparse.inline_images(raw) == {"logo1": ("image/png", PNG)}


def test_parse_body_html_only_and_only_inline_image():
    from email.message import EmailMessage
    only = EmailMessage()
    only["Subject"] = "nur HTML"
    only.set_content("<html><head><style>p{}</style></head><body><p>Erste&nbsp;Zeile</p><p>Zweite</p></body></html>",
                     subtype="html")
    b = mailparse.parse_body(bytes(only))
    assert b["text"] == "" and "Erste Zeile" in b["search_text"] and "p{}" not in b["search_text"]
    assert b["snippet"].startswith("Erste Zeile")
    # nur ein eingebettetes Bild → zählt nicht als Anhang (Büroklammer)
    raw = mail(text="x", html='<img src="cid:a">', inline=[("a", PNG)])
    assert mailparse.parse_body(raw)["has_attachments"] == 0


def test_parse_body_attached_mail_stays_one_attachment():
    inner = mail(subject="Innen", attachments=[("innen.txt", b"x", "text/plain")])
    from email.message import EmailMessage
    import email
    outer = EmailMessage()
    outer["Subject"] = "Außen"
    outer.set_content("siehe Anhang")
    outer.add_attachment(email.message_from_bytes(inner))
    b = mailparse.parse_body(bytes(outer))
    assert [a["mime"] for a in b["attachments"]] == ["message/rfc822"]
    assert b["attachments"][0]["filename"].endswith(".eml")
    assert "siehe Anhang" in b["text"] and "innen" not in b["text"]


def test_snippet_skips_quotes():
    b = mailparse.parse_body(mail(text="Danke!\n\nAm Montag schrieb X:\n> alter Text\n> noch mehr"))
    assert b["snippet"].startswith("Danke!") and "alter Text" not in b["snippet"]


# --- HTML-Filter ------------------------------------------------------------

def test_sanitize_removes_active_content():
    dirty = ('<p onclick="x()">Hi<script>alert(1)</script><a href="javascript:alert(1)">a</a>'
             '<a href="https://ok.de/x?a=1&b=2">ok</a><img src=x onerror=alert(1)>'
             '<iframe src="https://böse.de"></iframe><svg><script>1</script></svg><form action="x"><input name=a>Feld</form>'
             '<object data="x"></object><!-- kommentar --><meta http-equiv="refresh" content="0;url=x"></p>')
    clean, _ = htmlsafe.sanitize(dirty)
    low = clean.lower()
    for bad in ("<script", "onclick", "onerror", "javascript:", "<iframe", "<svg", "<form", "<input", "<object", "<meta",
                "kommentar", "alert(1)"):
        assert bad not in low, bad
    assert '<a href="https://ok.de/x?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">ok</a>' in clean
    assert "Feld" in clean and "Hi" in clean


def test_sanitize_blocks_remote_until_allowed():
    html = ('<img src="https://tracker.example/pixel.gif?u=tim"><td background="http://x/bg.png">'
            '<div style="background:url(https://x/y.png); color:red">t</div>'
            '<style>@import url(https://x/a.css); body{background:url("//x/b.png")}</style>')
    clean, blocked = htmlsafe.sanitize(html)
    assert blocked and "tracker.example" not in clean and "x/bg.png" not in clean and "x/y.png" not in clean
    assert "@import" not in clean and "//x/b.png" not in clean and "color:red" in clean
    clean2, blocked2 = htmlsafe.sanitize(html, allow_remote=True)
    assert "https://tracker.example/pixel.gif?u=tim" in clean2 and "https://x/y.png" in clean2
    assert "@import" not in clean2
    assert "img-src data:;" in htmlsafe.csp(False) and "https:" in htmlsafe.csp(True)


def test_sanitize_cid_and_style_breakout():
    clean, blocked = htmlsafe.sanitize('<img src="cid:logo1"><img src="cid:fehlt">', inline_images={"logo1": ("image/png", PNG)})
    assert 'src="data:image/png;base64,' in clean and "cid:" not in clean and not blocked
    clean, _ = htmlsafe.sanitize("<style>p{color:red}</style><style>x{}</style>")
    assert clean.count("<style>") == 2
    clean, _ = htmlsafe.sanitize('<p title="&quot;><script>">x</p>')
    assert "<script" not in clean


# --- IMAP-Hilfen ------------------------------------------------------------

def test_utf7():
    assert imapclient.utf7_decode("Gesch&AOQ-ftlich") == "Geschäftlich"
    assert imapclient.utf7_encode("Geschäftlich") == "Gesch&AOQ-ftlich"
    for s in ("Entwürfe", "A&B", "日本語", "Ordner/Unter ordner", "~peter/mail/台北"):
        assert imapclient.utf7_decode(imapclient.utf7_encode(s)) == s
    assert imapclient.utf7_encode("A&B") == "A&-B"


def test_uidset_and_parse_fetch():
    assert imapclient._uidset([5, 1, 2, 3, 7, 8, 10]) == "1:3,5,7:8,10"
    data = [(b'1 (UID 42 RFC822.SIZE 1234 INTERNALDATE "17-Jul-2026 02:44:25 -0700" BODY[HEADER.FIELDS (SUBJECT)] {20}',
             b"Subject: Hallo\r\n\r\n"), b" FLAGS (\\Seen \\Flagged))",
            (b"2 (UID 43 FLAGS () BODY[] {3}", b"abc"), b")",
            b"3 (UID 44 FLAGS (\\Answered))"]
    recs = imapclient.parse_fetch(data)
    assert [r["uid"] for r in recs] == [42, 43, 44]
    assert recs[0]["flags"] == {"\\seen", "\\flagged"} and recs[0]["size"] == 1234
    assert recs[0]["literals"] == [b"Subject: Hallo\r\n\r\n"] and recs[1]["literals"] == [b"abc"]
    assert recs[2]["flags"] == {"\\answered"}


def test_roles():
    fl = [{"path": p, "name": imapclient.utf7_decode(p.split("/")[-1]), "flags": f} for p, f in (
        ("INBOX", set()), ("Gesendete Objekte", set()), ("Sent", {"\\sent"}), ("Entw&APw-rfe", set()),
        ("Papierkorb", set()), ("Spam", set()), ("Kunden/Rechnungen", set()), ("[Gmail]/Alle Nachrichten", {"\\all"}))]
    imapclient.assign_roles(fl)
    roles = {f["path"]: f["role"] for f in fl}
    assert roles["INBOX"] == "inbox" and roles["Sent"] == "sent" and roles["Gesendete Objekte"] == ""
    assert roles["Entw&APw-rfe"] == "drafts" and roles["Papierkorb"] == "trash" and roles["Spam"] == "junk"
    assert roles["Kunden/Rechnungen"] == "" and roles["[Gmail]/Alle Nachrichten"] == "all"


def test_providers():
    g = providers.suggest("Max@GMAIL.com")
    assert g["known"] and g["imap_host"] == "imap.gmail.com" and "App-Passwort" in g["hint"]
    assert providers.suggest("info@praxis-henze.de")["imap_host"] == "imap.praxis-henze.de"
    assert providers.suggest("max@icloud.com")["imap_user"] == "max"
    assert providers.suggest("kaputt") is None
