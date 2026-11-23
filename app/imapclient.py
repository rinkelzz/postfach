"""Schlanker IMAP-Client auf Basis von imaplib (Standardbibliothek).

Kapselt alles, was Postfach vom Mailserver braucht: anmelden, Ordner auflisten, Nachrichten seit einem
Datum suchen, Kopfzeilen/Markierungen/ganze Nachrichten holen, Markierungen setzen und gesendete Mails
im „Gesendet“-Ordner ablegen. Ordnernamen kommen in „modified UTF-7“ (RFC 3501) – siehe utf7_decode.
"""
import base64
import imaplib
import re
import socket
import ssl
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

TIMEOUT = 45
CHUNK = 200
HEADER_FIELDS = ("DATE FROM TO CC REPLY-TO SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

imaplib._MAXLINE = 10_000_000  # lange Zeilen (große Ordnerlisten, SEARCH-Antworten) zulassen


class ImapError(Exception):
    pass


# --- modified UTF-7 (RFC 3501, Abschnitt 5.1.3) --------------------------------

def utf7_decode(name: str) -> str:
    out, i = [], 0
    while i < len(name):
        c = name[i]
        if c == "&":
            j = name.find("-", i)
            if j < 0:
                out.append(name[i:])
                break
            chunk = name[i + 1:j]
            if not chunk:
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except (ValueError, UnicodeDecodeError):
                    out.append(name[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def utf7_encode(name: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for c in name:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)


def quote(name: str) -> str:
    """Ordnernamen für IMAP-Befehle in Anführungszeichen setzen."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


# --- Verbindungen mit Zeitlimit (imaplib in Python 3.8 kennt noch kein timeout) ---

class _IMAP(imaplib.IMAP4):
    def _create_socket(self, timeout=None):
        return socket.create_connection((self.host, self.port), timeout or TIMEOUT)


class _IMAPS(imaplib.IMAP4_SSL):
    def _create_socket(self, timeout=None):
        sock = socket.create_connection((self.host, self.port), timeout or TIMEOUT)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def friendly_error(e: BaseException) -> str:
    """Technische Fehler in verständliche Sätze übersetzen."""
    text = str(e)
    if isinstance(e, ssl.SSLCertVerificationError):
        return "Das Zertifikat des Mailservers ist ungültig oder passt nicht zum Servernamen"
    if isinstance(e, ssl.SSLError):
        return f"Verschlüsselung fehlgeschlagen ({getattr(e, 'reason', None) or e}) – Port und Verschlüsselung prüfen"
    if isinstance(e, socket.gaierror):
        return "Mailserver nicht gefunden – Servername prüfen"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "Mailserver antwortet nicht (Zeitüberschreitung) – Servername und Port prüfen"
    if isinstance(e, ConnectionRefusedError):
        return "Verbindung abgelehnt – Port prüfen"
    if isinstance(e, imaplib.IMAP4.error):
        low = text.lower()
        if "authenticationfailed" in low or "login" in low or "auth" in low or "invalid credentials" in low:
            return "Anmeldung am Mailserver fehlgeschlagen – Benutzer und Passwort prüfen (bei Gmail ein App-Passwort)"
        return "Mailserver meldet: " + text.strip("b'\"")[:200]
    if isinstance(e, ImapError):
        return text
    if isinstance(e, OSError):
        return f"Verbindung zum Mailserver fehlgeschlagen: {e.strerror or text}"
    return text[:300]


class Client:
    """Eine angemeldete IMAP-Verbindung. Als Kontextmanager benutzen (meldet sich am Ende ab)."""

    def __init__(self, host: str, port: int, security: str, user: str, password: str):
        ctx = ssl.create_default_context()
        try:
            if security == "ssl":
                self.conn = _IMAPS(host, port, ssl_context=ctx)
            else:
                self.conn = _IMAP(host, port)
                if security == "starttls":
                    self.conn.starttls(ssl_context=ctx)
            self.conn.sock.settimeout(TIMEOUT)
            self.conn.login(user, password)
        except imaplib.IMAP4.error as e:
            if "LOGINDISABLED" in str(e) or "not supported" in str(e).lower():
                raise ImapError("Der Server erlaubt keine Anmeldung ohne Verschlüsselung – STARTTLS oder SSL wählen")
            raise
        self.capabilities = {c.decode() if isinstance(c, bytes) else str(c) for c in (self.conn.capabilities or ())}
        self.selected: Optional[str] = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001 – Verbindung ist ohnehin vorbei
            pass

    def _ok(self, typ, data, what: str):
        if typ != "OK":
            detail = (data[0].decode("utf-8", "replace") if data and isinstance(data[0], bytes) else str(data))[:200]
            raise ImapError(f"{what} fehlgeschlagen: {detail}")
        return data

    # --- Ordner ---------------------------------------------------------------

    def list_folders(self) -> List[Dict]:
        """[{path, name, delimiter, flags}] – ohne Ordner, die man nicht öffnen kann."""
        data = self._ok(*self.conn.list(), "Ordnerliste")
        out = []
        i = 0
        while i < len(data):
            item = data[i]
            i += 1
            if item is None:
                continue
            literal = None
            if isinstance(item, tuple):
                head, literal = item[0], item[1]
            else:
                head = item
            m = re.match(rb'\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) ?(?P<name>.*)$', head)
            if not m:
                continue
            flags = {f.decode("ascii", "replace").lower() for f in m.group("flags").split()}
            delim = m.group("delim")
            delim = "" if delim == b"NIL" else delim[1:-1].replace(b"\\\\", b"\\").replace(b'\\"', b'"').decode()
            if literal is not None:
                raw = literal
            else:
                raw = m.group("name").strip()
                if raw.startswith(b'"') and raw.endswith(b'"'):
                    raw = raw[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
            path = raw.decode("utf-8", "replace")
            if "\\noselect" in flags or "\\nonexistent" in flags:
                continue
            last = path.split(delim)[-1] if delim else path
            out.append({"path": path, "name": utf7_decode(last), "full_name": utf7_decode(path),
                        "delimiter": delim or "/", "flags": flags})
        return out

    def select(self, path: str, readonly: bool = True) -> Tuple[int, int]:
        """Ordner öffnen. Liefert (UIDVALIDITY, Anzahl Nachrichten)."""
        typ, data = self.conn.select(quote(path), readonly=readonly)
        self._ok(typ, data, f"Ordner „{utf7_decode(path)}“ öffnen")
        self.selected = path
        exists = int(data[0]) if data and data[0] and data[0].isdigit() else 0
        _, uv = self.conn.response("UIDVALIDITY")
        try:
            uidvalidity = int(uv[0]) if uv and uv[0] else 0
        except (TypeError, ValueError):
            uidvalidity = 0
        return uidvalidity, exists

    # --- Suchen und Holen -----------------------------------------------------

    def uids_since(self, since: date) -> List[int]:
        data = self._ok(*self.conn.uid("SEARCH", None, "SINCE", imap_date(since)), "Suche")
        return sorted({int(x) for x in b" ".join(d for d in data if d).split() if x.isdigit()})

    def fetch_headers(self, uids: Iterable[int]) -> List[Dict]:
        """Kopfzeilen, Markierungen, Größe und Eingangsdatum – in Paketen zu CHUNK Nachrichten."""
        out = []
        for chunk in _chunks(list(uids), CHUNK):
            typ, data = self.conn.uid("FETCH", _uidset(chunk),
                                      f"(UID FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
            out += parse_fetch(self._ok(typ, data, "Abruf der Kopfzeilen"))
        return out

    def fetch_flags(self, uids: Iterable[int]) -> Dict[int, set]:
        out = {}
        for chunk in _chunks(list(uids), 500):
            typ, data = self.conn.uid("FETCH", _uidset(chunk), "(UID FLAGS)")
            for rec in parse_fetch(self._ok(typ, data, "Abruf der Markierungen")):
                out[rec["uid"]] = rec["flags"]
        return out

    def fetch_message(self, uid: int) -> Optional[bytes]:
        """Ganze Nachricht (ohne sie als gelesen zu markieren). None, wenn es sie nicht mehr gibt."""
        typ, data = self.conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
        for rec in parse_fetch(self._ok(typ, data, "Abruf der Nachricht")):
            if rec["uid"] == uid and rec["literals"]:
                return rec["literals"][-1]
        return None

    def store_flags(self, uids: Iterable[int], flags: str, add: bool) -> None:
        uids = list(uids)
        if not uids:
            return
        typ, data = self.conn.uid("STORE", _uidset(uids), "+FLAGS.SILENT" if add else "-FLAGS.SILENT", f"({flags})")
        self._ok(typ, data, "Markierung setzen")

    def append(self, path: str, message: bytes, flags: str = "(\\Seen)") -> None:
        typ, data = self.conn.append(quote(path), flags, imaplib.Time2Internaldate(_now_ts()), message)
        self._ok(typ, data, f"Ablegen in „{utf7_decode(path)}“")


def _now_ts() -> float:
    import time
    return time.time()


def _chunks(seq: List[int], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _uidset(uids: List[int]) -> str:
    """1,2,3,7,8 → 1:3,7:8 (kürzere Befehle)."""
    uids = sorted(set(uids))
    parts, start, prev = [], None, None
    for u in uids:
        if start is None:
            start = prev = u
        elif u == prev + 1:
            prev = u
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = u
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


_NEW_MSG = re.compile(rb"^\d+ \(")


def parse_fetch(data) -> List[Dict]:
    """imaplib-FETCH-Antwort in Datensätze zerlegen: {uid, flags, size, internaldate, literals}.

    imaplib liefert eine Liste aus Tupeln (Kopf mit {n}, Literal) und Bytes (Rest/Abschluss). Je nach Server
    stehen FLAGS vor oder nach dem Literal – deshalb wird der Text eines Datensatzes erst gesammelt."""
    records, cur = [], None
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head, lit = item[0], item[1]
            if cur is None or _NEW_MSG.match(head):
                cur = {"meta": head, "literals": [lit]}
                records.append(cur)
            else:
                cur["meta"] += b" " + head
                cur["literals"].append(lit)
        else:
            if _NEW_MSG.match(item):
                cur = {"meta": item, "literals": []}
                records.append(cur)
            elif cur is not None:
                cur["meta"] += b" " + item
    out = []
    for r in records:
        meta = r["meta"]
        m = re.search(rb"UID (\d+)", meta)
        if not m:
            continue
        f = re.search(rb"FLAGS \(([^)]*)\)", meta)
        s = re.search(rb"RFC822\.SIZE (\d+)", meta)
        d = re.search(rb'INTERNALDATE "([^"]+)"', meta)
        out.append({
            "uid": int(m.group(1)),
            "flags": {x.decode("ascii", "replace").lower() for x in f.group(1).split()} if f else set(),
            "size": int(s.group(1)) if s else 0,
            "internaldate": d.group(1).decode("ascii", "replace") if d else "",
            "literals": r["literals"],
        })
    return out


# --- Rollen der Ordner -------------------------------------------------------

_ROLE_FLAGS = {"\\sent": "sent", "\\drafts": "drafts", "\\trash": "trash", "\\junk": "junk", "\\archive": "archive"}
_ROLE_NAMES = {
    "sent": ("sent", "sent items", "sent messages", "sent mail", "gesendet", "gesendete objekte", "gesendete elemente",
             "gesendete nachrichten"),
    "drafts": ("drafts", "draft", "entwürfe", "entwurf"),
    "trash": ("trash", "deleted", "deleted items", "deleted messages", "papierkorb", "gelöscht", "gelöschte objekte",
              "gelöschte elemente"),
    "junk": ("junk", "spam", "junk e-mail", "junk-e-mail", "bulk mail", "unerwünscht"),
    "archive": ("archive", "archiv", "all mail", "alle nachrichten"),
}
ROLE_ORDER = {"inbox": 0, "drafts": 1, "sent": 2, "archive": 3, "all": 4, "junk": 5, "trash": 6, "": 9}


def folder_role(folder: Dict) -> str:
    if folder["path"].upper() == "INBOX":
        return "inbox"
    for flag, role in _ROLE_FLAGS.items():
        if flag in folder["flags"]:
            return role
    if "\\all" in folder["flags"]:
        return "all"  # Gmail „Alle Nachrichten“: enthält jede Mail ein zweites Mal
    name = folder["name"].strip().lower()
    for role, names in _ROLE_NAMES.items():
        if name in names:
            return role
    return ""


def assign_roles(folders: List[Dict]) -> None:
    """Rolle je Ordner setzen – jede Rolle nur einmal (Sonderordner-Markierung schlägt Namensraten)."""
    taken = set()
    for f in sorted(folders, key=lambda f: (0 if any(fl in f["flags"] for fl in list(_ROLE_FLAGS) + ["\\all"]) else 1,
                                            len(f["path"]))):
        role = folder_role(f)
        if role and role in taken:
            role = ""
        f["role"] = role
        if role:
            taken.add(role)
