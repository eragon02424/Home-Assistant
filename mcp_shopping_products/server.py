"""MCP Shopping Products for Home Assistant v2.0.0

Two halves:
1. Ingress web UI (live camera via getUserMedia + zxing-js) - handles the live
   scan workflow directly against Grocy's API, without involving Claude.
2. MCP tools - used by Claude to (a) fill in names for products created with
   a placeholder name during scanning, (b) find products missing a barcode
   or picture, (c) build a shopping list from a recipe/message screenshot by
   searching for products by name, and (d) create/edit recipes (including
   free-text instructions, per-ingredient quantity units) and push a scaled
   recipe's ingredients onto Grocy's shopping list, and (e) queue recipe
   image jobs for asynchronous processing via the OneDrive add-on.

v2.0.0 changes:
- Added queue_recipe_image_job MCP tool: Claude queues a job {type, grocy_id,
  bildname} into /data/image_jobs.json. A background worker runs hourly,
  fetches the image from OneDrive via /api/photo, uploads it to Grocy, and
  removes the completed job. On failure the job stays in the queue for retry.
- set_recipe_picture is no longer exposed as an MCP tool (was too slow due to
  base64 token output). It is used internally by the worker only.
- Added /share mount so the worker can read /share/onedrive_downloads/.
"""

import asyncio
import base64
import html
import json
import os
import threading
import time

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

# OneDrive SyncServer läuft auf Port 8772 (Ingress-only).
# Der /api/photo Endpunkt nimmt nur den Dateinamen, der Pfad
# "Bilder/Eigene Aufnahmen" ist im OneDrive Add-on fest hinterlegt.
ONEDRIVE_PHOTO_URL = "http://57f327aa-onedrive-syncserver:8772/api/photo"

IMAGE_JOBS_FILE = "/data/image_jobs.json"
DOWNLOAD_DIR = "/share/onedrive_downloads"

WORKER_INTERVAL_SECONDS = 3600  # 1 Stunde


# ── Job Queue helpers ─────────────────────────────────────────────────────────

def load_jobs() -> list:
    if not os.path.exists(IMAGE_JOBS_FILE):
        return []
    try:
            with open(IMAGE_JOBS_FILE) as f:
                return json.load(f)
    except Exception:
        return []


def save_jobs(jobs: list):
    os.makedirs("/data", exist_ok=True)
    with open(IMAGE_JOBS_FILE, "w") as f:
        json.dump(jobs, f, indent=2)


def add_job(job: dict):
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)


def remove_job(job_id: str):
    jobs = load_jobs()
    jobs = [j for j in jobs if j.get("id") != job_id]
    save_jobs(jobs)


