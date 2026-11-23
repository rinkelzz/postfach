"""Nachgebauter IMAP-Server für Tests (gleiche Schnittstelle wie imapclient.Client) und Beispielmails."""
import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from app import imapclient


class FakeServer:
    def __init__(self):
        self.folders = {
            "INBOX": {"flags": set(), "uidvalidity": 1, "next": 1, "msgs": {}},
            "Sent": {"flags": {"\\sent"}, "uidvalidity": 1, "next": 1, "msgs": {}},
            "Gesch&AOQ-ftlich": {"flags": set(), "uidvalidity": 1, "next": 1, "msgs": {}},
            "Trash": {"flags": {"\\trash"}, "uidvalidity": 1, "next": 1, "msgs": {}},
        }
        self.logins = []
        self.fail_login = False
        self.capabilities = {"IDLE", "MOVE", "UIDPLUS"}

    def add(self, path, raw, flags=(), when=None):
        f = self.folders[path]
        uid = f["next"]
        f["next"] += 1
        if when is None:  # Eingangsdatum = Date-Kopfzeile (wie bei echten Servern meist ähnlich)
            import email
            from email.utils import parsedate_to_datetime
            date = email.message_from_bytes(raw).get("Date")
            when = parsedate_to_datetime(date) if date else datetime.now(timezone.utc)
        f["msgs"][uid] = {"raw": raw, "flags": {x.lower() for x in flags}, "when": when}
        return uid


class FakeClient:
    def __init__(self, server: FakeServer, user="", password=""):
        if server.fail_login:
            raise imapclient.ImapError("Anmeldung am Mailserver fehlgeschlagen")
        self.server = server
        server.logins.append(user)
        self.selected = None
        self.capabilities = set(server.capabilities)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def close(self):
        pass

    def list_folders(self):
        out = []
        for path, f in self.server.folders.items():
            out.append({"path": path, "name": imapclient.utf7_decode(path), "full_name": imapclient.utf7_decode(path),
                        "delimiter": "/", "flags": set(f["flags"])})
        return out

    def select(self, path, readonly=True):
        self.selected = path
        f = self.server.folders[path]
        return f["uidvalidity"], len(f["msgs"])

    def _f(self):
        return self.server.folders[self.selected]

    def uids_since(self, since):
        return sorted(u for u, m in self._f()["msgs"].items() if m["when"].date() >= since)

    def fetch_headers(self, uids):
        out = []
        for u in uids:
            m = self._f()["msgs"].get(u)
            if m:
                head = m["raw"].split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                out.append({"uid": u, "flags": set(m["flags"]), "size": len(m["raw"]),
                            "internaldate": m["when"].strftime("%d-%b-%Y %H:%M:%S +0000"), "literals": [head]})
        return out

    def fetch_flags(self, uids):
        return {u: set(self._f()["msgs"][u]["flags"]) for u in uids if u in self._f()["msgs"]}

    def fetch_message(self, uid):
        m = self._f()["msgs"].get(uid)
        return m["raw"] if m else None

    def store_flags(self, uids, flags, add):
        for u in uids:
            s = self._f()["msgs"][u]["flags"]
            (s.add if add else s.discard)(flags.lower())

    def append(self, path, message, flags="(\\Seen)"):
        self.server.add(path, message, [f for f in flags.strip("()").split() if f])

    def move(self, uids, dest):
        for u in list(uids):
            m = self._f()["msgs"].pop(u)
            self.server.add(dest, m["raw"], m["flags"], m["when"])

    def delete(self, uids):
        for u in list(uids):
            self._f()["msgs"].pop(u, None)

    def uids_by_message_id(self, message_id):
        import email
        return sorted(u for u, m in self._f()["msgs"].items()
                      if email.message_from_bytes(m["raw"]).get("Message-ID", "").strip() == message_id)


def mail(subject="Hallo", frm=("Anna Absender", "anna@example.de"), to="tim@test.de", text="Hallo Tim,\nwie geht's?",
         html=None, attachments=(), inline=(), days_ago=0, message_id=None, cc=None, reply_to=None):
    msg = EmailMessage()
    msg["From"] = f'"{frm[0]}" <{frm[1]}>' if frm[0] else frm[1]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))
    msg["Message-ID"] = message_id or f"<{abs(hash((subject, days_ago, text)))}@example.de>"
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
        for cid, data in inline:
            msg.get_payload()[1].add_related(data, maintype="image", subtype="png", cid=f"<{cid}>")
    for name, data, mime in attachments:
        mt, st = mime.split("/")
        msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
    return bytes(msg).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
