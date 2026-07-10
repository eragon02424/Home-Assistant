"""MCP Shopping Products for Home Assistant v2.2.0

v2.2.0 changes:
- trigger_image_worker: new MCP tool that runs the job queue immediately
  instead of waiting for the next hourly cycle. Uses an asyncio.Event to
  signal the background thread; the tool awaits the result (max 120s) and
  returns a summary of what was processed. A _worker_lock prevents parallel
  runs if the hourly timer fires during a manual trigger.

v2.1.0: get_image_job, list/create/update/delete_product_group
v2.0.0: queue_recipe_image_job, async OneDrive worker, /share mount
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

ONEDRIVE_PHOTO_URL = "http://57f327aa-onedrive-syncserver:8772/api/photo"
IMAGE_JOBS_FILE = "/data/image_jobs.json"
DOWNLOAD_DIR = "/share/onedrive_downloads"
WORKER_INTERVAL_SECONDS = 3600

# Prevents two worker cycles running in parallel (hourly + manual trigger)
_worker_lock = threading.Lock()

# Signalled by trigger_image_worker to wake the background thread immediately
_worker_trigger = threading.Event()

# Shared result written by the last worker cycle, read by trigger_image_worker
_last_cycle_result: dict = {}


# ── Job Queue helpers ──────────────────────────────────────────────────

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


# ── Grocy HTTP helpers ────────────────────────────────────────────────

async def grocy_get(client, path, params=None):
    return await client.get(f"{GROCY_BASE}{path}", params=params)

async def grocy_post(client, path, json_body):
    return await client.post(f"{GROCY_BASE}{path}", json=json_body)

async def grocy_put(client, path, json_body=None, content=None, headers=None):
    return await client.put(f"{GROCY_BASE}{path}", json=json_body, content=content, headers=headers)

async def fetch_image(client, group, picture_file_name):
    fname_b64 = base64.b64encode(picture_file_name.encode()).decode()
    r = await grocy_get(client, f"/files/{group}/{fname_b64}")
    if r.status_code == 200:
        return MCPImage(data=r.content, format="jpeg")
    return None

async def fetch_product_image(client, picture_file_name):
    return await fetch_image(client, "productpictures", picture_file_name)

def text_to_html(text: str) -> str:
    if not text:
        return text
    return "".join(
        f"<p>{html.escape(para).replace(chr(10), '<br>')}</p>"
        for para in text.split("\n\n")
    )


# ── Worker ──────────────────────────────────────────────────────────────────

async def _upload_recipe_picture(grocy_id: int, image_bytes: bytes, extension: str = "jpg") -> bool:
    filename = f"recipe_{grocy_id}.{extension}"
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        await client.delete(f"{GROCY_BASE}/files/recipepictures/{fname_b64}")
        upr = await grocy_put(client, f"/files/recipepictures/{fname_b64}",
                              content=image_bytes, headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            print(f"[Worker] Bild-Upload fehlgeschlagen: {upr.status_code} {upr.text}")
            return False
        ur = await grocy_put(client, f"/objects/recipes/{grocy_id}",
                             json_body={"picture_file_name": filename})
        if ur.status_code not in (200, 204):
            print(f"[Worker] picture_file_name setzen fehlgeschlagen: {ur.status_code} {ur.text}")
            return False
    return True

async def process_job(job: dict) -> bool:
    job_type = job.get("type")
    grocy_id = job.get("grocy_id")
    bildname = job.get("bildname")
    if not grocy_id or not bildname:
        print(f"[Worker] Ungültiger Job: {job}")
        return False
    local_path = os.path.join(DOWNLOAD_DIR, bildname)
    if not os.path.exists(local_path):
        print(f"[Worker] Hole Bild von OneDrive: {bildname}")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(ONEDRIVE_PHOTO_URL, json={"filename": bildname})
            if not r.is_success:
                print(f"[Worker] OneDrive Fehler: {r.status_code} {r.text}")
                return False
            if not r.json().get("success"):
                print(f"[Worker] OneDrive fehlgeschlagen: {r.json().get('error')}")
                return False
        except Exception as e:
            print(f"[Worker] OneDrive Verbindungsfehler: {e}")
            return False
    if not os.path.exists(local_path):
        print(f"[Worker] Datei nicht gefunden: {local_path}")
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

async def run_worker_cycle() -> dict:
    """Runs one pass over all pending jobs. Returns a result summary dict."""
    global _last_cycle_result
    jobs = load_jobs()
    if not jobs:
        result = {"jobs_found": 0, "completed": [], "failed": []}
        _last_cycle_result = result
        return result
    print(f"[Worker] {len(jobs)} Job(s) in Queue")
    completed = []
    failed = []
    for job in jobs:
        job_id = job.get("id", "unknown")
        try:
            success = await process_job(job)
            if success:
                completed.append(job_id)
            else:
                failed.append(job_id)
                print(f"[Worker] Job {job_id} fehlgeschlagen, bleibt in Queue")
        except Exception as e:
            failed.append(job_id)
            print(f"[Worker] Fehler bei Job {job_id}: {e}")
    for job_id in completed:
        remove_job(job_id)
    if completed:
        print(f"[Worker] {len(completed)} Job(s) abgeschlossen")
    result = {"jobs_found": len(jobs), "completed": completed, "failed": failed}
    _last_cycle_result = result
    return result

def _run_cycle_in_thread():
    """Called from the background thread. Acquires lock and runs the async cycle."""
    if not _worker_lock.acquire(blocking=False):
        print("[Worker] Cycle bereits aktiv, überspringe")
        return
    try:
        asyncio.run(run_worker_cycle())
    except Exception as e:
        print(f"[Worker] Fehler im Cycle: {e}")
    finally:
        _worker_lock.release()

def start_background_worker():
    def worker_loop():
        # First run after 60s; then wait for either the interval or a manual trigger
        _worker_trigger.wait(timeout=60)
        while True:
            _worker_trigger.clear()
            _run_cycle_in_thread()
            # Sleep until next hourly tick OR until triggered manually
            _worker_trigger.wait(timeout=WORKER_INTERVAL_SECONDS)
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    print("[Worker] Hintergrund-Worker gestartet (Intervall: 1h, manuell triggerbar)")


# ── MCP tools ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shopping Products",
    instructions=(
        "Tools for Grocy products, recipes, shopping lists and product groups.\n\n"
        "Image queue: queue_recipe_image_job, list_image_jobs, get_image_job, "
        "trigger_image_worker (run worker NOW instead of waiting for hourly cycle).\n\n"
        "Product groups: list_product_groups, create_product_group, "
        "update_product_group, delete_product_group.\n\n"
        "Products: search_products, create_product_simple, update_product, "
        "add_product_barcode, get_next_unnamed_product, "
        "get_next_product_without_barcode, get_next_product_without_picture.\n\n"
        "Shopping: add_to_shopping_list.\n\n"
        "Recipes: create_or_update_recipe, search_recipes, get_recipe_ingredients, "
        "add_recipe_ingredient, update_recipe_ingredient, remove_recipe_ingredient, "
        "add_recipe_to_shopping_list.\n\n"
        "Units: search_quantity_units, create_quantity_unit."
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


@mcp.tool()
async def trigger_image_worker() -> dict:
    """Trigger the image job worker immediately without waiting for the
    next hourly cycle. Useful after queuing a job to process it right away.

    Signals the background thread to wake up and run a full cycle, then
    waits up to 120 seconds for the result. Returns a summary:
    {"triggered": true, "jobs_found", "completed": [...job_ids...],
     "failed": [...job_ids...], "timed_out": false}.

    If the worker is already running (e.g. hourly timer just fired),
    the trigger is queued and will take effect on the next iteration."""
    if not load_jobs():
        return {"triggered": False, "message": "Keine Jobs in Queue - nichts zu tun."}

    # Clear any stale previous result so we can detect the new one
    global _last_cycle_result
    _last_cycle_result = {}

    # Wake the background thread
    _worker_trigger.set()
    print("[Worker] Manuell getriggert")

    # Poll for up to 120s until the cycle completes (result appears)
    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(2)
        result = _last_cycle_result
        if result:  # cycle wrote something
            return {"triggered": True, "timed_out": False, **result}

    return {
        "triggered": True,
        "timed_out": True,
        "message": "Worker läuft noch (>120s) - mit list_image_jobs später prüfen.",
    }


@mcp.tool()
async def queue_recipe_image_job(recipe_id: int, bildname: str) -> dict:
    """Queue an async job: fetch photo from OneDrive and attach to recipe in Grocy.
    Args: recipe_id, bildname (filename only, e.g. 'IMG_20260709_123456.jpg').
    Call trigger_image_worker afterwards to process immediately."""
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
    return {"success": True, "job_id": job["id"],
            "message": f"Job in Queue: '{bildname}' für Rezept {recipe_id}. trigger_image_worker() aufrufen zum sofortigen Start."}


@mcp.tool()
async def list_image_jobs() -> dict:
    """List all pending image jobs in the queue."""
    return {"job_count": len(load_jobs()), "jobs": load_jobs()}


@mcp.tool()
async def get_image_job(job_id: str) -> dict:
    """Read a single image job by job_id. Returns {found: false} if already processed."""
    for job in load_jobs():
        if job.get("id") == job_id:
            return {"found": True, "job": job}
    return {"found": False, "job_id": job_id,
            "note": "Nicht in Queue - vermutlich erfolgreich verarbeitet."}


@mcp.tool()
async def list_product_groups(query: str = "") -> dict:
    """List all Grocy product groups, optionally filtered by name substring."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {"query[]": f"name~{query}"} if query else None
        r = await grocy_get(client, "/objects/product_groups", params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"results": [
            {"group_id": g["id"], "name": g.get("name"), "description": g.get("description")}
            for g in r.json()
        ]}


