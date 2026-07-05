"""
MCP Shopping Products for Home Assistant v1.4.0

Two halves:
1. Ingress web UI (live camera via getUserMedia + zxing-js) - handles the live
   scan workflow directly against Grocy's API, without involving Claude.
2. MCP tools - used by Claude to (a) fill in names for products created with
   a placeholder name during scanning, (b) find products missing a barcode
   or picture, and (c) build a shopping list from a recipe/message screenshot
   by searching for products by name and adding them to Grocy's shopping list.

Grocy API facts this code relies on (verified against a live Grocy instance
during development, see conversation history):
- GET /stock/products/by-barcode/{barcode} returns 200 with product/
  picture_file_name/stock_amount for a known barcode, or 400 with
  error_message "No product with barcode ... found" for an unknown one.
- Product.name has a NOT NULL constraint AND a UNIQUE constraint. An empty
  string collides after the first use - the barcode string itself is used as
  a unique placeholder name during scanning instead (see api_create_unknown).
- query[]=name~<text> (the "~" operator) does a SQL LIKE substring search and
  returns ALL matches - confirmed live with name~Sprite returning two
  differently-named Sprite products. query[]=name=<text> requires an exact,
  non-empty value ("Invalid query" if the value is empty).
- Product pictures must be fetched via GET /files/productpictures/{b64name}
  (this addon has its own isolated /config, no shared filesystem with the
  Grocy addon) - proxied to the browser via /api/product-picture/{filename}.
- ProductBarcode is a separate object from Product (POST /objects/product_barcodes) -
  a product can exist validly with zero linked barcodes and/or no picture.
- POST /stock/shoppinglist/add-product requires a product_id (not free text);
  confirmed live (204, entry verified via GET /objects/shopping_list) that it
  adds/increments an item on the given (or default, list_id=1) shopping list.

Why the ingress page uses getUserMedia+zxing-js instead of <input capture>:
a file-input's capture attribute did not open the camera when this page was
opened inside the Home Assistant Companion App's ingress webview (confirmed
by the user testing on-device), while Grocy's own getUserMedia-based scanner,
running in the very same webview/ingress context, did open the camera.

Routing fix (v1.3.1): mcp.streamable_http_app() already serves its own single
route internally at exactly "/mcp". Mounting that whole app under ANOTHER
"/mcp" prefix via Starlette's app.mount() made the endpoint unreachable (307
to /mcp/, then 404). Fixed by merging mcp_app's routes directly into this
file's own Starlette app and passing through its lifespan.

Image return fix (v1.3.2): a dict with an Image instance as one of its values
does NOT produce a viewable image for the MCP client - confirmed live, the
client received the literal Python repr string instead of a picture. Root
cause (mcp/server/fastmcp/utilities/func_metadata.py, _convert_to_content):
FastMCP only special-cases a tool's return value when it IS an Image
instance directly, or a list/tuple whose items are converted recursively.
Fix: return [info_dict, Image(...)] as a list instead of nesting the Image
inside the dict.
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


async def fetch_product_image(client: httpx.AsyncClient, picture_file_name: str) -> MCPImage | None:
    fname_b64 = base64.b64encode(picture_file_name.encode()).decode()
    r = await grocy_get(client, f"/files/productpictures/{fname_b64}")
    if r.status_code == 200:
        return MCPImage(data=r.content, format="jpeg")
    return None


# ── MCP tools ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shopping Products",
    instructions=(
        "Tools to maintain Grocy products and build shopping lists. "
        "get_next_unnamed_product finds products still carrying their barcode "
        "as a placeholder name. get_next_product_without_barcode finds products "
        "with no barcode linked at all. get_next_product_without_picture finds "
        "products missing a photo. search_products does a broad substring "
        "search by name (e.g. 'Mehl' returns all flour products) - review the "
        "returned list yourself and pick the right product_id based on the "
        "user's wording; do not assume a single result is automatically "
        "correct. create_product_simple makes a new product with just a name "
        "(no barcode, no picture) when search_products found no suitable "
        "match. add_to_shopping_list adds a product_id to Grocy's shopping "
        "list by amount."
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@mcp.tool()
async def get_next_unnamed_product() -> list:
    """Return the next Grocy product that still has its barcode as a placeholder
    name (real name not filled in yet), along with its barcode and product
    photo. Returns just {"found": false} when none are left.
    A product still needs naming if its name exactly matches one of its own
    linked barcodes - this is checked in Python since Grocy's query[] filter
    does not support this kind of comparison."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return [{"found": False, "error": f"Grocy returned {pr.status_code}: {pr.text}"}]
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
            return [{"found": False}]

        product = candidates[0]
        product_id = product["id"]
        barcode = product["name"]

        info = {"found": True, "product_id": product_id, "barcode": barcode}

        picture_file_name = product.get("picture_file_name")
        if picture_file_name:
            image = await fetch_product_image(client, picture_file_name)
            if image:
                return [info, image]
            info["picture_error"] = "Could not fetch picture"
        else:
            info["picture_error"] = "No picture_file_name set on this product"

        return [info]


