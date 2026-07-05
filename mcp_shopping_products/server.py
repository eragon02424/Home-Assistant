"""
MCP Shopping Products for Home Assistant v1.2.1

Two halves:
1. Ingress web UI (live camera via getUserMedia + zxing-js, same library and
   version Grocy itself uses) - handles the live scan workflow directly
   against Grocy's API, without involving Claude at all.
2. MCP tools - used later by Claude in a separate session to fill in names
   for products that were created with a placeholder name during scanning.

Flow (check-first instead of try-then-fallback):
1. Barcode decoded client-side (zxing-js) from the live video.
2. Frontend calls /api/check-barcode (read-only GET against Grocy).
   - Known: shows name/picture + amount + Einlagern/Auslagern -> /api/book
   - Unknown: goes straight to a camera capture step (live preview with an
     explicit "Foto aufnehmen" button - not automatic, not continuous
     analysis) -> /api/create-unknown creates the product (barcode as
     placeholder name, links barcode, uploads picture) WITHOUT booking any
     stock -> frontend then shows amount + Einlagern/Auslagern -> /api/book

Grocy API facts this code relies on (verified against a live Grocy instance
during development, see conversation history):
- GET /stock/products/by-barcode/{barcode} returns 200 with product/
  picture_file_name/stock_amount for a known barcode, or 400 with
  error_message "No product with barcode ... found" for an unknown one.
- Product.name has a NOT NULL constraint AND a UNIQUE constraint. An empty
  string collides after the first use (Grocy stores/returns "" as null and
  enforces uniqueness on it) - the barcode string itself is used as a unique
  placeholder name instead.
- If a product with name==<barcode> already exists but has no linked
  ProductBarcode entry (e.g. a previous create-unknown run was interrupted
  after the product-create step but before the barcode-link step), a later
  scan of the same barcode used to crash with "UNIQUE constraint failed:
  products.name" when trying to create a second product with that name
  (reproduced live during development). Fix: api_create_unknown first checks
  for an existing product with that exact name and reuses it instead of
  always creating a new one.
- Product pictures must be fetched via GET /files/productpictures/{b64name}
  (this addon has its own isolated /config, no shared filesystem with the
  Grocy addon, so direct disk access is not possible here) - proxied to the
  browser via /api/product-picture/{filename} since the browser cannot reach
  Grocy's internal hostname directly.
- ProductBarcode is a separate object from Product (POST /objects/product_barcodes).

Why the ingress page uses getUserMedia+zxing-js instead of <input capture>:
a file-input's capture attribute did not open the camera when this page was
opened inside the Home Assistant Companion App's ingress webview (confirmed
by the user testing on-device), while Grocy's own getUserMedia-based scanner,
running in the very same webview/ingress context, did open the camera.
"""

import base64
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.requests import Request
import uvicorn

GROCY_HOST = os.environ.get("GROCY_HOST", "57f327aa-grocy-linuxserver")
GROCY_BASE = f"http://{GROCY_HOST}/api"
LOCATION_ID = int(os.environ.get("GROCY_LOCATION_ID", "2"))
QU_PURCHASE = int(os.environ.get("GROCY_QU_PURCHASE", "2"))
QU_STOCK = int(os.environ.get("GROCY_QU_STOCK", "2"))


# ── Grocy HTTP helpers ────────────────────────────────────────────────────────

async def grocy_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    return await client.get(f"{GROCY_BASE}{path}", params=params)


async def grocy_post(client: httpx.AsyncClient, path: str, json_body: dict):
    return await client.post(f"{GROCY_BASE}{path}", json=json_body)


async def grocy_put(client: httpx.AsyncClient, path: str, json_body: dict | None = None, content: bytes | None = None, headers: dict | None = None):
    return await client.put(f"{GROCY_BASE}{path}", json=json_body, content=content, headers=headers)