@mcp.tool()
async def create_product_group(name: str, description: str = "") -> dict:
    """Create a new Grocy product group."""
    async with httpx.AsyncClient(timeout=15) as client:
        body = {"name": name}
        if description:
            body["description"] = description
        r = await grocy_post(client, "/objects/product_groups", body)
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "group_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def update_product_group(group_id: int, name: str, description: str = "") -> dict:
    """Rename or update description of an existing product group."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_put(client, f"/objects/product_groups/{group_id}",
                            json_body={"name": name, "description": description})
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "group_id": group_id, "name": name}


@mcp.tool()
async def delete_product_group(group_id: int) -> dict:
    """Delete a Grocy product group by ID."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{GROCY_BASE}/objects/product_groups/{group_id}")
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "group_id": group_id}


@mcp.tool()
async def get_next_unnamed_product() -> list:
    """Return the next Grocy product that still has its barcode as placeholder name."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return [{"found": False, "error": f"Grocy returned {pr.status_code}"}]
        products = pr.json()
        br = await grocy_get(client, "/objects/product_barcodes")
        barcodes_by_product: dict[int, list[str]] = {}
        if br.status_code == 200:
            for entry in br.json():
                barcodes_by_product.setdefault(entry["product_id"], []).append(entry["barcode"])
        candidates = [p for p in products if p.get("name") and p["name"] in barcodes_by_product.get(p["id"], [])]
        if not candidates:
            return [{"found": False}]
        product = candidates[0]
        info = {"found": True, "product_id": product["id"], "barcode": product["name"]}
        pfn = product.get("picture_file_name")
        if pfn:
            img = await fetch_product_image(client, pfn)
            if img:
                return [info, img]
            info["picture_error"] = "Could not fetch picture"
        else:
            info["picture_error"] = "No picture_file_name set"
        return [info]


@mcp.tool()
async def get_next_product_without_barcode() -> list:
    """Return the next Grocy product that has no barcode linked at all."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return [{"found": False, "error": f"Grocy returned {pr.status_code}"}]
        products = pr.json()
        br = await grocy_get(client, "/objects/product_barcodes")
        ids_with_bc = set()
        if br.status_code == 200:
            ids_with_bc = {e["product_id"] for e in br.json()}
        candidates = [p for p in products if p["id"] not in ids_with_bc]
        if not candidates:
            return [{"found": False}]
        product = candidates[0]
        info = {"found": True, "product_id": product["id"], "name": product.get("name")}
        pfn = product.get("picture_file_name")
        if pfn:
            img = await fetch_product_image(client, pfn)
            if img:
                return [info, img]
        return [info]


