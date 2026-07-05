"""
MCP Shopping Products for Home Assistant v1.0.1

Two halves:
1. Ingress web UI (camera photo capture) - handles the live scan workflow
   directly against Grocy's API, without involving Claude at all.
2. MCP tools - used later by Claude in a separate session to fill in names
   for products that were created with an empty name during scanning.

Grocy API facts this code relies on (verified against a live Grocy instance
during development, see conversation history):
- Product.name has a NOT NULL constraint but accepts an empty string "".
- Filtering objects with query[]=name= (empty value) returns "Invalid query" -
  so unnamed products are found by fetching all products and filtering in
  Python, not via the query[] mechanism.
- POST /stock/products/by-barcode/{barcode}/add|consume returns HTTP 400 with
  error_message "No product with barcode ... found" for an unknown barcode -
  this exact substring is used to detect the unknown-product case.
- Product pictures must be fetched via GET /files/productpictures/{b64name}
  (this addon has its own isolated /config, no shared filesystem with the
  Grocy addon, so direct disk access is not possible here).
- ProductBarcode is a separate object from Product (POST /objects/product_barcodes).
"""

import base64
import io
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
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
    r = await client.get(f"{GROCY_BASE}{path}", params=params)
    return r


async def grocy_post(client: httpx.AsyncClient, path: str, json_body: dict):
    r = await client.post(f"{GROCY_BASE}{path}", json=json_body)
    return r


async def grocy_put(client: httpx.AsyncClient, path: str, json_body: dict | None = None, content: bytes | None = None, headers: dict | None = None):
    r = await client.put(f"{GROCY_BASE}{path}", json=json_body, content=content, headers=headers)
    return r


# ── Barcode decoding ──────────────────────────────────────────────────────────

def decode_barcode(image_bytes: bytes) -> str | None:
    """Decode a barcode from image bytes using zbar. Returns the barcode string
    or None if nothing was found. Note: reliability depends heavily on image
    resolution and orientation - this was confirmed to fail on downscaled
    (~800px) test images but succeed on a full-resolution, correctly oriented
    photo during development."""
    img = Image.open(io.BytesIO(image_bytes))
    results = zbar_decode(img)
    if not results:
        return None
    return results[0].data.decode("utf-8")


# ── MCP tools ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shopping Products",
    instructions=(
        "Tools to find Grocy products that were created during shopping with an "
        "empty name (barcode unknown at scan time, front-of-package photo was "
        "saved instead), and to fill in the name/details once a photo has been "
        "reviewed. Call get_next_unnamed_product repeatedly, updating each one "
        "with update_product, until it returns found=false."
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@mcp.tool()
async def get_next_unnamed_product() -> dict:
    """Return the next Grocy product that still has an empty name (created
    during scanning because its barcode was unknown), along with its barcode
    and product photo. Returns {"found": false} when none are left.
    Server-side filtering on an empty name is not possible (Grocy's query[]
    rejects an empty comparison value), so this fetches all products and
    filters in Python."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/products")
        if r.status_code != 200:
            return {"found": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        products = r.json()
        candidates = [p for p in products if p.get("name", "") == ""]
        if not candidates:
            return {"found": False}
        product = candidates[0]
        product_id = product["id"]

        barcode = None
        br = await grocy_get(client, "/objects/product_barcodes", params={"query[]": f"product_id={product_id}"})
        if br.status_code == 200:
            barcodes = br.json()
            if barcodes:
                barcode = barcodes[0].get("barcode")

        result = {
            "found": True,
            "product_id": product_id,
            "barcode": barcode,
        }

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
    after reviewing its photo. Args: product_id, name (required, non-empty),
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


async def api_scan(request: Request):
    """Body: multipart form with 'photo' (barcode photo), 'amount', 'action' (add|consume).
    Decodes the barcode and attempts the stock booking directly against Grocy.
    Returns status: ok | unknown | error."""
    form = await request.form()
    photo = await form["photo"].read()
    amount = form.get("amount", "1")
    action = form.get("action", "add")

    barcode = decode_barcode(photo)
    if barcode is None:
        return JSONResponse({"status": "error", "message": "Kein Barcode im Bild erkannt"})

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
        if "No product with barcode" in error_message:
            return JSONResponse({"status": "unknown", "barcode": barcode})
        return JSONResponse({"status": "error", "message": error_message or r.text})


async def api_create_unknown(request: Request):
    """Body: multipart form with 'photo' (product front photo), 'barcode', 'amount', 'action'.
    Creates the product with an empty name, links the barcode, uploads the
    picture, then retries the originally requested stock booking."""
    form = await request.form()
    photo = await form["photo"].read()
    barcode = form["barcode"]
    amount = form.get("amount", "1")
    action = form.get("action", "add")

    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_post(client, "/objects/products", {
            "name": "",
            "location_id": LOCATION_ID,
            "qu_id_purchase": QU_PURCHASE,
            "qu_id_stock": QU_STOCK,
        })
        if pr.status_code != 200:
            return JSONResponse({"status": "error", "message": f"Produkt anlegen fehlgeschlagen: {pr.text}"})
        product_id = pr.json()["created_object_id"]

        bcr = await grocy_post(client, "/objects/product_barcodes", {"product_id": int(product_id), "barcode": barcode})
        if bcr.status_code != 200:
            return JSONResponse({"status": "error", "message": f"Barcode verknuepfen fehlgeschlagen: {bcr.text}"})

        filename = f"scan_{barcode}.jpg"
        fname_b64 = base64.b64encode(filename.encode()).decode()
        upr = await grocy_put(client, f"/files/productpictures/{fname_b64}", content=photo, headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return JSONResponse({"status": "error", "message": f"Bild-Upload fehlgeschlagen: {upr.text}"})

        await grocy_put(client, f"/objects/products/{product_id}", json_body={"picture_file_name": filename})

        body = {"amount": float(amount), "transaction_type": "purchase" if action == "add" else "consume"}
        sr = await grocy_post(client, f"/stock/products/by-barcode/{barcode}/{action}", body)
        if sr.status_code != 200:
            return JSONResponse({"status": "error", "message": f"Produkt angelegt, aber Buchung fehlgeschlagen: {sr.text}"})

        return JSONResponse({"status": "ok", "product_id": product_id})


# ── App assembly ──────────────────────────────────────────────────────────────
# FastMCP's ASGI app is mounted at /mcp; the ingress web UI and its own small
# JSON API live at the root paths. Both share one Python process and one port.

mcp_app = mcp.streamable_http_app()

app = Starlette(routes=[
    Route("/", index),
    Route("/api/scan", api_scan, methods=["POST"]),
    Route("/api/create-unknown", api_create_unknown, methods=["POST"]),
])
app.mount("/mcp", mcp_app)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8770)
