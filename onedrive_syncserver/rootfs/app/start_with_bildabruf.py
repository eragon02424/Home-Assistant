#!/usr/bin/env python3
"""Starter: laedt server.py unveraendert, haengt das Bildabruf-Modul ein und startet den Server.

server.py bleibt wie es ist (Ingress-Oberflaeche und Sync-Steuerung). Scheitert
das Bildabruf-Modul, laeuft der Server trotzdem ohne Bildabruf weiter.

Ab 1.46.0: Die alten Endpunkte ohne Anmeldung (/api/search, /api/download_by_id,
/api/download_by_path, /api/photo) sind abgeschaltet und antworten mit 410. Ersatz ist die
Token-API des Bildabrufs (Port 8776). Der Code dieser Routen steht noch in server.py,
wird aber nie erreicht.
"""
import os
import sys
import traceback

sys.path.insert(0, "/app")
import server  # liest die Portnummer aus sys.argv[1]
from flask import jsonify, request

_CLOSED_PATHS = ("/api/search", "/api/download_by_id", "/api/download_by_path", "/api/photo")


@server.app.before_request
def _block_closed_endpoints():
    if request.path in _CLOSED_PATHS:
        return jsonify({
            "success": False,
            "error": "Dieser Endpunkt ist abgeschaltet (er hatte keine Anmeldung). Neue Token-API: Port 8776, /v1/...",
        }), 410
    return None


try:
    import bildabruf

    bildabruf.setup(server.app, server.get_access_token, server.graph_get, server.GRAPH_BASE)
except Exception:
    print("[Bildabruf] FEHLER beim Laden - die Sync-Oberflaeche laeuft ohne Bildabruf weiter:", flush=True)
    traceback.print_exc()

os.makedirs(server.CONFIG_DIR, exist_ok=True)
os.makedirs(server.ONEDRIVE_CONFIG_DIR, exist_ok=True)
server.app.run(host="0.0.0.0", port=server.PORT, debug=False)
