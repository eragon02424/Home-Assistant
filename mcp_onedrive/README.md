# MCP OneDrive

HA-Add-on (MCP-Server), die Tuer fuer Claude zum OneDrive-Bildabruf. Er reicht
Suche, Auftraege und Status an die Token-API des Add-ons **OneDrive SyncServer**
(Version 1.45.0 oder neuer, Port 8776) durch und **wartet nie**. Token-Pruefung,
Freigabe durch den Besitzer, Download und Download-Link liegen im SyncServer.

## Ablauf

1. `search_file(filename)` - Dateiname -> voller Pfad (direkt bei OneDrive, ohne Suchindex).
2. `request_download(path)` - legt einen Auftrag an; mit Bestaetigung wartet er auf die
   Freigabe auf der Seite "Bildabruf" des SyncServers (Ingress, mit Vorschaubild).
3. `get_job(job_id)` - Status; jede Antwort enthaelt `wait_seconds` (so lange warten,
   dann erneut fragen). Bei `ready` steht `download_url` und `sha256` in der Antwort.
4. Die Datei holt ein Programm mit Shell (z. B. Claude Code) direkt per HTTP (`curl`);
   sie laeuft nie durch diesen Server oder den Chat.

## Add-on-Optionen

| Option | Bedeutung |
|---|---|
| `syncserver_api_url` | Adresse der Token-API, Standard `http://192.168.178.76:8776` |
| `endpoint_token` | Token des Endpunkts `bilder` aus dem Log des SyncServers (Zeile "Endpunkt 'bilder' ... Token") |

Der MCP-Token dieses Add-ons wird beim ersten Start erzeugt und bei jedem Start im Log
ausgegeben. Der Endpunkt-Token wird nie an Claude zurueckgegeben.

## Werkzeuge

`get_endpoint_info`, `search_file`, `request_download`, `get_job`.
Schreiben und Loeschen gibt es bewusst nicht.

## Versionshistorie

- 0.1.0 - Erste Version.