def make_job_id() -> str:
    return f"job_{int(time.time() * 1000)}"


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
    newlines within a paragraph into HTML."""
    if not text:
        return text
    paragraphs = text.split("\n\n")
    html_paragraphs = []
    for para in paragraphs:
        escaped = html.escape(para).replace("\n", "<br>")
        html_paragraphs.append(f"<p>{escaped}</p>")
    return "".join(html_paragraphs)


# ── Worker: stündlicher Hintergrund-Job für Bildverarbeitung ──────────────────

async def _upload_recipe_picture(grocy_id: int, image_bytes: bytes, extension: str = "jpg") -> bool:
    """Lädt Bild intern zu Grocy hoch (früher set_recipe_picture MCP Tool)."""
    filename = f"recipe_{grocy_id}.{extension}"
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        await client.delete(f"{GROCY_BASE}/files/recipepictures/{fname_b64}")
        upr = await grocy_put(
            client,
            f"/files/recipepictures/{fname_b64}",
            content=image_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        if upr.status_code not in (200, 204):
            print(f"[Worker] Bild-Upload fehlgeschlagen: {upr.status_code} {upr.text}")
            return False
        ur = await grocy_put(
            client,
            f"/objects/recipes/{grocy_id}",
            json_body={"picture_file_name": filename},
        )
        if ur.status_code not in (200, 204):
            print(f"[Worker] picture_file_name setzen fehlgeschlagen: {ur.status_code} {ur.text}")
            return False
    return True


async def process_job(job: dict) -> bool:
    """Verarbeitet einen einzelnen Job. Gibt True zurück wenn erfolgreich."""
    job_type = job.get("type")
    grocy_id = job.get("grocy_id")
    bildname = job.get("bildname")

    if not grocy_id or not bildname:
        print(f"[Worker] Ungültiger Job (fehlende Felder): {job}")
        return False

    local_path = os.path.join(DOWNLOAD_DIR, bildname)

    if not os.path.exists(local_path):
        print(f"[Worker] Hole Bild von OneDrive: {bildname}")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    ONEDRIVE_PHOTO_URL,
                    json={"filename": bildname},
                )
            if not r.is_success:
                print(f"[Worker] OneDrive /api/photo Fehler: {r.status_code} {r.text}")
                return False
            result = r.json()
            if not result.get("success"):
                print(f"[Worker] OneDrive Download fehlgeschlagen: {result.get('error')}")
                return False
        except Exception as e:
            print(f"[Worker] OneDrive Verbindungsfehler: {e}")
            return False

    if not os.path.exists(local_path):
        print(f"[Worker] Datei nach Download nicht gefunden: {local_path}")
        return False

    ext = os.path.splitext(bildname)[1].lstrip(".").lower() or "jpg"

    with open(local_path, "rb") as f:
        image_bytes = f.read()

    print(f"[Worker] Lade Bild zu Grocy hoch: {job_type} id={grocy_id}")

    if job_type == "rezept":
        success = await _upload_recipe_picture(grocy_id, image_bytes, ext)
    else:
        print(f"[Worker] Unbekannter Job-Typ: {job_type}")
        return False

    if success:
        print(f"[Worker] Job erfolgreich: {job_type} id={grocy_id} bild={bildname}")
    return success


async def run_worker_cycle():
    """Läuft einmal durch alle offenen Jobs."""
    jobs = load_jobs()
    if not jobs:
        return

    print(f"[Worker] {len(jobs)} Job(s) in der Queue")
    completed_ids = []

    for job in jobs:
        job_id = job.get("id", "unknown")
        try:
            success = await process_job(job)
            if success:
                completed_ids.append(job_id)
            else:
                print(f"[Worker] Job {job_id} fehlgeschlagen, bleibt in Queue")
        except Exception as e:
            print(f"[Worker] Unerwarteter Fehler bei Job {job_id}: {e}")

    for job_id in completed_ids:
        remove_job(job_id)

    if completed_ids:
        print(f"[Worker] {len(completed_ids)} Job(s) abgeschlossen")


def start_background_worker():
    """Startet den Worker in einem Daemon-Thread."""
    def worker_loop():
        time.sleep(60)  # Erster Durchlauf nach 60s
        while True:
            try:
                asyncio.run(run_worker_cycle())
            except Exception as e:
                print(f"[Worker] Fehler im Worker-Cycle: {e}")
            time.sleep(WORKER_INTERVAL_SECONDS)

    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    print("[Worker] Hintergrund-Worker gestartet (Intervall: 1h)")


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
        "Recipes: create_or_update_recipe upserts a recipe by its exact name. "
        "After creating a recipe, use queue_recipe_image_job to schedule the "
        "recipe photo upload - pass the filename of the image the user sent "
        "(visible in the chat upload), and the worker will fetch it from "
        "OneDrive and attach it to the recipe automatically within the hour. "
        "search_recipes finds a recipe by name. get_recipe_ingredients lists "
        "a recipe's ingredients including their quantity unit. "
        "search_quantity_units/create_quantity_unit look up or create units. "
        "add_recipe_ingredient/update_recipe_ingredient/remove_recipe_ingredient "
        "manage individual ingredients. add_recipe_to_shopping_list adds all "
        "ingredients to the shopping list scaled by a multiplier."
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@mcp.tool()
async def queue_recipe_image_job(recipe_id: int, bildname: str) -> dict:
    """Queue an asynchronous job to fetch a recipe photo from OneDrive and
    attach it to the recipe in Grocy. The worker runs hourly and retries
    failed jobs automatically.

    Args:
        recipe_id: The Grocy recipe ID (from create_or_update_recipe).
        bildname: The filename of the photo as uploaded to OneDrive
                  (e.g. 'IMG_20260709_123456.jpg'). The OneDrive add-on
                  always looks in 'Bilder/Eigene Aufnahmen' - only the
                  filename is needed here, not the full path.

    Returns {"success": true, "job_id", "message"} on success.
    The worker will pick this up within the next hour."""
    if not bildname or not recipe_id:
        return {"success": False, "error": "recipe_id und bildname sind Pflichtfelder"}

    job = {
        "id": make_job_id(),
        "type": "rezept",
        "grocy_id": recipe_id,
        "bildname": bildname,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    add_job(job)
    print(f"[Queue] Neuer Job: {job}")
    return {
        "success": True,
        "job_id": job["id"],
        "message": f"Job in Queue: Bild '{bildname}' wird innerhalb der nächsten Stunde für Rezept {recipe_id} hinterlegt.",
    }


@mcp.tool()
async def list_image_jobs() -> dict:
    """List all pending image jobs in the queue. Useful to check whether
    a previously queued recipe image has already been processed."""
    jobs = load_jobs()
    return {"job_count": len(jobs), "jobs": jobs}


@mcp.tool()
async def get_next_unnamed_product() -> list:
    """Return the next Grocy product that still has its barcode as a placeholder
    name (real name not filled in yet), along with its barcode and product
    photo. Returns just {"found": false} when none are left."""
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
    """Return the next Grocy product that has no barcode linked to it at all."""
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
    """Return the next Grocy product that has no picture_file_name set at all."""
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
    """Update a Grocy product's name and optionally description/product group."""
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
    """Link a barcode to an existing Grocy product that currently has none."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/product_barcodes", {"product_id": product_id, "barcode": barcode})
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "product_id": product_id, "barcode": barcode}


@mcp.tool()
async def search_products(query: str) -> dict:
    """Broad substring search for Grocy products by name."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/products", params={"query[]": f"name~{query}"})
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        products = r.json()
        return {"results": [{"product_id": p["id"], "name": p.get("name")} for p in products]}


