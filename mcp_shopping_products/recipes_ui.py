"""Rezepte-Reiter des Einkaufsscanners (v3.14.0).

Stellt zwei Endpunkte fuer die Ingress-Oberflaeche bereit:
- GET /api/recipes            Rezepte (id, name, Bild, Rezeptart) + Rezeptarten mit Anzahl
- GET /api/recipe-picture/{f} Rezeptbild (von Grocy verkleinert, ?w=Breite in Pixel)

Die Routen werden in main.py vor die MCP-Routen von server.py eingehaengt.
"""

import base64

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from server import GROCY_BASE, grocy_get

NO_CATEGORY = "Ohne Rezeptart"


async def _category_order(client) -> list:
    """Erlaubte Werte des Userfields 'rezeptart' in der in Grocy gepflegten Reihenfolge."""
    r = await grocy_get(client, "/objects/userfields", params={"query[]": "name=rezeptart"})
    if r.status_code != 200:
        return []
    field = next((f for f in r.json() if f.get("entity") == "recipes"), None)
    if not field:
        return []
    return [line.strip() for line in (field.get("config") or "").split("\n") if line.strip()]


async def api_recipes(request: Request):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await grocy_get(client, "/objects/recipes", params={"query[]": "type=normal"})
        if r.status_code != 200:
            return JSONResponse({"recipes": [], "categories": [],
                                 "error": f"Grocy returned {r.status_code}"})
        order = await _category_order(client)

    recipes = []
    counts: dict = {}
    for rec in r.json():
        cat = ((rec.get("userfields") or {}).get("rezeptart") or "").strip() or NO_CATEGORY
        counts[cat] = counts.get(cat, 0) + 1
        recipes.append({
            "id": rec["id"],
            "name": rec.get("name") or "",
            "picture_file_name": rec.get("picture_file_name"),
            "category": cat,
        })
    recipes.sort(key=lambda x: x["name"].lower())

    # Reihenfolge wie in Grocy, danach unbekannte Werte, zuletzt 'Ohne Rezeptart'
    names = [c for c in order if c in counts]
    names += sorted(c for c in counts if c not in order and c != NO_CATEGORY)
    if NO_CATEGORY in counts:
        names.append(NO_CATEGORY)
    categories = [{"name": c, "count": counts[c]} for c in names]
    return JSONResponse({"recipes": recipes, "categories": categories})


async def api_recipe_picture(request: Request):
    filename = request.path_params["filename"]
    try:
        width = int(request.query_params.get("w", "500"))
    except ValueError:
        width = 500
    width = max(100, min(width, 1600))
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await grocy_get(client, f"/files/recipepictures/{fname_b64}",
                            params={"force_serve_as": "picture", "best_fit_width": width})
        if r.status_code != 200:
            return Response(status_code=404)
        return Response(content=r.content,
                        media_type=r.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "private, max-age=600"})


routes = [
    Route("/api/recipes", api_recipes, methods=["GET"]),
    Route("/api/recipe-picture/{filename}", api_recipe_picture, methods=["GET"]),
]
