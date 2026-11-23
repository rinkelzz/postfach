"""Mails schreiben und verschicken: Antworten, Allen antworten, Weiterleiten, Versand per SMTP, Ablage in „Gesendet“."""
import base64
import re
import smtplib
import socket
import sqlite3
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Dict, List, Optional, Tuple

from . import crypto, imapclient, mailparse
from .mailer import valid_email
from .timeutil import parse_utc, to_local

MAX_TOTAL = 25 * 1024 * 1024
MAX_RECIPIENTS = 100
TIMEOUT = 45
# Diese Anbieter legen gesendete Mails selbst in „Gesendet“ ab – sonst stünde jede Mail doppelt da
AUTO_SENT_HOSTS = ("smtp.gmail.com", "smtp.office365.com", "smtp-mail.outlook.com")


class SendError(Exception):
    pass


def split_addresses(value: str) -> List[Tuple[str, str]]:
    """„Anna <a@x.de>; b@y.de“ → [(Anna, a@x.de), ("", b@y.de)] – Semikolon zählt wie Komma."""
    value = (value or "").replace(";", ",").replace("\n", ",")
    pairs = [(n.strip(), a.strip()) for n, a in getaddresses([value]) if a.strip()]
    bad = [a for _, a in pairs if not valid_email(a)]
    if bad:
        raise SendError("Ungültige Adresse: " + ", ".join(bad[:3]))
    return pairs


def _lenient(value: str) -> List[Tuple[str, str]]:
    value = (value or "").replace(";", ",").replace("\n", ",")
    return [(n.strip(), a.strip()) for n, a in getaddresses([value]) if a.strip()]


def new_draft_id(account: sqlite3.Row) -> str:
    return make_msgid(idstring="entwurf", domain=account["email"].split("@")[-1])


