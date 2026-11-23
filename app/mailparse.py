"""Mails zerlegen: Kopfzeilen, Text, HTML und Anhänge – nur Standardbibliothek (email-Paket).

Fehlerhafte Mails (kaputte Kopfzeilen, unbekannte Zeichensätze) dürfen nie den Abruf stoppen:
im Zweifel wird mit Ersatzzeichen dekodiert.
"""
import email
import email.policy
import html
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

SNIPPET_LEN = 200


def _decode_header_value(value) -> str:
    if value is None:
        return ""
    try:
        text = str(make_header(decode_header(str(value))))
    except (UnicodeError, LookupError, ValueError, email.errors.HeaderParseError):
        text = str(value)
    return re.sub(r"[\r\n\t]+", " ", text).strip()


def header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except (UnicodeError, LookupError, ValueError, IndexError, email.errors.HeaderParseError):
        value = None
    return _decode_header_value(value)


def addresses(msg: Message, name: str) -> List[Tuple[str, str]]:
    raw = header(msg, name)
    if not raw:
        return []
    try:
        return [(n.strip(), a.strip()) for n, a in getaddresses([raw]) if a.strip()]
    except (ValueError, IndexError):
        return []


def format_addresses(pairs: List[Tuple[str, str]]) -> str:
    out = []
    for name, addr in pairs:
        name = name.replace('"', "'")
        out.append(f'"{name}" <{addr}>' if name and name != addr else addr)
    return ", ".join(out)


def parse_date(msg: Message, fallback: Optional[datetime] = None) -> datetime:
    raw = header(msg, "Date")
    dt = None
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is None:
        dt = fallback or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def parse_internaldate(value: str) -> Optional[datetime]:
    """IMAP-INTERNALDATE „17-Jul-2026 02:44:25 -0700“."""
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _ids(value: str) -> str:
    return " ".join(re.findall(r"<[^<>\s]+>", value or ""))


def parse_headers(raw: bytes, internaldate: str = "") -> Dict:
    """Kopfzeilen einer Nachricht (aus BODY[HEADER.FIELDS …] oder der ganzen Mail)."""
    msg = email.message_from_bytes(raw or b"", policy=email.policy.compat32)
    frm = addresses(msg, "From")
    from_name, from_addr = frm[0] if frm else ("", "")
    if not from_addr:
        from_name, from_addr = parseaddr(header(msg, "From"))
    ctype = (header(msg, "Content-Type") or "").lower()
    return {
        "subject": header(msg, "Subject"),
        "from_name": from_name,
        "from_addr": from_addr,
        "to_addrs": format_addresses(addresses(msg, "To")),
        "cc_addrs": format_addresses(addresses(msg, "Cc")),
        "reply_to": format_addresses(addresses(msg, "Reply-To")),
        "message_id": (_ids(header(msg, "Message-ID")).split() or [""])[0],
        "in_reply_to": (_ids(header(msg, "In-Reply-To")).split() or [""])[0],
        "refs": _ids(header(msg, "References"))[:2000],
        "date": parse_date(msg, parse_internaldate(internaldate)),
        # Vermutung bis zum Laden des Inhalts: multipart/mixed hat fast immer Anhänge
        "has_attachments": int(ctype.startswith("multipart/mixed") or ctype.startswith("application/")),
    }


# --- Inhalt ------------------------------------------------------------------

def _payload_text(part: Message) -> str:
    try:
        data = part.get_payload(decode=True) or b""
    except (ValueError, AssertionError):
        data = b""
    charset = part.get_content_charset() or "utf-8"
    for cs in (charset, "utf-8", "cp1252"):
        try:
            return data.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1", "replace")


def _filename(part: Message) -> str:
    try:
        name = part.get_filename()
    except (UnicodeError, LookupError, ValueError, IndexError):
        name = None
    if not name:
        ct = part.get_params(header="content-type") or []
        name = next((v for k, v in ct if k.lower() == "name"), None) if ct else None
        if isinstance(name, tuple):
            name = email.utils.collapse_rfc2231_value(name)
    return _decode_header_value(name) if name else ""


