# MyCupra ReadOnly

Read-only Datenintegration für Cupra/SEAT-Fahrzeuge in Home Assistant, basierend
auf dem offiziellen EU Data Act Portal (eu-data-act.drivesomethinggreater.com).

## Hintergrund

VW Group hat seit ca. Ende Mai 2026 den Zugriff von Drittanbieter-Integrationen
(z.B. über die klassische "We Connect"/Cariad-API) für Cupra/SEAT-Fahrzeuge mit
Play-Integrity-Attestation abgesichert. Die einzige verbleibende, offiziell
unterstützte read-only Datenquelle ist das EU Data Act Portal, das per Gesetz
(EU Data Act) Fahrzeugdaten in regelmäßigen Abständen als ZIP/JSON bereitstellt.

Dieses Projekt bildet den Browser-Login-Flow nach und automatisiert das
Herunterladen und Auswerten der Daten für die Nutzung in Home Assistant.

**Wichtig: Dies ist bewusst read-only.** Climate-Steuerung, Ver-/Entriegeln
oder Ladestart sind über das EU Data Act Portal grundsätzlich nicht möglich
(weder für Cupra/SEAT noch für VW/Audi/Skoda) - das ist eine Einschränkung
auf Seiten von VW Group, keine technische Lücke in diesem Projekt.

## Status

- [x] Login-Flow manuell verifiziert (17.06.2026)
- [x] Download-Flow manuell verifiziert (17.06.2026)
- [x] `scripts/cupra_client.py` - eigenständiges Test-Skript für Login + Download
- [ ] JSON-Auswertung der Datenpakete (SOC, Ladezustand, Kilometerstand, ...)
- [ ] Home Assistant Custom-Integration mit Config-Flow

## Verwendung des Test-Skripts

```bash
pip install requests --break-system-packages

python3 scripts/cupra_client.py \
  --email DEINE@EMAIL.de \
  --password DEINPASSWORT \
  --vin DEINEFAHRZEUGNUMMER \
  --output /tmp \
  --debug
```

Mit `--list-only` wird nur die Liste der verfügbaren Dateien angezeigt, ohne
einen Download durchzuführen.

## Architektur (geplant)

1. **Login**: Nachbau des OAuth/OIDC-Flows von identity.vwgroup.io, inklusive
   CSRF/HMAC-Handling (siehe Code-Kommentare in `cupra_client.py` für die
   genauen Unterschiede zwischen HTML-Formularen und dem `window._IDK`
   JS-Objekt auf der Passwort-Seite).
2. **Download**: Liste der verfügbaren Dateien abrufen (`/list` Endpoint mit
   Header `type: partial`), neueste Datei anhand `createdOn` bestimmen,
   per `/download` Endpoint mit Headern `type: partial` und `filename: ...`
   laden.
3. **Auswertung**: ZIP entpacken, JSON parsen (flaches Array aus
   `{key, dataFieldName, value}` Objekten - kein Verlauf, sondern ein
   Event-Log mit Duplikaten; jeweils neuesten Wert pro `dataFieldName`
   verwenden).
4. **HA-Integration**: Custom Component mit Config-Flow (E-Mail, Passwort,
   VIN als Eingabefelder, Validierung des Logins direkt beim Einrichten),
   DataUpdateCoordinator mit konfigurierbarem Intervall (Standard 15 Min).

## Bekannte technische Details

- Authorization Code ist nur 300 Sekunden gültig - der Austausch gegen den
  Access Token muss also zeitnah erfolgen.
- Access Token ist 60 Minuten gültig (`Max-Age=3600`).
- Es gibt zwei relevante Cookies nach erfolgreichem Login: `access_token`
  und `ath` (beide `HttpOnly`, beide mit gleicher Gültigkeit).
- Das `d_*` Cookie von identity.vwgroup.io hat 1 Jahr Gültigkeit
  (möglicherweise relevant für "Gerät merken" - noch nicht getestet, ob
  es einen erneuten Passwort-Schritt überflüssig macht).
