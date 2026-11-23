"""Verschlüsselung der Konto-Passwörter – nur Standardbibliothek, keine nativen Bibliotheken.

Mailkonten brauchen ihr Passwort im Klartext für IMAP/SMTP, es kann also nicht (wie Login-Passwörter)
gehasht werden. Gespeichert wird es deshalb verschlüsselt:

- Schlüssel: 32 Zufallsbytes in <Datenordner>/secret.key (nur für den Paketbenutzer lesbar, 0600).
  Die Datei liegt NICHT in der Datenbank und damit in keiner Sicherung.
- Verfahren: Stromverschlüsselung mit HMAC-SHA256 als Pseudozufallsfunktion im Zählermodus
  (Schlüsselstrom = HMAC(k_enc, nonce ‖ zähler)), danach Encrypt-then-MAC mit HMAC-SHA256 über
  Version, Nonce und Geheimtext. Getrennte Teilschlüssel für Verschlüsseln und Prüfen (abgeleitet per HMAC).
  Das ist ein anerkanntes Konstrukt (PRF-CTR + HMAC) und kommt ohne AES-Bibliothek aus.
- Format: "v1:" + base64url(nonce[16] ‖ geheimtext ‖ tag[32])

Wer nur die Datenbank (oder eine Sicherung) in die Hände bekommt, kann die Passwörter nicht lesen.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Dict, Optional

from .config import settings

PREFIX = "v1:"
_NONCE = 16
_TAG = 32
_lock = threading.Lock()
_cache: Dict[str, bytes] = {}


class CryptoError(Exception):
    pass


def key_path() -> Path:
    return settings.data_dir / "secret.key"


def _master() -> bytes:
    path = key_path()
    cached = _cache.get(str(path))
    if cached:
        return cached
    with _lock:
        try:
            key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            key = b""
        if len(key) != 32:
            if path.exists():
                # Vorhandene, aber kaputte Datei nie stillschweigend ersetzen – sonst wären alle Passwörter weg
                raise CryptoError(f"Schlüsseldatei {path} ist beschädigt")
            key = secrets.token_bytes(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key.hex())
            os.chmod(str(tmp), 0o600)
            tmp.replace(path)
        _cache[str(path)] = key
        return key


def _subkeys():
    master = _master()
    return (hmac.new(master, b"postfach-enc", hashlib.sha256).digest(),
            hmac.new(master, b"postfach-mac", hashlib.sha256).digest())


def _keystream(k_enc: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(k_enc, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(plain: str) -> str:
    k_enc, k_mac = _subkeys()
    data = plain.encode("utf-8")
    nonce = secrets.token_bytes(_NONCE)
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(k_enc, nonce, len(data))))
    tag = hmac.new(k_mac, PREFIX.encode() + nonce + cipher, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")


def decrypt(token: str) -> str:
    if not token or not token.startswith(PREFIX):
        raise CryptoError("Unbekanntes Format")
    try:
        raw = base64.urlsafe_b64decode(token[len(PREFIX):].encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise CryptoError("Unbekanntes Format")
    if len(raw) < _NONCE + _TAG:
        raise CryptoError("Zu kurz")
    nonce, cipher, tag = raw[:_NONCE], raw[_NONCE:-_TAG], raw[-_TAG:]
    k_enc, k_mac = _subkeys()
    expected = hmac.new(k_mac, PREFIX.encode() + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise CryptoError("Prüfsumme falsch – anderer Schlüssel oder veränderte Daten")
    return bytes(a ^ b for a, b in zip(cipher, _keystream(k_enc, nonce, len(cipher)))).decode("utf-8")


# --- Passwörter eines Kontos (IMAP und SMTP in einem verschlüsselten Feld) ---

def pack_secrets(imap_password: str, smtp_password: Optional[str] = None) -> str:
    data = {"imap": imap_password}
    if smtp_password:
        data["smtp"] = smtp_password
    return encrypt(json.dumps(data))


def unpack_secrets(token: str) -> Dict[str, str]:
    """{"imap": …, "smtp": …} – SMTP fällt auf das IMAP-Passwort zurück. Leeres Feld → leere Werte."""
    if not token:
        return {"imap": "", "smtp": ""}
    data = json.loads(decrypt(token))
    imap = data.get("imap", "")
    return {"imap": imap, "smtp": data.get("smtp") or imap}
