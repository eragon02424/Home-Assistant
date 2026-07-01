# Changelog

## 1.0.0
- Initial release
- Persistent USB-CDC listener for ESP32-S2 (pyudev auto-reconnect)
- Auto-detect first available ttyACM* device
- Runtime baudrate change via MCP tool (no restart)
- RAM ring buffer (300 lines default)
- Rotating disk log under /data/ (24h retention)
- MCP tools: serial_read_recent, serial_read_timerange, serial_list_ports, serial_set_port, serial_set_baudrate, serial_status
- stdio transport via MCP Proxy
