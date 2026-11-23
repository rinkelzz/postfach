# Änderungen

Alle nennenswerten Änderungen an diesem Projekt. Versionen nach [Semantic Versioning](https://semver.org/lang/de/).

## [0.1.1] – 2026-11-23

### Behoben
- **App startete nicht, wenn Port 8790 schon belegt war** (z. B. vom Foto-Globus). Neuer Standard-Port ist **8810**.
- Ist der eingestellte Port beim Installieren oder Starten belegt, nimmt Postfach automatisch den nächsten freien
  Port (ab 8810), trägt ihn in `config.env` ein und schreibt einen Hinweis ins Protokoll. Die aktuelle Adresse der
  Paketquelle steht wie immer unter Verwaltung → System → Updates.

## [0.1.0] – 2026-11-23

Erste Version: **Lesen und Senden**.

### Mailkonten
- Beliebig viele IMAP-Konten pro Benutzer, SSL/TLS oder STARTTLS, eigener Postausgang (SMTP) pro Konto.
- Server-Vorschläge für bekannte Anbieter (Gmail, GMX, WEB.DE, Telekom, IONOS, STRATO, Posteo, mailbox.org, iCloud,
  Yahoo, freenet) mit Hinweisen zu App-Passwörtern; Verbindungstest vor dem Speichern.
- Passwörter verschlüsselt gespeichert (Schlüssel in eigener Datei, nicht in der Datenbank), nie an den Browser
  ausgeliefert, nicht in Sicherungen.
- Admins richten unter **Verwaltung → Mailkonten** Konten für ihre Benutzer ein, können deren Mails aber nicht lesen.
  Abschaltbar, dass Benutzer selbst Konten hinzufügen.

### Post
- Abruf aller Konten im Hintergrund (Standard alle 5 Minuten) oder per Knopf; Zwischenspeicher der letzten 90 Tage
  (pro Konto einstellbar), der Server bleibt führend.
- Gemeinsamer Posteingang aller Konten, alle Ordner (auch mit Umlauten), Rollen wie Gesendet und Papierkorb erkannt.
- Suche über alle Konten (Betreff, Absender, Empfänger, Text), Filter „Ungelesen“ und „Markiert“.
- Lesen von Text- und HTML-Mails: HTML bereinigt und abgeschottet angezeigt, externe Bilder blockiert bis „Bilder laden“,
  eingebettete Bilder direkt. Anhänge zum Herunterladen.
- Gelesen/ungelesen und Stern, auch am Server.
- Neue Mail, Antworten, Allen antworten, Weiterleiten (mit Original-Anhängen), Anhänge bis 25 MB, Kopie/Blindkopie,
  Signatur pro Konto; Ablage in „Gesendet“, Original wird als beantwortet markiert.
- Ungelesene im Reiter und im Fenstertitel, Aktualisierung jede Minute.

### Grundlage (wie bei den anderen Synology-Apps)
- SPK „noarch“ für Intel/AMD und ARM, Python 3.8 bis 3.13, keine nativen Bibliotheken.
- DSM-Fenster mit Single Sign-on, Anmeldung mit DSM-Konto (inkl. 2FA) oder eigenem Konto, Login-Sperre, CSRF-Schutz.
- Benutzerverwaltung, Firmenname und Logo, vier Designs, Handy und Desktop.
- Nächtliche Datensicherung in einen freigegebenen Ordner, Wiederherstellen per Klick, alte Sicherungen löschbar.
- Updates übers Paket-Zentrum (die App ist ihre eigene Paketquelle), Änderungsprotokoll mit CSV-Export, Port änderbar.

[0.1.1]: https://github.com/rinkelzz/postfach/releases/tag/v0.1.1
[0.1.0]: https://github.com/rinkelzz/postfach/releases/tag/v0.1.0
