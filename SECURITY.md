# Sicherheit

Bitte Sicherheitslücken **nicht** als öffentliches Issue melden, sondern über
[GitHub → Security → Report a vulnerability](https://github.com/rinkelzz/postfach/security/advisories/new).
Ich melde mich so schnell wie möglich.

Unterstützt wird jeweils die neueste Version unter [Releases](https://github.com/rinkelzz/postfach/releases).

## Was die App selbst absichert

- **Passwörter** werden mit PBKDF2-SHA256 (200 000 Runden, eigenes Salz) gespeichert, Sitzungen nur als
  SHA-256-Hash. Beim Passwortwechsel und beim Deaktivieren eines Benutzers enden alle seine Sitzungen.
- **Login-Sperre:** 5 Fehlversuche pro Benutzername und Adresse, 10 pro Benutzername insgesamt – jeweils
  15 Minuten. Die Tabelle ist in der Größe begrenzt, sie lässt sich nicht volllaufen lassen.
- **CSRF:** Änderungen per Login-Cookie werden nur von der eigenen Seite angenommen (`Sec-Fetch-Site`
  bzw. `Origin`/`Referer`). Läuft die App hinter einem Proxy, der den `Host`-Header umschreibt, die
  **Öffentliche Adresse** in den Einstellungen eintragen.
- **DSM-Passwortprüfung** nur gegen die NAS selbst (`127.0.0.1`, `localhost`, `::1` – der Hostname wird
  exakt geprüft), ohne Proxy-Umgebungsvariablen und ohne Weiterleitungen. DSM-Passwörter verlassen die NAS nie.
- **Single Sign-on im DSM-Fenster:** Der Schlüssel (`.proxy_secret` im Paketordner) wird nur von
  Verbindungen über 127.0.0.1 ohne `X-Forwarded-For` angenommen, also nur von `app.cgi` auf der NAS selbst.
  Die Datei ist nur für den Paketbenutzer lesbar (Modus 0600), unter dem auch `app.cgi` läuft.
- **Echte Absenderadresse** (`X-Forwarded-For`) wird nur übernommen, wenn die Anfrage vom Reverse Proxy
  auf derselben NAS (127.0.0.1) kommt. Bei einem Proxy auf einem anderen Gerät sieht die App dessen Adresse –
  die Login-Sperre greift dann nur noch pro Benutzername.
- **Keine Zugangsdaten in Sicherungen:** Die Passwörter der Mailkonten und der Mail-Zwischenspeicher werden vor dem
  Sichern entfernt (inklusive `VACUUM`, damit nichts in freien Seiten der Datei steht). Browser-Sitzungen auch nicht.
- **CSV-Export** (Änderungsprotokoll) entschärft Zellen, die mit `=`, `+`, `-`, `@`, Tab oder CR beginnen (Formel-Injection).
- **Paketquelle** (`/spk`) nur aus dem lokalen Netz erreichbar.

## Mails

- **Konto-Passwörter** werden verschlüsselt gespeichert: Stromverschlüsselung mit HMAC-SHA256 im Zählermodus,
  danach Encrypt-then-MAC mit HMAC-SHA256 (getrennte Teilschlüssel, zufällige 128-Bit-Nonce) – nur Standardbibliothek,
  ohne native Bibliotheken. Der 256-Bit-Schlüssel liegt in `secret.key` im Datenordner (Modus 0600), **nicht** in der
  Datenbank. Wer nur die Datenbank oder eine Sicherung hat, kann die Passwörter nicht lesen. Eine beschädigte
  Schlüsseldatei wird nie stillschweigend ersetzt. Passwörter werden nie an den Browser ausgeliefert.
- **Trennung der Benutzer:** Ordner, Nachrichten, Anhänge, Antwortentwürfe und Versand prüfen bei jeder Anfrage, dass
  das Konto der angemeldeten Person gehört – auch Admins sehen keine fremden Mails (sie dürfen Konten nur einrichten,
  ändern und löschen). Fremde Nachrichten antworten mit 404, nicht 403, damit nichts über ihre Existenz verrät.
- **HTML-Mails:** Ein Filter (Allowlist) lässt nur harmlose Tags und Attribute durch – kein `script`, `iframe`,
  `object`, `svg`, keine Formulare, keine `on…`-Attribute, Links nur `http(s):`, `mailto:`, `tel:`. CSS wird von
  `expression()`, `@import` und `javascript:` befreit, ein Ausbruch aus `<style>` ist nicht möglich. Angezeigt wird in
  einem `iframe` mit `sandbox` **ohne** `allow-scripts` und **ohne** `allow-same-origin` und eigener
  Content-Security-Policy (`default-src 'none'`). Selbst wenn der Filter etwas übersähe, könnte es weder Code ausführen
  noch auf Postfach zugreifen.
- **Externe Inhalte** (Bilder, Hintergründe, `url(…)` in CSS) werden entfernt und per CSP verboten, bis der Benutzer
  „Bilder laden“ klickt (Schutz vor Tracking-Pixeln). Die Seite schickt keinen Referrer mit.
- **Anhänge** werden immer als `application/octet-stream` mit `Content-Disposition: attachment` und
  `X-Content-Type-Options: nosniff` ausgeliefert – ein HTML-Anhang kann nicht auf der Adresse von Postfach laufen.
- **Versand:** Zeilenumbrüche in Betreff und Kopfzeilen werden entfernt (keine Header-Injection), Empfänger werden
  geprüft (höchstens 100), Anhänge zusammen höchstens 25 MB. Bcc steht nicht in der verschickten Mail.
- **Verbindungen zu Mailservern** nur mit SSL/TLS oder STARTTLS, Zertifikat und Servername werden geprüft. Nur beim
  Postausgang gibt es „keine Verschlüsselung“ für Relays im eigenen Netz.
- **Microsoft 365 (OAuth2):** Anmeldung per Gerätecode direkt bei Microsoft – Postfach sieht das Microsoft-Passwort
  nie. Erneuerungs- und Zugriffstoken werden wie Passwörter verschlüsselt gespeichert, nie an den Browser gegeben und
  nicht gesichert. Laufende Anmeldungen gehören dem Benutzer, der sie gestartet hat (zufällige Kennung, nach spätestens
  einer Stunde verworfen). Beim „Neu verbinden“ muss es dasselbe Microsoft-Konto sein. Die App-Registrierung ist ein
  öffentlicher Client ohne Geheimnis; Berechtigungen nur `IMAP.AccessAsUser.All`, `SMTP.Send` (delegiert),
  `offline_access`, `openid`, `email`. Widerrufen lässt sich der Zugriff jederzeit in Entra ID bzw. unter
  myaccount.microsoft.com.
- **Regeln** gehören einem Benutzer und gelten nur für seine Konten; niemand sonst (auch kein Admin) sieht sie.
  Weiterleiten nur an gültige fremde Adressen (nicht an die eigene), weitergeleitete Mails tragen `Auto-Submitted:
  auto-forwarded` und eine eigene Kennung; automatische Mails, Rückläufer und schon weitergeleitete Mails werden nie
  weitergeleitet, höchstens 50 Weiterleitungen pro Konto und Stunde. „Jetzt anwenden“ leitet nie weiter.
- **Server-Adressen** sind frei wählbar (eigene Mailserver, eigene Ports). Damit kann jeder, der Konten anlegen darf,
  die NAS Verbindungen zu beliebigen Servern aufbauen lassen. Wer das nicht möchte, schaltet unter
  **Verwaltung → Einstellungen** ab, dass Benutzer selbst Konten hinzufügen – dann gilt das auch für den Verbindungstest.

## Hinweise für den Betrieb

- Die App nur per **HTTPS** (Reverse Proxy von DSM mit Zertifikat) nach außen freigeben, nie den Port
  (Standard 8810) direkt. Bei Zugriff per `http://` aus dem Internet zeigt die App eine Warnung.
- Updates kommen aus den GitHub-Releases dieses Repos. Sie sind nicht zusätzlich signiert – geschützt
  sind sie durch HTTPS und das (per Zwei-Faktor gesicherte) GitHub-Konto des Maintainers.
- Der Schlüssel `secret.key` und die Datenbank liegen beide in `/var/packages/postfach/var/`, lesbar nur für den
  Paketbenutzer. Die Verschlüsselung schützt also Sicherungen und eine kopierte Datenbank, nicht aber vor jemandem,
  der als root auf der NAS arbeitet. Wo der Anbieter es erlaubt, ein **App-Passwort** statt des Hauptpassworts
  verwenden – es lässt sich jederzeit einzeln widerrufen.
- Den freigegebenen Ordner für Sicherungen nicht für alle Benutzer freigeben – die Sicherungen enthalten
  Benutzer, Passwort-Hashes und Kontoeinstellungen (ohne Konto-Passwörter).
- Der Zwischenspeicher (letzte 90 Tage, pro Konto einstellbar) liegt unverschlüsselt in der Datenbank auf der NAS.
  Für besonders schützenswerte Postfächer (z. B. Patientendaten) die NAS-Volumes verschlüsseln und den Zeitraum klein halten.

# Security (English)

Please report vulnerabilities privately via GitHub's *Report a vulnerability* button, not as a public issue.
