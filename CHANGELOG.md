# Änderungen

Alle nennenswerten Änderungen an diesem Projekt. Versionen nach [Semantic Versioning](https://semver.org/lang/de/).

## [0.3.1] – 2026-11-23

### Behoben
- **Paket-Zentrum zeigte ein Update manchmal erst verspätet an:** Fragte das Paket-Zentrum direkt nach dem Start der App,
  kannte Postfach die neueste Version noch nicht und antwortete mit einer leeren Liste – die DSM sich eine Weile merkt.
  Jetzt fragt die Paketquelle in dem Fall sofort bei GitHub nach; die SPK wird im Hintergrund auf die NAS geladen.

## [0.3.0] – 2026-11-23

### Neu: Regeln
- Regeln für **neue Mails im Posteingang** (unter **Konten → Regeln**). Sie laufen auf der NAS – auch wenn Postfach
  gerade nicht offen ist – und dank IDLE meist Sekunden nach dem Eingang.
- Bedingungen: Absender, Empfänger (An/Kopie), Betreff, Text, hat Anhang – mit „enthält“, „enthält nicht“, „ist gleich“,
  „endet auf“ (z. B. `@kunde.de`); alle oder eine der Bedingungen. Für ein bestimmtes Konto oder alle eigenen Konten.
- Aktionen: **Verschieben** in einen Ordner, **Weiterleiten** an eine Adresse (mit Anhängen, Antworten gehen an den
  ursprünglichen Absender), **in den Papierkorb**. Reihenfolge per ↑ ↓, „danach keine weiteren Regeln“.
- **Jetzt anwenden** auf Mails, die schon im Posteingang liegen – mit Vorschau, wie viele passen. Weitergeleitet wird
  dabei nie, nur verschoben bzw. gelöscht.
- **⚙ Regel** beim Lesen einer Mail legt eine Regel für diesen Absender an.
- Beim ersten Abruf eines Kontos werden keine Regeln angewendet (sonst würde z. B. eine Weiterleitung 90 Tage Post verschicken).
- Schutz vor Mail-Schleifen: automatische Mails (Abwesenheitsnotizen, Rückläufer, Massenmails) und schon von Postfach
  weitergeleitete Mails werden nie weitergeleitet, nie an die eigene Adresse, höchstens 50 Weiterleitungen pro Konto
  und Stunde. Treffer und Fehler (z. B. gelöschter Zielordner) stehen an der Regel.
- Regeln sieht nur ihr Besitzer – auch Admins nicht (sie verraten viel über die Mails eines Benutzers).

## [0.2.0] – 2026-11-23

### Neu
- **Neue Mails kommen sofort:** Pro Konto beobachtet ein Wächter den Posteingang per IMAP IDLE. Meldet der Server
  eine neue Mail, wird nur der Posteingang abgeglichen – meist in Sekunden statt erst beim nächsten Abruf. Kann ein
  Server kein IDLE, bleibt es beim regelmäßigen Abruf; unter **Konten** steht, welches Konto sofort benachrichtigt.
- **Browser-Benachrichtigung** bei neuer Mail im Posteingang (🔔 über der Liste oder unter **Mein Zugang**), solange
  Postfach in einem Tab oder im DSM-Fenster offen ist. Ein Klick öffnet die Mail. Braucht https (DSM-Fenster oder Reverse Proxy).
- **Löschen:** 🗑 bzw. Taste Entf legt die Mail in den Papierkorb, im Papierkorb wird endgültig gelöscht (mit Rückfrage).
  Danach öffnet sich die nächste Mail.
- **Verschieben** in einen anderen Ordner desselben Kontos. Funktioniert auch bei Servern ohne IMAP-MOVE.
- **Entwürfe:** Beim Schreiben wird alle 30 Sekunden (und beim Schließen) automatisch im Entwürfe-Ordner des Kontos
  gespeichert – auch in Outlook oder am Handy sichtbar. Entwürfe lassen sich mit „Weiter bearbeiten“ fortsetzen
  (mit Bcc und Anhängen), „Verwerfen“ löscht sie, nach dem Senden verschwindet der Entwurf von selbst.

### Verbessert
- Kommt eine neue Mail, während gerade abgerufen wird, wird direkt danach noch einmal abgeglichen – nichts geht verloren.

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

[0.3.1]: https://github.com/rinkelzz/postfach/releases/tag/v0.3.1
[0.3.0]: https://github.com/rinkelzz/postfach/releases/tag/v0.3.0
[0.2.0]: https://github.com/rinkelzz/postfach/releases/tag/v0.2.0
[0.1.1]: https://github.com/rinkelzz/postfach/releases/tag/v0.1.1
[0.1.0]: https://github.com/rinkelzz/postfach/releases/tag/v0.1.0
