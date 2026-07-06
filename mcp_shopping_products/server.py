"""
MCP Shopping Products for Home Assistant v1.9.0

Two halves:
1. Ingress web UI (live camera via getUserMedia + zxing-js) - handles the live
   scan workflow directly against Grocy's API, without involving Claude.
2. MCP tools - used by Claude to (a) fill in names for products created with
   a placeholder name during scanning, (b) find products missing a barcode
   or picture, (c) build a shopping list from a recipe/message screenshot by
   searching for products by name, and (d) create/edit recipes (including
   free-text instructions, per-ingredient quantity units, and a dish photo)
   and push a scaled recipe's ingredients onto Grocy's shopping list.

Grocy API facts this code relies on (verified against a live Grocy instance
during development, see conversation history):
- GET /stock/products/by-barcode/{barcode} returns 200 with product/
  picture_file_name/stock_amount for a known barcode, or 400 with
  error_message "No product with barcode ... found" for an unknown one.
- Product.name has a NOT NULL constraint AND a UNIQUE constraint. An empty
  string collides after the first use - the barcode string itself is used as
  a unique placeholder name during scanning instead (see api_create_unknown).
- query[]=name~<text> (the "~" operator) does a SQL LIKE substring search and
  returns ALL matches. query[]=name=<text> requires an exact, non-empty value
  ("Invalid query" if the value is empty) - used for exact-name lookups.
- Product pictures must be fetched via GET /files/productpictures/{b64name}
  (this addon has its own isolated /config, no shared filesystem with the
  Grocy addon) - proxied to the browser via /api/product-picture/{filename}.
- ProductBarcode is a separate object from Product (POST /objects/product_barcodes) -
  a product can exist validly with zero linked barcodes and/or no picture.
- POST /stock/shoppinglist/add-product requires a product_id (not free text);
  confirmed live (204, entry verified via GET /objects/shopping_list) that it
  adds/increments an item on the given (or default, list_id=1) shopping list.
- Recipes: entity names are "recipes" and "recipes_pos". POST /objects/recipes
  only strictly needs "name" (base_servings/desired_servings default to 1).
  The "description" field is a RICH-TEXT/HTML field (Grocy's recipe editor is
  a WYSIWYG editor with bold/underline/list buttons, confirmed from a
  screenshot of the actual edit page) - it is NOT plain text. Sending plain
  text containing "\\n" characters gets stored verbatim, but HTML collapses
  ordinary whitespace/newlines when rendering, so every line runs together
  into one unbroken paragraph in Grocy's UI (confirmed live: this exact
  problem was reported and reproduced by fetching the stored description and
  seeing literal "\\n" characters in it). Fix: create_or_update_recipe now
  runs description through text_to_html(), which converts blank-line-
  separated blocks into separate <p> paragraphs and single newlines within a
  block into <br> tags, before sending it to Grocy.
  POST /objects/recipes_pos only strictly needs recipe_id/product_id/amount,
  but ALSO accepts an optional qu_id (column added in migration 0034) to
  record which quantity unit (g, ml, tsp, piece, pinch, ...) the amount is
  measured in. Without qu_id, Grocy silently falls back to the product's own
  default stock unit, which is wrong whenever a recipe's ingredient unit
  differs from that product's usual stock unit (e.g. "Butter" tracked in
  "Stück" in stock but needed in "Gramm" for a recipe) - fixed by adding
  qu_id support to add_recipe_ingredient/update_recipe_ingredient plus a
  search_quantity_units/create_quantity_unit tool pair.
- Deleting a recipe does NOT cascade-delete its recipes_pos rows (confirmed
  live: an orphaned recipes_pos row with a dangling recipe_id remained after
  DELETE /objects/recipes/{id} and had to be removed separately).
- add_recipe_to_shopping_list intentionally does NOT use Grocy's stock-
  fulfillment-based add-not-fulfilled-products-to-shoppinglist endpoint (used
  in an earlier version) - the user explicitly said the "only order the
  deficit versus current stock" behavior is not wanted for now. Instead it
  reads recipes_pos directly and adds amount*multiplier for every ingredient
  unconditionally.
- Recipe pictures: the /files/{group}/{fileName} endpoint's "group" path
  parameter accepts "recipepictures" as a valid FileGroups enum value
  (confirmed against the OpenAPI schema and live). Since Claude has no direct
  byte-level access to images the user pastes into chat, set_recipe_picture
  takes a base64-encoded string parameter instead of a file upload - Claude
  reads the user-uploaded image from its own sandbox, base64-encodes it
  there, and passes that string as a tool argument.
- Grocy's file PUT does NOT overwrite an existing file at the same path - it
  returns 400 "Error while creating file ..." if one already exists there.
  Fixed in set_recipe_picture by issuing a DELETE for that path first
  (best-effort, response ignored) before the PUT.

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
import html
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


async def fetch_image(client: httpx.AsyncClient, group: str, picture_file_name: str) -> MCPImage | None:
    fname_b64 = base64.b64encode(picture_file_name.encode()).decode()
    r = await grocy_get(client, f"/files/{group}/{fname_b64}")
    if r.status_code == 200:
        return MCPImage(data=r.content, format="jpeg")
    return None


async def fetch_product_image(client: httpx.AsyncClient, picture_file_name: str) -> MCPImage | None:
    return await fetch_image(client, "productpictures", picture_file_name)


def text_to_html(text: str) -> str:
    """Convert plain text with blank-line-separated paragraphs and single
    newlines within a paragraph into HTML - needed because Grocy's recipe
    description field is a rich-text/HTML field, not plain text, and
    ordinary "\\n" characters get stored verbatim but collapsed away when
    rendered as HTML (confirmed live - see module docstring)."""
    if not text:
        return text
    paragraphs = text.split("\n\n")
    html_paragraphs = []
    for para in paragraphs:
        escaped = html.escape(para).replace("\n", "<br>")
        html_paragraphs.append(f"<p>{escaped}</p>")
    return "".join(html_paragraphs)


# ── MCP tools ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shopping Products",
    instructions=(
        "Tools to maintain Grocy products, recipes and shopping lists. "
        "get_next_unnamed_product finds products still carrying their barcode "
        "as a placeholder name. get_next_product_without_barcode finds products "
        "with no barcode linked at all. get_next_product_without_picture finds "
        "products missing a photo. search_products does a broad substring "
        "search by name - review the returned list yourself and pick the "
        "right product_id based on the user's wording; do not assume a single "
        "result is automatically correct. create_product_simple makes a new "
        "product with just a name (no barcode, no picture) when "
        "search_products found no suitable match. add_to_shopping_list adds a "
        "product_id to Grocy's shopping list by amount.\n\n"
        "Recipes: create_or_update_recipe upserts a recipe by its exact name "
        "(name, description - plain text with blank lines between paragraphs "
        "and single newlines for line breaks, e.g. numbered steps each on "
        "their own line - this gets converted to HTML automatically since "
        "Grocy's description field is rich-text, not plain text; and "
        "base_servings, the serving count all ingredient amounts are defined "
        "for). search_recipes finds a recipe by name. get_recipe_ingredients "
        "lists a recipe's ingredients including their quantity unit. "
        "search_quantity_units/create_quantity_unit look up or create units "
        "like Gramm/Teelöffel/Prise - always pass the correct qu_id to "
        "add_recipe_ingredient/update_recipe_ingredient, since omitting it "
        "silently defaults to the product's own stock unit which is usually "
        "wrong for a recipe (e.g. Butter tracked in Stück in stock but "
        "needed in Gramm for a recipe). "
        "add_recipe_ingredient/update_recipe_ingredient/remove_recipe_ingredient "
        "manage individual ingredients. add_recipe_to_shopping_list takes "
        "every ingredient's amount times a multiplier and adds it to the "
        "shopping list directly - it does NOT check current stock or "
        "existing shopping list amounts, it always adds the full scaled "
        "quantity (and does not currently convert units - the shopping list "
        "amount will be in the ingredient's recipe unit). set_recipe_picture "
        "attaches a dish photo to a recipe (pass the image as a base64 "
        "string, read from Claude's own sandbox) and overwrites any existing "
        "one; get_recipe_picture retrieves it again."
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


@mcp.tool()
async def search_quantity_units(query: str = "") -> dict:
    """Look up Grocy quantity units by name (substring search; pass an empty
    query to list all of them). Returns {"results": [{"qu_id", "name"}, ...]}.
    Use this before add_recipe_ingredient to find the correct qu_id for a
    unit like Gramm/ml/Teelöffel/Stück/Prise/Päckchen."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {"query[]": f"name~{query}"} if query else None
        r = await grocy_get(client, "/objects/quantity_units", params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        units = r.json()
        return {"results": [{"qu_id": u["id"], "name": u.get("name")} for u in units]}


@mcp.tool()
async def create_quantity_unit(name: str, name_plural: str = "") -> dict:
    """Create a new Grocy quantity unit (e.g. 'Gramm', 'Teelöffel', 'Prise').
    Args: name, name_plural (defaults to name if not given)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/quantity_units", {
            "name": name,
            "name_plural": name_plural or name,
        })
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "qu_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def create_or_update_recipe(name: str, description: str = "", base_servings: float = 1) -> dict:
    """Create a new recipe, or update an existing one if a recipe with this
    EXACT name already exists (matched via an exact, case-sensitive name
    lookup). Args: name (required - used as the lookup key), description
    (plain text - write it with blank lines between paragraphs and a single
    newline per line break, e.g. numbered steps each on their own line; this
    is automatically converted to HTML before saving, since Grocy's
    description field is rich-text/HTML and does not respect plain
    newlines), base_servings (default 1 - the serving count all ingredient
    amounts are defined for). Returns {"success", "recipe_id",
    "created": true|false}."""
    html_description = text_to_html(description)
    async with httpx.AsyncClient(timeout=15) as client:
        existing = await grocy_get(client, "/objects/recipes", params={"query[]": f"name={name}"})
        if existing.status_code == 200 and existing.json():
            recipe_id = existing.json()[0]["id"]
            ur = await grocy_put(client, f"/objects/recipes/{recipe_id}", json_body={
                "description": html_description,
                "base_servings": base_servings,
            })
            if ur.status_code not in (200, 204):
                return {"success": False, "error": f"Grocy returned {ur.status_code}: {ur.text}"}
            return {"success": True, "recipe_id": recipe_id, "name": name, "created": False}

        cr = await grocy_post(client, "/objects/recipes", {
            "name": name,
            "description": html_description,
            "base_servings": base_servings,
        })
        if cr.status_code != 200:
            return {"success": False, "error": f"Grocy returned {cr.status_code}: {cr.text}"}
        return {"success": True, "recipe_id": int(cr.json()["created_object_id"]), "name": name, "created": True}


@mcp.tool()
async def search_recipes(query: str) -> dict:
    """Broad substring search for Grocy recipes by name. Returns
    {"results": [{"recipe_id", "name", "base_servings", "description"}, ...]}."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/recipes", params={"query[]": f"name~{query}"})
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        recipes = r.json()
        return {"results": [
            {
                "recipe_id": rec["id"],
                "name": rec.get("name"),
                "base_servings": rec.get("base_servings"),
                "description": rec.get("description"),
            }
            for rec in recipes
        ]}


