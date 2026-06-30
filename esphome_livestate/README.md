# ESPHome LiveState

HA Custom Component that creates **Online/Offline binary_sensor** entities for all ESPHome devices, attached directly to the existing ESP device in HA — just like PowerCalc attaches power sensors.

## Voraussetzung

Das **MCP ESPHome Addon** muss installiert und gestartet sein. Ohne das Addon zeigt die Integration einen Konfigurationsfehler.

## Installation

1. Diesen Ordner nach `/config/custom_components/esphome_livestate/` kopieren
2. HA neu starten
3. `Einstellungen → Geräte & Dienste → Integration hinzufügen → ESPHome LiveState`
4. URL eintragen: `http://localhost:8090`, Bearer Token: `MCP_ESPHome_2026_qR8tY3wN5vK`

## Was passiert

- Alle vom MCP ESPHome Addon erkannten Geräte bekommen automatisch eine `binary_sensor.<name>_online` Entity
- Die Entity wird dem vorhandenen ESP-Gerät in HA zugeordnet (via MAC-Adresse)
- Neue Geräte werden automatisch erkannt (kein Neustart nötig)
- Wenn das Addon stoppt → Integration zeigt Fehler
