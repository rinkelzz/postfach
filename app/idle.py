"""Neue Mails sofort erkennen: pro Konto ein Wächter, der den Posteingang per IMAP IDLE (RFC 2177) beobachtet.

- Meldet der Server eine Änderung (neue, gelöschte oder geänderte Nachricht), wird nur der Posteingang abgeglichen –
  das dauert meist unter einer Sekunde. Der regelmäßige Abruf aller Ordner läuft unabhängig davon weiter.
- Nach 24 Minuten ohne Meldung wird neu verbunden (Server beenden IDLE spätestens nach 30 Minuten) und
  vorsichtshalber abgeglichen.
- Fehler (Server weg, Passwort falsch …) führen zu einer Pause mit wachsendem Abstand (30 s bis 10 min).
- Kann der Server kein IDLE, beendet sich der Wächter – dann bleibt es beim regelmäßigen Abruf.
- ensure() wird vom Zeitplan jede Minute aufgerufen: startet fehlende Wächter, beendet überflüssige und startet
  neu, wenn sich Server, Benutzer oder Passwort eines Kontos geändert haben.
"""
import hashlib
import threading
from typing import Dict, Optional

from . import imapclient, sync
from .config import settings
from .db import connect

IDLE_SECONDS = 24 * 60
BACKOFF_START, BACKOFF_MAX = 30, 600
MAX_WATCHERS = 50

_watchers: Dict[int, "Watcher"] = {}
_guard = threading.Lock()


def _signature(row) -> str:
    # Bei Microsoft 365 ändert sich das Geheimnis stündlich (neuer Token) – das ist kein Grund für einen Neustart
    keys = ("imap_host", "imap_port", "imap_security", "imap_user", "auth_type")
    if row["auth_type"] != "oauth_ms":
        keys += ("secret",)
    raw = "|".join(str(row[k]) for k in keys)
    return hashlib.sha256(raw.encode()).hexdigest()


class Watcher(threading.Thread):
    def __init__(self, account_id: int, signature: str):
        super().__init__(name=f"postfach-idle-{account_id}", daemon=True)
        self.account_id = account_id
        self.signature = signature
        self.stopped = threading.Event()
        self.client: Optional[imapclient.Client] = None
        self.state = "startet"       # verbunden | wartet | kein IDLE | Fehler: …
        self.unsupported = False

    def stop(self) -> None:
        self.stopped.set()
        c = self.client
        if c is not None:
            c.interrupt()

    def _account(self):
        conn = connect()
        try:
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (self.account_id,)).fetchone()
        finally:
            conn.close()

    def _inbox_path(self) -> str:
        conn = connect()
        try:
            row = conn.execute("SELECT path FROM folders WHERE account_id = ? AND role = 'inbox'",
                               (self.account_id,)).fetchone()
            return row["path"] if row else "INBOX"
        finally:
            conn.close()

    def run(self) -> None:
        backoff = BACKOFF_START
        while not self.stopped.is_set():
            account = self._account()
            if not account or not account["active"] or not account["secret"]:
                return
            try:
                with sync.open_client(account) as client:
                    self.client = client
                    if "IDLE" not in client.capabilities:
                        self.unsupported = True
                        self.state = "kein IDLE"
                        return
                    client.select(self._inbox_path(), readonly=True)
                    self.state = "wartet"
                    backoff = BACKOFF_START
                    while not self.stopped.is_set():
                        result = client.idle_wait(IDLE_SECONDS)
                        if self.stopped.is_set():
                            return
                        sync.start_sync(self.account_id, roles=("inbox",))
                        if result == "timeout":
                            break  # Verbindung ist danach nicht mehr nutzbar → neu verbinden
            except Exception as e:  # noqa: BLE001 – Wächter dürfen nie die App stören
                if self.stopped.is_set():
                    return
                self.state = "Fehler: " + imapclient.friendly_error(e)
                if self.stopped.wait(backoff):
                    return
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                self.client = None


def ensure() -> Dict[int, str]:
    """Wächter passend zu den aktiven Konten starten bzw. beenden. Liefert Konto → Zustand."""
    conn = connect()
    try:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT * FROM accounts WHERE active = 1 AND secret != '' ORDER BY id LIMIT ?", (MAX_WATCHERS,))}
    finally:
        conn.close()
    with _guard:
        for account_id in list(_watchers):
            w = _watchers[account_id]
            row = rows.get(account_id)
            if row is None or _signature(row) != w.signature or (not w.is_alive() and not w.unsupported):
                w.stop()
                del _watchers[account_id]
        for account_id, row in rows.items():
            if account_id not in _watchers:
                w = Watcher(account_id, _signature(row))
                _watchers[account_id] = w
                w.start()
        return {k: w.state for k, w in _watchers.items()}


def refresh(account_id: int) -> None:
    """Nach Änderungen an einem Konto: dessen Wächter neu starten (beim nächsten ensure())."""
    with _guard:
        w = _watchers.pop(account_id, None)
    if w:
        w.stop()


def state(account_id: int) -> Optional[str]:
    w = _watchers.get(account_id)
    return w.state if w else None


def stop_all() -> None:
    with _guard:
        for w in _watchers.values():
            w.stop()
        _watchers.clear()


def kick(account_id: Optional[int] = None) -> None:
    """Nach Anlegen/Ändern/Löschen eines Kontos die Wächter gleich anpassen (nicht erst beim nächsten Zeitplan)."""
    if account_id is not None:
        refresh(account_id)
    if settings.scheduler:
        threading.Thread(target=ensure, name="postfach-idle-ensure", daemon=True).start()