@mcp.tool()
async def get_recipe_ingredients(recipe_id: int) -> dict:
    """List all ingredients of a recipe. Returns {"results": [{"recipe_pos_id",
    "product_id", "product_name", "amount", "qu_id", "unit_name"}, ...]} where
    amount is defined for the recipe's base_servings, and unit_name is null
    if no qu_id was set on that ingredient (meaning Grocy falls back to the
    product's own default stock unit)."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
        if pr.status_code != 200:
            return {"results": [], "error": f"Grocy returned {pr.status_code}: {pr.text}"}
        positions = pr.json()

        results = []
        for pos in positions:
            name = None
            prod = await grocy_get(client, f"/objects/products/{pos['product_id']}")
            if prod.status_code == 200:
                name = prod.json().get("name")

            unit_name = None
            qu_id = pos.get("qu_id")
            if qu_id:
                qu = await grocy_get(client, f"/objects/quantity_units/{qu_id}")
                if qu.status_code == 200:
                    unit_name = qu.json().get("name")

            results.append({
                "recipe_pos_id": pos["id"],
                "product_id": pos["product_id"],
                "product_name": name,
                "amount": pos["amount"],
                "qu_id": qu_id,
                "unit_name": unit_name,
            })
        return {"results": results}


@mcp.tool()
async def add_recipe_ingredient(recipe_id: int, product_id: int, amount: float, qu_id: int | None = None) -> dict:
    """Add one ingredient to a recipe. Args: recipe_id, product_id, amount
    (the quantity of this product needed for the recipe's base_servings -
    e.g. if base_servings=4 and the recipe needs 200g flour for 4 servings,
    amount=200, not a per-serving amount), qu_id (the quantity unit id from
    search_quantity_units/create_quantity_unit - e.g. Gramm for flour,
    Teelöffel for cocoa powder. STRONGLY RECOMMENDED to always set this: if
    omitted, Grocy silently uses the product's own default stock unit, which
    is very often wrong for a recipe amount - e.g. a product tracked in
    'Stück' in stock but needed in 'Gramm' for this recipe)."""
    async with httpx.AsyncClient(timeout=15) as client:
        body = {
            "recipe_id": recipe_id,
            "product_id": product_id,
            "amount": amount,
        }
        if qu_id is not None:
            body["qu_id"] = qu_id
        r = await grocy_post(client, "/objects/recipes_pos", body)
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "recipe_pos_id": int(r.json()["created_object_id"])}


@mcp.tool()
async def update_recipe_ingredient(recipe_pos_id: int, amount: float, qu_id: int | None = None) -> dict:
    """Change the amount (and optionally the quantity unit) of an existing
    recipe ingredient. Args: recipe_pos_id (from get_recipe_ingredients),
    amount (new quantity, at the recipe's base_servings), qu_id (optional -
    only changes the unit if given)."""
    body = {"amount": amount}
    if qu_id is not None:
        body["qu_id"] = qu_id
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_put(client, f"/objects/recipes_pos/{recipe_pos_id}", json_body=body)
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "recipe_pos_id": recipe_pos_id, "amount": amount}


@mcp.tool()
async def remove_recipe_ingredient(recipe_pos_id: int) -> dict:
    """Remove an ingredient from a recipe. Args: recipe_pos_id (from
    get_recipe_ingredients)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{GROCY_BASE}/objects/recipes_pos/{recipe_pos_id}")
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "recipe_pos_id": recipe_pos_id}


