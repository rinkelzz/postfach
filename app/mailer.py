"""Kleine Hilfen rund um Mailadressen."""
import re

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def valid_email(addr: str) -> bool:
    return bool(addr) and len(addr) <= 200 and bool(EMAIL_RE.match(addr))
