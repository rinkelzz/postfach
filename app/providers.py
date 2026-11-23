"""Server-Vorschläge für bekannte Mailanbieter (nach der Domain der Mailadresse).

Für eigene Domains wird imap.<domain> / smtp.<domain> vorgeschlagen – das passt oft, aber nicht immer
(bei Webhostern wie IONOS, Strato oder all-inkl steht der richtige Server im Kundenmenü).
"""
from typing import Dict, Optional

_P = {
    "gmail": {"label": "Gmail", "imap": ("imap.gmail.com", 993, "ssl"), "smtp": ("smtp.gmail.com", 587, "starttls"),
              "hint": "Gmail braucht ein App-Passwort – das normale Passwort funktioniert nicht.",
              "steps": ["Im Google-Konto die Bestätigung in zwei Schritten einschalten (ohne sie gibt es keine App-Passwörter).",
                        "App-Passwörter öffnen, als Namen z. B. „Postfach“ eingeben und „Erstellen“ klicken.",
                        "Das angezeigte 16-stellige Passwort (ohne Leerzeichen) hier als Passwort eintragen – Google zeigt es "
                        "nur einmal an.",
                        "Ändert man später das Google-Passwort, werden App-Passwörter ungültig – dann ein neues anlegen."],
              "links": [["Bestätigung in zwei Schritten", "https://myaccount.google.com/signinoptions/twosv"],
                        ["App-Passwörter", "https://myaccount.google.com/apppasswords"],
                        ["Hilfe von Google", "https://support.google.com/accounts/answer/185833?hl=de"]],
              "note": "Bei Google-Workspace-Konten (Firmen) kann der Administrator App-Passwörter sperren."},
    "gmx": {"label": "GMX", "imap": ("imap.gmx.net", 993, "ssl"), "smtp": ("mail.gmx.net", 587, "starttls"),
            "hint": "Bei GMX muss der Abruf per IMAP einmalig im Webmailer erlaubt werden "
                    "(Einstellungen → POP3/IMAP Abruf)."},
    "webde": {"label": "WEB.DE", "imap": ("imap.web.de", 993, "ssl"), "smtp": ("smtp.web.de", 587, "starttls"),
              "hint": "Bei WEB.DE muss der Abruf per IMAP einmalig im Webmailer erlaubt werden "
                      "(Einstellungen → POP3/IMAP Abruf)."},
    "tonline": {"label": "Telekom (t-online.de)", "imap": ("secureimap.t-online.de", 993, "ssl"),
                "smtp": ("securesmtp.t-online.de", 465, "ssl"),
                "hint": "Die Telekom verlangt ein eigenes E-Mail-Passwort (Telekom Kundencenter → E-Mail-Passwort), "
                        "nicht das Login-Passwort."},
    "ionos": {"label": "IONOS", "imap": ("imap.ionos.de", 993, "ssl"), "smtp": ("smtp.ionos.de", 587, "starttls"), "hint": ""},
    "strato": {"label": "STRATO", "imap": ("imap.strato.de", 993, "ssl"), "smtp": ("smtp.strato.de", 465, "ssl"), "hint": ""},
    "posteo": {"label": "Posteo", "imap": ("posteo.de", 993, "ssl"), "smtp": ("posteo.de", 587, "starttls"), "hint": ""},
    "mailbox": {"label": "mailbox.org", "imap": ("imap.mailbox.org", 993, "ssl"),
                "smtp": ("smtp.mailbox.org", 587, "starttls"), "hint": ""},
    "icloud": {"label": "iCloud", "imap": ("imap.mail.me.com", 993, "ssl"), "smtp": ("smtp.mail.me.com", 587, "starttls"),
               "hint": "iCloud braucht ein app-spezifisches Passwort (appleid.apple.com → Anmeldung und Sicherheit). "
                       "Als Benutzername für IMAP nur den Teil vor dem @ eintragen."},
    "yahoo": {"label": "Yahoo", "imap": ("imap.mail.yahoo.com", 993, "ssl"),
              "smtp": ("smtp.mail.yahoo.com", 465, "ssl"), "hint": "Yahoo braucht ein App-Passwort."},
    "freenet": {"label": "freenet", "imap": ("mx.freenet.de", 993, "ssl"), "smtp": ("mx.freenet.de", 587, "starttls"),
                "hint": ""},
    "microsoft": {"label": "Microsoft (Outlook.com / Hotmail)", "imap": ("outlook.office365.com", 993, "ssl"),
                  "smtp": ("smtp.office365.com", 587, "starttls"),
                  "hint": "Microsoft erlaubt die Anmeldung mit Passwort per IMAP meist nicht mehr – bitte oben "
                          "„Mit Microsoft 365 verbinden“ nutzen (bei privaten Outlook.com-Konten geht das nur, wenn die "
                          "App-Registrierung auch private Microsoft-Konten zulässt)."},
}

DOMAINS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "gmx.de": "gmx", "gmx.net": "gmx", "gmx.at": "gmx", "gmx.ch": "gmx",
    "web.de": "webde",
    "t-online.de": "tonline", "magenta.de": "tonline",
    "ionos.de": "ionos", "online.de": "ionos", "1und1.de": "ionos",
    "strato.de": "strato",
    "posteo.de": "posteo", "posteo.net": "posteo",
    "mailbox.org": "mailbox",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
    "yahoo.com": "yahoo", "yahoo.de": "yahoo",
    "freenet.de": "freenet",
    "outlook.com": "microsoft", "outlook.de": "microsoft", "hotmail.com": "microsoft", "hotmail.de": "microsoft",
    "live.com": "microsoft", "live.de": "microsoft", "msn.com": "microsoft",
}


def suggest(email: str) -> Optional[Dict]:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    key = DOMAINS.get(domain)
    if key:
        p = _P[key]
        user = local if key == "icloud" else email
        return {"known": True, "provider": p["label"], "hint": p["hint"], "steps": p.get("steps", []),
                "links": p.get("links", []), "note": p.get("note", ""),
                "imap_host": p["imap"][0], "imap_port": p["imap"][1], "imap_security": p["imap"][2], "imap_user": user,
                "smtp_host": p["smtp"][0], "smtp_port": p["smtp"][1], "smtp_security": p["smtp"][2], "smtp_user": email}
    return {"known": False, "provider": "", "hint": "Eigene Domain: Bitte die Serverdaten beim Anbieter nachsehen "
                                                    "(steht meist im Kundenmenü unter „E-Mail“).",
            "imap_host": "imap." + domain, "imap_port": 993, "imap_security": "ssl", "imap_user": email,
            "smtp_host": "smtp." + domain, "smtp_port": 587, "smtp_security": "starttls", "smtp_user": email}