@mcp.tool()
async def create_product_simple(name: str) -> dict:
    """Create a new Grocy product with just a name - no barcode, no picture."""
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
    """Add a product to Grocy's default shopping list (list_id=1)."""
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
    """Look up Grocy quantity units by name (substring search; pass empty to list all)."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {"query[]": f"name~{query}"} if query else None
        r = await grocy_get(client, "/objects/quantity_units", params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        units = r.json()
        return {"results": [{"qu_id": u["id"], "name": u.get("name")} for u in units]}


@mcp.tool()
async def create_quantity_unit(name: str, name_plural: str = "") -> dict:
    """Create a new Grocy quantity unit (e.g. 'Gramm', 'Teelöffel', 'Prise')."""
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
    EXACT name already exists. Description is plain text (converted to HTML
    automatically). After creating a recipe, call queue_recipe_image_job to
    schedule the photo upload."""
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
    """Broad substring search for Grocy recipes by name."""
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
    """List all ingredients of a recipe with quantity units."""
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
    """Add one ingredient to a recipe."""
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
    """Change the amount (and optionally the quantity unit) of an existing recipe ingredient."""
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
    """Remove an ingredient from a recipe."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{GROCY_BASE}/objects/recipes_pos/{recipe_pos_id}")
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "recipe_pos_id": recipe_pos_id}


@mcp.tool()
async def add_recipe_to_shopping_list(recipe_id: int, multiplier: float = 1) -> dict:
    """Add every ingredient of a recipe to Grocy's shopping list, scaled by multiplier."""
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


# ── Ingress web UI ─────────────────────────────────────────────────────────────

async def index(request: Request):
    with open("/static/index.html") as f:
        return HTMLResponse(f.read())


async def api_check_barcode(request: Request):
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
    filename = request.path_params["filename"]
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, f"/files/productpictures/{fname_b64}")
        if r.status_code != 200:
            return Response(status_code=404)
        return Response(content=r.content, media_type="image/jpeg")


async def api_book(request: Request):
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
    form = await request.form()
    photo = await form["photo"].read()
    barcode = form["barcode"]

    async with httpx.AsyncClient(timeout=15) as client:
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


# ── App assembly ───────────────────────────────────────────────────────────────

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
    start_background_worker()
    uvicorn.run(app, host="0.0.0.0", port=8770)