# ── MCP tools ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shopping Products",
    instructions=(
        "Tools to find Grocy products that were created during shopping with the "
        "barcode used as a placeholder name (real name unknown at scan time, a "
        "front-of-package photo was saved instead), and to fill in the real name "
        "once the photo has been reviewed. Call get_next_unnamed_product "
        "repeatedly, updating each one with update_product, until it returns "
        "found=false."
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@mcp.tool()
async def get_next_unnamed_product() -> dict:
    """Return the next Grocy product that still has its barcode as a placeholder
    name (real name not filled in yet), along with its barcode and product
    photo. Returns {"found": false} when none are left.
    A product still needs naming if its name exactly matches one of its own
    linked barcodes - this is checked in Python since Grocy's query[] filter
    does not support this kind of comparison."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return {"found": False, "error": f"Grocy returned {pr.status_code}: {pr.text}"}
        products = pr.json()

        br = await grocy_get(client, "/objects/product_barcodes")
        barcodes_by_product: dict[int, list[str]] = {}
        if br.status_code == 200:
            for entry in br.json():
                barcodes_by_product.setdefault(entry["product_id"], []).append(entry["barcode"])

        candidates = [
            p for p in products
            if p.get("name") and p["name"] in barcodes_by_product.get(p["id"], [])
        ]
        if not candidates:
            return {"found": False}

        product = candidates[0]
        product_id = product["id"]
        barcode = product["name"]

        result = {"found": True, "product_id": product_id, "barcode": barcode}

        picture_file_name = product.get("picture_file_name")
        if picture_file_name:
            fname_b64 = base64.b64encode(picture_file_name.encode()).decode()
            pic_r = await grocy_get(client, f"/files/productpictures/{fname_b64}")
            if pic_r.status_code == 200:
                result["image"] = MCPImage(data=pic_r.content, format="jpeg")
            else:
                result["picture_error"] = f"Could not fetch picture: {pic_r.status_code}"
        else:
            result["picture_error"] = "No picture_file_name set on this product"

        return result


@mcp.tool()
async def update_product(product_id: int, name: str, description: str = "", product_group_id: int | None = None) -> dict:
    """Update a Grocy product's name and optionally description/product group,
    after reviewing its photo. Args: product_id, name (required, non-empty,
    must not equal the product's barcode - Grocy names must be unique),
    description (optional free text), product_group_id (optional category id)."""
    body = {"name": name, "description": description}
    if product_group_id is not None:
        body["product_group_id"] = product_group_id
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_put(client, f"/objects/products/{product_id}", json_body=body)
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "product_id": product_id, "name": name}


# ── Ingress web UI (scan workflow, no Claude involved) ───────────────────────

async def index(request: Request):
    with open("/static/index.html") as f:
        return HTMLResponse(f.read())


async def api_check_barcode(request: Request):
    """Body: JSON {barcode}. Read-only check against Grocy, books nothing.
    Returns {found: true, name, picture_file_name, stock_amount} or
    {found: false}."""
    data = await request.json()
    barcode = data.get("barcode")
    if not barcode:
        return JSONResponse({"found": False, "error": "Kein Barcode uebergeben"})
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, f"/stock/products/by-barcode/{barcode}")
        if r.status_code == 200:
            d = r.json()
            return JSONResponse({
                "found": True,
                "name": d["product"]["name"],
                "picture_file_name": d["product"].get("picture_file_name"),
                "stock_amount": d.get("stock_amount"),
            })
        error_message = ""
        try:
            error_message = r.json().get("error_message", "")
        except Exception:
            pass
        if "No product with barcode" in error_message:
            return JSONResponse({"found": False})
        return JSONResponse({"found": False, "error": error_message or r.text})


async def api_product_picture(request: Request):
    """Proxies a product picture from Grocy to the browser (the browser
    cannot reach Grocy's internal hostname directly)."""
    filename = request.path_params["filename"]
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, f"/files/productpictures/{fname_b64}")
        if r.status_code != 200:
            return Response(status_code=404)
        return Response(content=r.content, media_type="image/jpeg")


async def api_book(request: Request):
    """Body: JSON {barcode, amount, action}. Books directly - the frontend
    only calls this once a barcode is already confirmed known (either it was
    found by api_check_barcode, or it was just created by api_create_unknown).
    Returns status: ok | error."""
    data = await request.json()
    barcode = data.get("barcode")
    amount = data.get("amount", "1")
    action = data.get("action", "add")
    async with httpx.AsyncClient(timeout=15) as client:
        body = {"amount": float(amount), "transaction_type": "purchase" if action == "add" else "consume"}
        r = await grocy_post(client, f"/stock/products/by-barcode/{barcode}/{action}", body)
        if r.status_code == 200:
            return JSONResponse({"status": "ok", "barcode": barcode})
        error_message = ""
        try:
            error_message = r.json().get("error_message", "")
        except Exception:
            pass
        return JSONResponse({"status": "error", "message": error_message or r.text})


async def api_create_unknown(request: Request):
    """Body: multipart form with 'photo' (product front photo, a single
    explicit capture, not a continuous stream) and 'barcode'.
    Reuses an existing product with name==barcode if one already exists
    (e.g. left over from a previously interrupted run), otherwise creates a
    new one using the barcode itself as a (unique, valid) placeholder name.
    Ensures the barcode is linked and uploads the picture. Does NOT book any
    stock - the frontend calls /api/book separately afterwards."""
    form = await request.form()
    photo = await form["photo"].read()
    barcode = form["barcode"]

    async with httpx.AsyncClient(timeout=15) as client:
        # Reuse an existing product with this exact placeholder name if present.
        existing = await grocy_get(client, "/objects/products", params={"query[]": f"name={barcode}"})
        product_id = None
        if existing.status_code == 200 and existing.json():
            product_id = existing.json()[0]["id"]
        else:
            pr = await grocy_post(client, "/objects/products", {
                "name": barcode,
                "location_id": LOCATION_ID,
                "qu_id_purchase": QU_PURCHASE,
                "qu_id_stock": QU_STOCK,
            })
            if pr.status_code != 200:
                return JSONResponse({"status": "error", "message": f"Produkt anlegen fehlgeschlagen: {pr.text}"})
            product_id = pr.json()["created_object_id"]

        # Link the barcode only if not already linked to this product.
        existing_barcodes = await grocy_get(client, "/objects/product_barcodes", params={"query[]": f"product_id={product_id}"})
        already_linked = existing_barcodes.status_code == 200 and any(
            b["barcode"] == barcode for b in existing_barcodes.json()
        )
        if not already_linked:
            bcr = await grocy_post(client, "/objects/product_barcodes", {"product_id": int(product_id), "barcode": barcode})
            if bcr.status_code != 200:
                return JSONResponse({"status": "error", "message": f"Barcode verknuepfen fehlgeschlagen: {bcr.text}"})

        filename = f"scan_{barcode}.jpg"
        fname_b64 = base64.b64encode(filename.encode()).decode()
        upr = await grocy_put(client, f"/files/productpictures/{fname_b64}", content=photo, headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return JSONResponse({"status": "error", "message": f"Bild-Upload fehlgeschlagen: {upr.text}"})

        await grocy_put(client, f"/objects/products/{product_id}", json_body={"picture_file_name": filename})

        return JSONResponse({"status": "ok", "product_id": product_id})


# ── App assembly ──────────────────────────────────────────────────────────────

mcp_app = mcp.streamable_http_app()

app = Starlette(routes=[
    Route("/", index),
    Route("/api/check-barcode", api_check_barcode, methods=["POST"]),
    Route("/api/book", api_book, methods=["POST"]),
    Route("/api/create-unknown", api_create_unknown, methods=["POST"]),
    Route("/api/product-picture/{filename}", api_product_picture, methods=["GET"]),
])
app.mount("/mcp", mcp_app)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8770)