@mcp.tool()
async def add_recipe_to_shopping_list(recipe_id: int, multiplier: float = 1) -> dict:
    """Add every ingredient of a recipe to Grocy's shopping list, each scaled
    by multiplier (e.g. multiplier=1.5 doubles-and-a-half every ingredient
    amount). This adds the full scaled amount unconditionally - it does NOT
    check current stock or existing shopping list amounts and subtract them
    first (that stock-fulfillment behavior was intentionally removed per the
    user's request). Note: this does not convert units - the shopping list
    entry gets the recipe ingredient's amount as-is, in whatever unit that
    ingredient's qu_id is (or the product's default stock unit if none was
    set). Returns {"success", "added": [{"product_id", "product_name",
    "amount"}, ...]}."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
        if pr.status_code != 200:
            return {"success": False, "error": f"Grocy returned {pr.status_code}: {pr.text}"}
        positions = pr.json()
        if not positions:
            return {"success": False, "error": "Recipe has no ingredients (or does not exist)"}

        added = []
        for pos in positions:
            scaled_amount = pos["amount"] * multiplier
            body = {"product_id": pos["product_id"], "product_amount": scaled_amount}
            ar = await grocy_post(client, "/stock/shoppinglist/add-product", body)
            if ar.status_code != 204:
                error_message = ""
                try:
                    error_message = ar.json().get("error_message", "")
                except Exception:
                    pass
                return {"success": False, "error": f"Failed on product_id {pos['product_id']}: {error_message or ar.text}", "added_so_far": added}

            name = None
            prod = await grocy_get(client, f"/objects/products/{pos['product_id']}")
            if prod.status_code == 200:
                name = prod.json().get("name")
            added.append({"product_id": pos["product_id"], "product_name": name, "amount": scaled_amount})

        return {"success": True, "added": added}


@mcp.tool()
async def set_recipe_picture(recipe_id: int, image_base64: str, extension: str = "jpg") -> dict:
    """Attach a dish photo to a recipe. Args: recipe_id, image_base64 (the
    image's bytes, base64-encoded as a string - Claude reads the image from
    its own sandbox and encodes it before calling this), extension (file
    extension without dot, default 'jpg'). Overwrites any existing picture on
    this recipe."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            image_bytes = base64.b64decode(image_base64)
        except Exception as e:
            return {"success": False, "error": f"Invalid base64: {e}"}

        filename = f"recipe_{recipe_id}.{extension}"
        fname_b64 = base64.b64encode(filename.encode()).decode()
        # Delete any existing file at this path first - Grocy's file PUT does
        # not overwrite, it errors "Error while creating file ..." if a file
        # already exists there (confirmed live: re-uploading a picture to the
        # same recipe_id failed with this error until this delete was added).
        await client.delete(f"{GROCY_BASE}/files/recipepictures/{fname_b64}")
        upr = await grocy_put(client, f"/files/recipepictures/{fname_b64}", content=image_bytes, headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return {"success": False, "error": f"Upload fehlgeschlagen: Grocy returned {upr.status_code}: {upr.text}"}

        ur = await grocy_put(client, f"/objects/recipes/{recipe_id}", json_body={"picture_file_name": filename})
        if ur.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {ur.status_code}: {ur.text}"}

        return {"success": True, "recipe_id": recipe_id, "picture_file_name": filename}


@mcp.tool()
async def get_recipe_picture(recipe_id: int) -> list:
    """Retrieve a recipe's dish photo, if one is set. Returns just
    {"found": false} if the recipe has no picture_file_name set."""
    async with httpx.AsyncClient(timeout=15) as client:
        rr = await grocy_get(client, f"/objects/recipes/{recipe_id}")
        if rr.status_code != 200:
            return [{"found": False, "error": f"Grocy returned {rr.status_code}: {rr.text}"}]
        picture_file_name = rr.json().get("picture_file_name")
        if not picture_file_name:
            return [{"found": False}]

        image = await fetch_image(client, "recipepictures", picture_file_name)
        if not image:
            return [{"found": False, "error": "Could not fetch picture"}]
        return [{"found": True, "recipe_id": recipe_id}, image]


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