@mcp.tool()
async def get_next_product_without_picture() -> dict:
    """Return the next Grocy product that has no picture_file_name set."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return {"found": False, "error": f"Grocy returned {pr.status_code}"}
        candidates = [p for p in pr.json() if not p.get("picture_file_name")]
        if not candidates:
            return {"found": False}
        product = candidates[0]
        barcode = None
        br = await grocy_get(client, "/objects/product_barcodes",
                             params={"query[]": f"product_id={product['id']}"})
        if br.status_code == 200 and br.json():
            barcode = br.json()[0]["barcode"]
        return {"found": True, "product_id": product["id"],
                "name": product.get("name"), "barcode": barcode}


@mcp.tool()
async def update_product(product_id: int, name: str, description: str = "",
                         product_group_id: int | None = None) -> dict:
    """Update a Grocy product's name, description and/or product group."""
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
    """Link a barcode to an existing Grocy product."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/product_barcodes",
                             {"product_id": product_id, "barcode": barcode})
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
        return {"results": [{"product_id": p["id"], "name": p.get("name")} for p in r.json()]}


@mcp.tool()
async def create_product_simple(name: str) -> dict:
    """Create a new Grocy product with just a name."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/products", {
            "name": name, "location_id": LOCATION_ID,
            "qu_id_purchase": QU_PURCHASE, "qu_id_stock": QU_STOCK,
        })
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "product_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def add_to_shopping_list(product_id: int, amount: float = 1, note: str = "") -> dict:
    """Add a product to Grocy's default shopping list."""
    async with httpx.AsyncClient(timeout=15) as client:
        body = {"product_id": product_id, "product_amount": amount}
        if note:
            body["note"] = note
        r = await grocy_post(client, "/stock/shoppinglist/add-product", body)
        if r.status_code != 204:
            try:
                return {"success": False, "error": r.json().get("error_message", r.text)}
            except Exception:
                return {"success": False, "error": r.text}
        return {"success": True, "product_id": product_id, "amount": amount}


