# MCP ESPHome

Vermittler-Service zwischen Claude und ESPHome-Geräten.

## Status: In Entwicklung (Stufe 1)

## Architektur
- Nutzt `aioesphomeapi` (offizielle Lib, gleiche wie Home Assistant selbst nutzt)
- Dauerhafte Hintergrundverbindung zu allen erkannten ESPHome-Geräten
- Claude fragt nur diesen Service ab, nie direkt das ESP-Gerät

## Funktionen (Stufe 1)
- YAML lesen/schreiben (über bestehenden HA MCP Filesystem-Zugriff)
- `list_devices()` — automatische Geräte-Discovery, regelmäßiger Online/Offline-Check
- `start_compile(device_name)` / `get_status(job_id)` / `get_error_summary(job_id)` / `get_full_log(job_id)`
- `start_install(device_name)` — OTA Flash
- `get_device_logs(device_name)` — 24h Ringpuffer aller Runtime-Logs
- `get_last_seen(device_name)` / `get_uptime_pattern(device_name)` — 30 Tage Heartbeat-Historie

## Funktionen (Stufe 2 — später)
- Log-Filterung nach Komponente/Level
- Anomalie-Erkennung (Batterie-Ausfall-Früherkennung)
- Direkte Sensor-Werte, Config-Validierung, YAML-Diff, Geräte-Neustart, Auto-Backup

## Anbindung
Erreichbar über MCP Proxy (bestehender Webhook-Proxy-Mechanismus) für Claude Mobile/Web.
