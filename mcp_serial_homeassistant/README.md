# MCP Serial HomeAssistant

Persistenter serieller Port-Listener als MCP Server, optimiert für ESP32-S2 (USB-CDC).

## Funktionsweise

Der ESP32-S2 verwendet USB-CDC direkt im Chip — beim Deep Sleep verschwindet `/dev/ttyACM*` vom Host. Dieses Add-on verwendet `pyudev` um den Moment der USB-Enumeration zu erkennen und öffnet den Port sofort (<50ms). Kein Byte geht verloren.

## MCP Tools

| Tool | Beschreibung |
|---|---|
| `serial_read_recent` | Letzte N Zeilen aus RAM-Buffer (default 50) |
| `serial_read_timerange` | Log-Einträge aus Zeitraum (ISO timestamps) |
| `serial_list_ports` | Zeigt verfügbare ttyACM*/ttyUSB* Ports |
| `serial_set_port` | Aktiven Port wechseln (live, kein Neustart) |
| `serial_set_baudrate` | Baudrate ändern (live, kein Neustart) |
| `serial_status` | Aktueller Status, Port, Baudrate, Buffer-Füllstand |

## Konfiguration (options)

```yaml
baud_rate: 115200          # Default Baudrate
ring_buffer_lines: 300     # RAM-Buffer Kapazität
log_retention_hours: 24    # Disk-Log Aufbewahrung
log_max_size_mb: 20        # Max Log-Dateigröße vor Rotation
```

## MCP Proxy Eintrag

```json
{
  "slot_X": {
    "command": "/usr/bin/mcp_serial_server.py",
    "name": "Serial"
  }
}
```

## ESP32-S2 Hinweise

- Kein externer UART-IC — USB-CDC verschwindet beim Schlafen, das ist normal
- `DTR=False` verhindert ungewollten Reset beim Port-Öffnen
- Nach Aufwachen ~50-200ms bis USB-CDC bereit — der udev Watcher öffnet in dieser Zeit
- Immer denselben physischen USB-Port verwenden (Location-basierte Erkennung möglich)