@mcp.tool()
async def get_next_product_without_barcode() -> list:
    """Return the next Grocy product that has no barcode linked to it at all
    (checked by cross-referencing every product id against the full
    product_barcodes list - not the same case as get_next_unnamed_product,
    which is about products that DO have a barcode but it's being used as a
    placeholder name). Returns just {"found": false} when none are left."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return [{"found": False, "error": f"Grocy returned {pr.status_code}: {pr.text}"}]
        products = pr.json()

        br = await grocy_get(client, "/objects/product_barcodes")
        product_ids_with_barcode = set()
        if br.status_code == 200:
            product_ids_with_barcode = {entry["product_id"] for entry in br.json()}

        candidates = [p for p in products if p["id"] not in product_ids_with_barcode]
        if not candidates:
            return [{"found": False}]

        product = candidates[0]
        info = {"found": True, "product_id": product["id"], "name": product.get("name")}

        picture_file_name = product.get("picture_file_name")
        if picture_file_name:
            image = await fetch_product_image(client, picture_file_name)
            if image:
                return [info, image]

        return [info]


@mcp.tool()
async def get_next_product_without_picture() -> dict:
    """Return the next Grocy product that has no picture_file_name set at all
    (regardless of whether it has a real name or a barcode-as-placeholder
    name). Returns {"found": false, "product_id", "name", "barcode"} - there is
    no image to return here by definition, since this tool exists to find
    products that are missing one."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return {"found": False, "error": f"Grocy returned {pr.status_code}: {pr.text}"}
        products = pr.json()

        candidates = [p for p in products if not p.get("picture_file_name")]
        if not candidates:
            return {"found": False}

        product = candidates[0]
        product_id = product["id"]

        barcode = None
        br = await grocy_get(client, "/objects/product_barcodes", params={"query[]": f"product_id={product_id}"})
        if br.status_code == 200:
            entries = br.json()
            if entries:
                barcode = entries[0]["barcode"]

        return {
            "found": True,
            "product_id": product_id,
            "name": product.get("name"),
            "barcode": barcode,
        }


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


@mcp.tool()
async def add_product_barcode(product_id: int, barcode: str) -> dict:
    """Link a barcode to an existing Grocy product that currently has none.
    Args: product_id, barcode (the EAN/UPC string to link)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/product_barcodes", {"product_id": product_id, "barcode": barcode})
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "product_id": product_id, "barcode": barcode}


@mcp.tool()
async def search_products(query: str) -> dict:
    """Broad substring search for Grocy products by name (SQL LIKE, e.g.
    'Mehl' returns every product whose name contains 'Mehl' - Vollkornmehl,
    Weizenmehl Type 405, etc. all at once). Returns {"results": [{"product_id",
    "name"}, ...]}. Caller must pick the correct product_id from the list
    based on context (e.g. the user asking specifically for '405') - this
    tool does not attempt to disambiguate itself, and if the distinguishing
    detail isn't actually part of the stored name, it cannot be told apart
    from the returned data alone."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/products", params={"query[]": f"name~{query}"})
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        products = r.json()
        return {"results": [{"product_id": p["id"], "name": p.get("name")} for p in products]}


@mcp.tool()
async def create_product_simple(name: str) -> dict:
    """Create a new Grocy product with just a name - no barcode, no picture.
    Use this when search_products found no suitable existing match for an
    item from a shopping list/recipe. Uses this addon's configured default
    location_id/qu_id_purchase/qu_id_stock."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/products", {
            "name": name,
            "location_id": LOCATION_ID,
            "qu_id_purchase": QU_PURCHASE,
            "qu_id_stock": QU_STOCK,
        })
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "product_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def add_to_shopping_list(product_id: int, amount: float = 1, note: str = "") -> dict:
    """Add a product to Grocy's default shopping list (list_id=1). If the
    product is already on the list, Grocy increases the existing amount
    rather than duplicating the entry (this is Grocy's own behavior, not
    something this tool checks for). Args: product_id, amount (default 1),
    note (optional)."""
    async with httpx.AsyncClient(timeout=15) as client:
        body = {"product_id": product_id, "product_amount": amount}
        if note:
            body["note"] = note
        r = await grocy_post(client, "/stock/shoppinglist/add-product", body)
        if r.status_code != 204:
            error_message = ""
            try:
                error_message = r.json().get("error_message", "")
            except Exception:
                pass
            return {"success": False, "error": error_message or r.text}
        return {"success": True, "product_id": product_id, "amount": amount}


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
# mcp_app's own route ("/mcp") is merged directly into this app's route list
# (not nested via Mount). Its lifespan is passed through so FastMCP's session
# manager actually starts.

mcp_app = mcp.streamable_http_app()

app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/check-barcode", api_check_barcode, methods=["POST"]),
        Route("/api/book", api_book, methods=["POST"]),
        Route("/api/create-unknown", api_create_unknown, methods=["POST"]),
        Route("/api/product-picture/{filename}", api_product_picture, methods=["GET"]),
    ] + mcp_app.routes,
    lifespan=mcp_app.router.lifespan_context,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8770)
