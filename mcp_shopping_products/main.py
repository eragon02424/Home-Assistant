"""Einstiegspunkt des Einkaufsscanners (ab 3.14.0).

Laedt server.py unveraendert, haengt die Rezepte-Routen aus recipes_ui.py vor die
MCP-Routen und startet danach dieselben Worker und denselben Server wie der
__main__-Block von server.py (bei neuen Workern dort hier mitpflegen).

Ab 3.15.0: onedrive_client.install() biegt den OneDrive-Weg der Rezept-Jobs (bildname)
auf den Endpunkt 'scanner' des OneDrive SyncServers um (Token-API statt /api/photo).
Der alte Zweig in server.py bleibt als ungenutzter Code stehen.

Ab 3.16.0: grocy_tags.install() registriert die Pflege-Tag-Werkzeuge fuer Rezepte und
Produkte (Zusatzfelder rezept_tags / produkt_tags, Listen mit Pflicht- und Ausschluss-Tags,
get_recipe, update_recipe, set_tags, list_interventions, refresh_tags, get_tag_overview).
"""

import os

import uvicorn

import grocy_tags
import onedrive_client
import server
from recipes_ui import routes as recipe_routes

server.app.router.routes[0:0] = recipe_routes
onedrive_client.install(server, os.environ.get("ONEDRIVE_API_URL", ""), os.environ.get("ONEDRIVE_TOKEN", ""))
grocy_tags.install(server)

if __name__ == "__main__":
    server.start_image_worker()
    server.start_off_lookup_worker()
    server.start_auto_queue_worker()
    server.start_shopping_purge_worker()
    uvicorn.run(server.app, host="0.0.0.0", port=8770)