def _clean(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", s or "").strip()


def build(account: sqlite3.Row, to: str, cc: str, bcc: str, subject: str, body: str,
          attachments: List[Tuple[str, str, bytes]], in_reply_to: str = "", references: str = "",
          draft_id: str = "") -> Tuple[EmailMessage, List[str]]:
    """Mail bauen. Mit draft_id als Entwurf: Empfänger dürfen fehlen, Bcc bleibt (nur im eigenen Entwürfe-Ordner) stehen."""
    if draft_id:  # im Entwurf darf eine Adresse noch halb getippt sein
        to_p, cc_p, bcc_p = (_lenient(v) for v in (to, cc, bcc))
    else:
        to_p, cc_p, bcc_p = split_addresses(to), split_addresses(cc), split_addresses(bcc)
    rcpts = [a for _, a in to_p + cc_p + bcc_p]
    if not rcpts and not draft_id:
        raise SendError("Bitte mindestens einen Empfänger angeben")
    if len(rcpts) > MAX_RECIPIENTS:
        raise SendError(f"Höchstens {MAX_RECIPIENTS} Empfänger pro Mail")
    total = sum(len(d) for _, _, d in attachments)
    if total > MAX_TOTAL:
        raise SendError("Die Anhänge sind zusammen größer als 25 MB")
    msg = EmailMessage()
    msg["From"] = formataddr((_clean(account["from_name"]), account["email"])) if account["from_name"] else account["email"]
    if to_p:
        msg["To"] = ", ".join(formataddr(p) for p in to_p)
    if cc_p:
        msg["Cc"] = ", ".join(formataddr(p) for p in cc_p)
    msg["Subject"] = _clean(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = draft_id or make_msgid(domain=account["email"].split("@")[-1])
    if draft_id and bcc_p:
        msg["Bcc"] = ", ".join(formataddr(p) for p in bcc_p)
    if in_reply_to:
        msg["In-Reply-To"] = _clean(in_reply_to)
    if references:
        msg["References"] = _clean(references)[:4000]
    msg["User-Agent"] = "Postfach (Synology)"
    msg.set_content(body or "")
    for name, mime, data in attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        if maintype == "message" and subtype == "rfc822":
            maintype, subtype = "application", "octet-stream"  # .eml als Datei anhängen
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                           filename=_clean(name) or "anhang")
    return msg, rcpts


def smtp_send(account: sqlite3.Row, msg: EmailMessage, rcpts: List[str], password: Optional[str] = None) -> None:
    if password is None:
        password = crypto.unpack_secrets(account["secret"])["smtp"]
    host = account["smtp_host"].strip()
    if not host:
        raise SendError("Für dieses Konto ist kein Postausgangsserver (SMTP) eingetragen")
    user = account["smtp_user"] or account["imap_user"]
    try:
        ctx = ssl.create_default_context()
        if account["smtp_security"] == "ssl":
            server = smtplib.SMTP_SSL(host, account["smtp_port"], timeout=TIMEOUT, context=ctx)
        else:
            server = smtplib.SMTP(host, account["smtp_port"], timeout=TIMEOUT)
        with server:
            server.ehlo()
            if account["smtp_security"] == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            # Ohne Verschlüsselung (Relay im LAN) bietet der Server oft gar keine Anmeldung an – dann ohne
            if user and password and (account["smtp_security"] != "none" or server.has_extn("auth")):
                server.login(user, password)
            if msg is not None:
                server.send_message(msg, from_addr=account["email"], to_addrs=rcpts)
    except smtplib.SMTPAuthenticationError:
        raise SendError("Anmeldung am Postausgangsserver fehlgeschlagen – Benutzer und Passwort prüfen")
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError("Empfänger abgelehnt: " + ", ".join(list(e.recipients)[:3]))
    except smtplib.SMTPSenderRefused:
        raise SendError("Der Server nimmt die Absenderadresse nicht an – stimmt die Mailadresse des Kontos?")
    except smtplib.SMTPNotSupportedError:
        raise SendError("Der Server unterstützt die gewählte Verschlüsselung nicht – Port und Verschlüsselung prüfen")
    except (smtplib.SMTPException, OSError, socket.timeout) as e:
        if isinstance(e, smtplib.SMTPException):
            raise SendError(f"Versand fehlgeschlagen: {e}")
        raise SendError(imapclient.friendly_error(e).replace("Mailserver", "Postausgangsserver"))


def test_smtp(account: sqlite3.Row, password: str) -> None:
    """Nur anmelden, nichts verschicken."""
    smtp_send(account, None, [], password)


def save_sent(conn: sqlite3.Connection, account: sqlite3.Row, msg: EmailMessage, client_factory) -> Optional[str]:
    """Gesendete Mail im „Gesendet“-Ordner ablegen. Liefert einen Hinweis, wenn das nicht ging."""
    if account["smtp_host"].strip().lower() in AUTO_SENT_HOSTS:
        return None
    folder = conn.execute("SELECT path FROM folders WHERE account_id = ? AND role = 'sent'", (account["id"],)).fetchone()
    if not folder:
        return "Kein „Gesendet“-Ordner gefunden – die Mail wurde verschickt, aber nicht abgelegt."
    raw = bytes(msg)
    try:
        with client_factory(account) as client:
            client.append(folder["path"], raw)
    except Exception as e:  # noqa: BLE001
        return "Verschickt, aber nicht in „Gesendet“ abgelegt: " + imapclient.friendly_error(e)
    return None


def decode_upload(items) -> List[Tuple[str, str, bytes]]:
    out = []
    for it in items or []:
        try:
            data = base64.b64decode(it.data_b64.split(",", 1)[-1], validate=False)
        except (ValueError, TypeError):
            raise SendError(f"Anhang „{it.name}“ ist beschädigt")
        out.append((it.name, it.mime or "application/octet-stream", data))
    return out


# --- Antworten und Weiterleiten ---------------------------------------------

def _prefix(subject: str, prefix: str) -> str:
    s = (subject or "").strip()
    if re.match(rf"^({prefix}|{'re' if prefix == 'AW' else 'fwd?'})\s*:", s, re.I):
        return s
    return f"{prefix}: {s}" if s else f"{prefix}:"


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in (text or "").splitlines())


def _when(date_utc: str) -> str:
    return to_local(parse_utc(date_utc)).strftime("%d.%m.%Y um %H:%M")


