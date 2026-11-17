#!/usr/bin/env python3
"""Starter: laedt server.py unveraendert, haengt das Bildabruf-Modul ein und startet den Server.

server.py bleibt wie es ist (Ingress-Oberflaeche und Sync-Steuerung). Scheitert
das Bildabruf-Modul, laeuft der Server trotzdem ohne Bildabruf weiter.
"""
import os
import sys
import traceback

sys.path.insert(0, "/app")
import server  # liest die Portnummer aus sys.argv[1]

try:
    import bildabruf

    bildabruf.setup(server.app, server.get_access_token, server.graph_get, server.GRAPH_BASE)
except Exception:
    print("[Bildabruf] FEHLER beim Laden - die Sync-Oberflaeche laeuft ohne Bildabruf weiter:", flush=True)
    traceback.print_exc()

os.makedirs(server.CONFIG_DIR, exist_ok=True)
os.makedirs(server.ONEDRIVE_CONFIG_DIR, exist_ok=True)
server.app.run(host="0.0.0.0", port=server.PORT, debug=False)
