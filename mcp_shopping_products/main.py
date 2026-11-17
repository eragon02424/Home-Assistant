"""Einstiegspunkt des Einkaufsscanners.

server.py ist der Kern (Grocy-Werkzeuge, Worker, Ingress-Oberflaeche). Hier werden die
Erweiterungen angehaengt und alles gestartet:
  * recipes_ui:  Rezepte-Ansicht der Ingress-Oberflaeche (Routen vor den MCP-Routen)
  * grocy_tags:  Pflege-Tags, Produktpflege-Werkzeuge und Auto-Queue fuer den Bild-Lookup
"""

import uvicorn

import grocy_tags
import server
from recipes_ui import routes as recipe_routes

server.app.router.routes[0:0] = recipe_routes
start_auto_queue_worker = grocy_tags.install(server)

if __name__ == "__main__":
    server.start_image_worker()
    server.start_off_lookup_worker()
    start_auto_queue_worker()
    server.start_shopping_purge_worker()
    uvicorn.run(server.app, host="0.0.0.0", port=8770)