def _own(addr: str, own: set) -> bool:
    return addr.lower() in own


def draft(conn: sqlite3.Connection, account: sqlite3.Row, m: sqlite3.Row, mode: str, own_addresses: set) -> Dict:
    """Vorbefülltes Formular für reply | replyall | forward."""
    text = m["body_text"] or ""
    if not text.strip() and m["body_html"]:
        text = mailparse.html_to_text(m["body_html"])
    sender = mailparse.format_addresses([(m["from_name"], m["from_addr"])]) if m["from_addr"] else ""
    sig = ("\n\n-- \n" + account["signature"].strip()) if account["signature"].strip() else ""
    refs = " ".join(x for x in ((m["refs"] or "").split() + [m["message_id"]]) if x)[-4000:]
    if mode == "forward":
        head = (f"\n\n-------- Weitergeleitete Nachricht --------\nBetreff: {m['subject']}\nDatum: {_when(m['date_utc'])}\n"
                f"Von: {sender}\nAn: {m['to_addrs']}\n" + (f"Kopie: {m['cc_addrs']}\n" if m["cc_addrs"] else "") + "\n")
        atts = [dict(r) for r in conn.execute(
            "SELECT id, filename, mime, size FROM attachments WHERE message_id = ? AND NOT (inline = 1 AND content_id != '' "
            "AND mime LIKE 'image/%') ORDER BY idx", (m["id"],))]
        return {"account_id": account["id"], "to": "", "cc": "", "subject": _prefix(m["subject"], "WG"),
                "body": sig + head + text, "in_reply_to": "", "references": "", "forward_attachments": atts}
    reply_to = m["reply_to"] or sender
    to_pairs = [p for p in getaddresses([reply_to]) if p[1]]
    cc_pairs: List[Tuple[str, str]] = []
    if mode == "replyall":
        seen = {a.lower() for _, a in to_pairs} | own_addresses
        for n, a in getaddresses([m["to_addrs"], m["cc_addrs"]]):
            if a and a.lower() not in seen:
                cc_pairs.append((n, a))
                seen.add(a.lower())
    # Eigene, gesendete Mail beantworten → an die ursprünglichen Empfänger
    if to_pairs and all(_own(a, own_addresses) for _, a in to_pairs) and m["to_addrs"]:
        to_pairs = [p for p in getaddresses([m["to_addrs"]]) if p[1]]
    body = f"{sig}\n\nAm {_when(m['date_utc'])} schrieb {m['from_name'] or m['from_addr']}:\n{_quote(text)}\n"
    return {"account_id": account["id"], "to": mailparse.format_addresses(to_pairs),
            "cc": mailparse.format_addresses(cc_pairs), "subject": _prefix(m["subject"], "AW"), "body": body,
            "in_reply_to": m["message_id"], "references": refs, "forward_attachments": []}


def edit_draft(m: sqlite3.Row, raw: bytes) -> Dict:
    """Gespeicherten Entwurf wieder ins Formular laden – Anhänge kommen als Dateien mit (sie werden beim nächsten
    Speichern wieder in den Entwurf geschrieben, der alte wird dabei ersetzt)."""
    import email as _email
    msg = _email.message_from_bytes(raw, policy=_email.policy.compat32)
    body = mailparse.parse_body(raw)
    files = []
    for a in body["attachments"]:
        got = mailparse.attachment_data(raw, a["idx"])
        if got:
            files.append({"name": got[0], "mime": got[1], "size": len(got[2]),
                          "data_b64": base64.b64encode(got[2]).decode("ascii")})
    text = body["text"] if body["text"].strip() else mailparse.html_to_text(body["html"])
    return {"account_id": m["account_id"], "to": m["to_addrs"], "cc": m["cc_addrs"],
            "bcc": mailparse.format_addresses(mailparse.addresses(msg, "Bcc")), "subject": m["subject"], "body": text,
            "in_reply_to": m["in_reply_to"], "references": m["refs"], "files": files, "forward_attachments": [],
            "draft_message_id": m["message_id"]}