@mcp.tool()
async def search_quantity_units(query: str = "") -> dict:
    """Look up Grocy quantity units by name (pass empty to list all)."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {"query[]": f"name~{query}"} if query else None
        r = await grocy_get(client, "/objects/quantity_units", params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"results": [{"qu_id": u["id"], "name": u.get("name")} for u in r.json()]}


@mcp.tool()
async def create_quantity_unit(name: str, name_plural: str = "") -> dict:
    """Create a new Grocy quantity unit."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/quantity_units",
                             {"name": name, "name_plural": name_plural or name})
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "qu_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def create_or_update_recipe(name: str, description: str = "", base_servings: float = 1) -> dict:
    """Create or update a recipe by exact name. Plain text -> HTML auto-convert.
    Afterwards call queue_recipe_image_job + trigger_image_worker."""
    html_desc = text_to_html(description)
    async with httpx.AsyncClient(timeout=15) as client:
        existing = await grocy_get(client, "/objects/recipes", params={"query[]": f"name={name}"})
        if existing.status_code == 200 and existing.json():
            recipe_id = existing.json()[0]["id"]
            ur = await grocy_put(client, f"/objects/recipes/{recipe_id}",
                                 json_body={"description": html_desc, "base_servings": base_servings})
            if ur.status_code not in (200, 204):
                return {"success": False, "error": f"Grocy returned {ur.status_code}: {ur.text}"}
            return {"success": True, "recipe_id": recipe_id, "name": name, "created": False}
        cr = await grocy_post(client, "/objects/recipes",
                              {"name": name, "description": html_desc, "base_servings": base_servings})
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
        return {"results": [{"recipe_id": rec["id"], "name": rec.get("name"),
                             "base_servings": rec.get("base_servings")} for rec in r.json()]}


