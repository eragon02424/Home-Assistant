"""Einstiegspunkt des Einkaufsscanners (ab 3.14.0).

Laedt server.py unveraendert, haengt die Rezepte-Routen aus recipes_ui.py vor die
MCP-Routen und startet danach dieselben Worker und denselben Server wie der
__main__-Block von server.py (bei neuen Workern dort hier mitpflegen).
"""

import uvicorn

import server
from recipes_ui import routes as recipe_routes

server.app.router.routes[0:0] = recipe_routes

if __name__ == "__main__":
    server.start_image_worker()
    server.start_off_lookup_worker()
    server.start_auto_queue_worker()
    server.start_shopping_purge_worker()
    uvicorn.run(server.app, host="0.0.0.0", port=8770)