def _leaves(part: Message):
    """Alle Endteile in Reihenfolge – eingebettete Mails (message/rfc822) bleiben als Ganzes ein Anhang."""
    if part.get_content_maintype() == "message":
        yield part
    elif part.is_multipart():
        payload = part.get_payload()
        for sub in payload if isinstance(payload, list) else []:
            yield from _leaves(sub)
    else:
        yield part


def _part_bytes(part: Message) -> bytes:
    if part.get_content_maintype() == "message":
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            return payload[0].as_bytes()
        return part.as_bytes()
    try:
        return part.get_payload(decode=True) or b""
    except (ValueError, AssertionError):
        return b""


def _attachment_parts(msg: Message):
    """(Teil, ist_inline) für alles, was nicht der eigentliche Text/HTML-Inhalt ist."""
    have_text = have_html = False
    for part in _leaves(msg):
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        name = _filename(part)
        if disp != "attachment" and not name:
            if ctype == "text/plain" and not have_text:
                have_text = True
                continue
            if ctype == "text/html" and not have_html:
                have_html = True
                continue
            if ctype in ("text/plain", "text/html"):
                continue  # weitere Textteile (z. B. Fußzeilen von Mailinglisten) werden angehängt, nicht als Datei gezeigt
        yield part, disp == "inline" or bool(part.get("Content-ID") and disp != "attachment")


def html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_body(raw: bytes) -> Dict:
    """Text, HTML, Vorschau und Anhangsliste einer ganzen Mail."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    texts, htmls = [], []
    for part in _leaves(msg):
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        if disp == "attachment" or _filename(part):
            continue
        if ctype == "text/plain":
            texts.append(_payload_text(part))
        elif ctype == "text/html":
            htmls.append(_payload_text(part))
    attachments = []
    for i, (part, inline) in enumerate(_attachment_parts(msg)):
        name = _filename(part)
        ctype = part.get_content_type()
        if not name:
            ext = {"message/rfc822": ".eml", "message/delivery-status": ".txt", "text/calendar": ".ics", "image/png": ".png", "image/jpeg": ".jpg",
                   "image/gif": ".gif", "application/pdf": ".pdf"}.get(ctype, ".bin")
            name = f"Anhang {i + 1}{ext}"
        attachments.append({
            "idx": i,
            "filename": name[:200],
            "mime": ctype,
            "size": len(_part_bytes(part)),
            "content_id": (part.get("Content-ID") or "").strip().strip("<>")[:200],
            "inline": int(inline),
        })
    text = "\n\n".join(t for t in texts if t.strip())
    body_html = "\n".join(htmls)
    plain = text if text.strip() else html_to_text(body_html)
    snippet = re.sub(r"\s+", " ", re.sub(r"(?m)^>.*$", "", plain)).strip()[:SNIPPET_LEN]
    visible = [a for a in attachments if not (a["inline"] and a["content_id"] and a["mime"].startswith("image/"))]
    return {"text": text or (plain if not body_html else ""), "html": body_html, "snippet": snippet,
            "search_text": plain, "attachments": attachments, "has_attachments": int(bool(visible))}


def attachment_data(raw: bytes, idx: int) -> Optional[Tuple[str, str, bytes]]:
    """(Dateiname, MIME-Typ, Inhalt) des idx-ten Anhangs – gleiche Zählung wie in parse_body."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    for i, (part, _inline) in enumerate(_attachment_parts(msg)):
        if i == idx:
            return _filename(part) or f"Anhang {i + 1}", part.get_content_type(), _part_bytes(part)
    return None


def inline_images(raw: bytes, max_bytes: int = 3 * 1024 * 1024) -> Dict[str, Tuple[str, bytes]]:
    """Content-ID → (MIME-Typ, Bild) für eingebettete Bilder (cid:…) im HTML."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    out = {}
    for part, _inline in _attachment_parts(msg):
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if cid and part.get_content_type() in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            data = _part_bytes(part)
            if len(data) <= max_bytes:
                out[cid] = (part.get_content_type(), data)
    return out