@mcp.tool()
async def get_recipe_ingredients(recipe_id: int) -> dict:
    """List all ingredients of a recipe with quantity units."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
        if pr.status_code != 200:
            return {"results": [], "error": f"Grocy returned {pr.status_code}: {pr.text}"}
        results = []
        for pos in pr.json():
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
            results.append({"recipe_pos_id": pos["id"], "product_id": pos["product_id"],
                            "product_name": name, "amount": pos["amount"],
                            "qu_id": qu_id, "unit_name": unit_name})
        return {"results": results}


@mcp.tool()
async def add_recipe_ingredient(recipe_id: int, product_id: int, amount: float,
                                qu_id: int | None = None) -> dict:
    """Add one ingredient to a recipe."""
    async with httpx.AsyncClient(timeout=15) as client:
        body = {"recipe_id": recipe_id, "product_id": product_id, "amount": amount}
        if qu_id is not None:
            body["qu_id"] = qu_id
        r = await grocy_post(client, "/objects/recipes_pos", body)
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text}"}
        return {"success": True, "recipe_pos_id": int(r.json()["created_object_id"])}


@mcp.tool()
async def update_recipe_ingredient(recipe_pos_id: int, amount: float,
                                   qu_id: int | None = None) -> dict:
    """Change amount and/or unit of an existing recipe ingredient."""
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
    """Add all recipe ingredients to shopping list, scaled by multiplier."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
        if pr.status_code != 200:
            return {"success": False, "error": f"Grocy returned {pr.status_code}: {pr.text}"}
        positions = pr.json()
        if not positions:
            return {"success": False, "error": "Recipe has no ingredients"}
        added = []
        for pos in positions:
            scaled = pos["amount"] * multiplier
            ar = await grocy_post(client, "/stock/shoppinglist/add-product",
                                  {"product_id": pos["product_id"], "product_amount": scaled})
            if ar.status_code != 204:
                try:
                    err = ar.json().get("error_message", ar.text)
                except Exception:
                    err = ar.text
                return {"success": False,
                        "error": f"Failed on product_id {pos['product_id']}: {err}",
                        "added_so_far": added}
            name = None
            prod = await grocy_get(client, f"/objects/products/{pos['product_id']}")
            if prod.status_code == 200:
                name = prod.json().get("name")
            added.append({"product_id": pos["product_id"], "product_name": name, "amount": scaled})
        return {"success": True, "added": added}


# ── Ingress web UI ─────────────────────────────────────────────────────────

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
            return JSONResponse({"found": True, "name": d["product"]["name"],
                                 "picture_file_name": d["product"].get("picture_file_name"),
                                 "stock_amount": d.get("stock_amount")})
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
        body = {"amount": float(amount),
                "transaction_type": "purchase" if action == "add" else "consume"}
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
                "name": barcode, "location_id": LOCATION_ID,
                "qu_id_purchase": QU_PURCHASE, "qu_id_stock": QU_STOCK,
            })
            if pr.status_code != 200:
                return JSONResponse({"status": "error",
                                     "message": f"Produkt anlegen fehlgeschlagen: {pr.text}"})
            product_id = pr.json()["created_object_id"]
        existing_barcodes = await grocy_get(client, "/objects/product_barcodes",
                                            params={"query[]": f"product_id={product_id}"})
        already_linked = existing_barcodes.status_code == 200 and any(
            b["barcode"] == barcode for b in existing_barcodes.json())
        if not already_linked:
            bcr = await grocy_post(client, "/objects/product_barcodes",
                                   {"product_id": int(product_id), "barcode": barcode})
            if bcr.status_code != 200:
                return JSONResponse({"status": "error",
                                     "message": f"Barcode verknuepfen fehlgeschlagen: {bcr.text}"})
        filename = f"scan_{barcode}.jpg"
        fname_b64 = base64.b64encode(filename.encode()).decode()
        upr = await grocy_put(client, f"/files/productpictures/{fname_b64}", content=photo,
                              headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return JSONResponse({"status": "error",
                                 "message": f"Bild-Upload fehlgeschlagen: {upr.text}"})
        await grocy_put(client, f"/objects/products/{product_id}",
                        json_body={"picture_file_name": filename})
        return JSONResponse({"status": "ok", "product_id": product_id})


# ── App assembly ───────────────────────────────────────────────────────────

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
