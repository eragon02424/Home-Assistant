"""MCP Shopping Products (Einkaufsscanner) fuer Home Assistant.

Kern: Grocy-Zugriff, Werkzeuge fuer Produkte, Rezepte, Einkaufsliste und Produktgruppen,
Bild- und OFF-Lookup-Worker, Ingress-Oberflaeche. Die Pflege-Tags (Rezepte und Produkte)
liegen in grocy_tags.py, der OneDrive-Abruf in onedrive_client.py, die Rezepte-Ansicht in
recipes_ui.py; zusammengesetzt und gestartet wird alles in main.py.
"""

import asyncio
import base64
import html
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import httpx
from onedrive_client import OneDriveClient, OneDriveError
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.requests import Request

GROCY_HOST = os.environ.get("GROCY_HOST", "57f327aa-grocy-linuxserver")
GROCY_BASE = f"http://{GROCY_HOST}/api"
LOCATION_ID = int(os.environ.get("GROCY_LOCATION_ID", "2"))
QU_PURCHASE = int(os.environ.get("GROCY_QU_PURCHASE", "2"))
QU_STOCK = int(os.environ.get("GROCY_QU_STOCK", "2"))

VERSION = "3.18.0"
DATA_DIR = "/data"
IMAGE_JOBS_FILE = f"{DATA_DIR}/image_jobs.json"
LOOKUP_PENDING_FILE = f"{DATA_DIR}/lookup_pending.json"
LOOKUP_DONE_FILE = f"{DATA_DIR}/lookup_done.json"
SEARCH_TERMS_FILE = f"{DATA_DIR}/search_terms.json"
SHOPPING_PENDING_DELETE_FILE = f"{DATA_DIR}/shopping_pending_delete.json"

PENDING_DELETE_HOURS = 2  # Items sit here before being permanently removed from Grocy

OFF_USER_AGENT = "GrocyMCP/3.5 (home-assistant-addon; contact=eragon02424)"
# Rezeptseiten/CDNs blocken oft nicht-browserartige Clients, daher eigener User-Agent für Rezeptbilder
RECIPE_IMAGE_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
RECIPE_IMAGE_MAX_BYTES = 5_000_000
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_EAN_URL = "https://world.openfoodfacts.org/api/v2/product/{ean}.json"

NOTIFY_AFTER_ATTEMPTS = 3  # After this many failures, set needs_user_decision=true
                             # Worker still retries - flag is informational only

_image_trigger = threading.Event()
_lookup_trigger = threading.Event()
_last_image_result: dict = {}

# Token-API des OneDrive SyncServers (Rezeptfotos per Dateiname)
onedrive = OneDriveClient(os.environ.get("ONEDRIVE_API_URL", ""), os.environ.get("ONEDRIVE_TOKEN", ""))


# ── File helpers ─────────────────────────────────────────────────

def _load_json(path: str, default):
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def make_job_id() -> str:
    return f"job_{int(time.time() * 1000)}"


# ── image_jobs ─────────────────────────────────────────────────────

def load_image_jobs() -> list:
    return _load_json(IMAGE_JOBS_FILE, [])

def save_image_jobs(jobs: list):
    _save_json(IMAGE_JOBS_FILE, jobs)

def add_image_job(job: dict):
    """Reiht einen Job ein. Ein bestehender Job fuer dasselbe Objekt (grocy_id + type)
    wird ersetzt, damit eine neue Bildquelle nicht stillschweigend verworfen wird."""
    jobs = [j for j in load_image_jobs()
            if not (j.get("grocy_id") == job.get("grocy_id") and j.get("type") == job.get("type"))]
    job.setdefault("failed_attempts", 0)
    job.setdefault("needs_user_decision", False)
    job.setdefault("last_error", None)
    job.setdefault("source", "off")
    jobs.append(job)
    save_image_jobs(jobs)

def remove_image_job(job_id: str):
    save_image_jobs([j for j in load_image_jobs() if j.get("id") != job_id])


# ── lookup helpers ─────────────────────────────────────────────────

def load_lookup_pending() -> list:
    return _load_json(LOOKUP_PENDING_FILE, [])

def save_lookup_pending(items):
    _save_json(LOOKUP_PENDING_FILE, items)

def add_lookup_pending(product_id: int, grocy_name: str, search_term: str, barcode=None):
    items = load_lookup_pending()
    if any(i["product_id"] == product_id for i in items):
        return
    if any(d["product_id"] == product_id for d in load_lookup_done()):
        return
    items.append({"product_id": product_id, "grocy_name": grocy_name,
                  "search_term": search_term, "barcode": barcode,
                  "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_lookup_pending(items)

def remove_lookup_pending(product_id: int):
    save_lookup_pending([i for i in load_lookup_pending() if i["product_id"] != product_id])

def load_lookup_done() -> list:
    return _load_json(LOOKUP_DONE_FILE, [])

def save_lookup_done(items):
    _save_json(LOOKUP_DONE_FILE, items)

def remove_lookup_done(job_id: str):
    save_lookup_done([i for i in load_lookup_done() if i.get("id") != job_id])

def load_search_terms() -> dict:
    return _load_json(SEARCH_TERMS_FILE, {})

def save_search_terms(terms):
    _save_json(SEARCH_TERMS_FILE, terms)

def set_search_term_local(product_id: int, term: str):
    terms = load_search_terms()
    terms[str(product_id)] = term
    save_search_terms(terms)


# ── shopping list pending-delete helpers ────────────────────────────────

def load_shopping_pending_delete() -> list:
    return _load_json(SHOPPING_PENDING_DELETE_FILE, [])

def save_shopping_pending_delete(items):
    _save_json(SHOPPING_PENDING_DELETE_FILE, items)

def mark_pending_delete(list_item_id: int):
    items = load_shopping_pending_delete()
    if any(i["list_item_id"] == list_item_id for i in items):
        return
    items.append({"list_item_id": list_item_id,
                  "marked_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_shopping_pending_delete(items)

def unmark_pending_delete(list_item_id: int):
    save_shopping_pending_delete(
        [i for i in load_shopping_pending_delete() if i["list_item_id"] != list_item_id]
    )

async def _purge_expired_pending_deletes():
    """Permanently delete from Grocy any shopping list items that have sat in
    the pending-delete list for longer than PENDING_DELETE_HOURS."""
    items = load_shopping_pending_delete()
    if not items:
        return
    now = time.time()
    still_pending = []
    to_delete = []
    for item in items:
        try:
            marked_at = time.mktime(time.strptime(item["marked_at"], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            marked_at = now  # malformed timestamp - keep it pending, don't crash
        age_hours = (now - marked_at) / 3600
        if age_hours >= PENDING_DELETE_HOURS:
            to_delete.append(item["list_item_id"])
        else:
            still_pending.append(item)
    if to_delete:
        async with httpx.AsyncClient(timeout=15) as client:
            for list_item_id in to_delete:
                try:
                    r = await client.delete(f"{GROCY_BASE}/objects/shopping_list/{list_item_id}")
                    if r.status_code in (200, 204):
                        print(f"[ShoppingList] Endgueltig geloescht: {list_item_id}")
                    else:
                        print(f"[ShoppingList] Loeschen fehlgeschlagen {list_item_id}: {r.status_code}")
                        still_pending.append({"list_item_id": list_item_id,
                                              "marked_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                except Exception as e:
                    print(f"[ShoppingList] Fehler beim Loeschen {list_item_id}: {e}")
                    still_pending.append({"list_item_id": list_item_id,
                                          "marked_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_shopping_pending_delete(still_pending)

def start_shopping_purge_worker():
    def loop():
        time.sleep(60)
        while True:
            try:
                asyncio.run(_purge_expired_pending_deletes())
            except Exception as e:
                print(f"[ShoppingList] Purge-Worker Fehler: {e}")
            time.sleep(300)  # check every 5 minutes
    threading.Thread(target=loop, daemon=True).start()
    print("[ShoppingList] Purge-Worker gestartet (Pruefung alle 5min)")


# ── Grocy HTTP helpers ──────────────────────────────────────────────

async def grocy_get(client, path, params=None):
    return await client.get(f"{GROCY_BASE}{path}", params=params)

async def grocy_post(client, path, json_body):
    return await client.post(f"{GROCY_BASE}{path}", json=json_body)

async def grocy_put(client, path, json_body=None, content=None, headers=None):
    return await client.put(f"{GROCY_BASE}{path}", json=json_body, content=content, headers=headers)

def text_to_html(text: str) -> str:
    if not text:
        return text
    return "".join(
        f"<p>{html.escape(para).replace(chr(10), '<br>')}</p>"
        for para in text.split("\n\n")
    )


# ── OpenFoodFacts helpers ──────────────────────────────────────────────

def off_lookup_ean(ean: str) -> list:
    try:
        url = OFF_EAN_URL.format(ean=ean) + "?fields=product_name,brands,image_front_url,quantity,code"
        req = urllib.request.Request(url, headers={"User-Agent": OFF_USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == 1:
            p = data.get("product", {})
            img = p.get("image_front_url", "")
            if img:
                return [{"index": 0, "name": p.get("product_name", ""),
                         "brand": p.get("brands", ""), "quantity": p.get("quantity", ""),
                         "image_url": img, "code": p.get("code", ean)}]
    except Exception as e:
        print(f"[OFF] EAN Fehler {ean}: {e}")
    return []

def off_search_text(search_term: str) -> list:
    try:
        q = urllib.parse.quote(search_term)
        url = f"{OFF_SEARCH_URL}?q={q}&page_size=5&fields=product_name,brands,image_front_url,quantity,code&langs=de"
        req = urllib.request.Request(url, headers={"User-Agent": OFF_USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        results = []
        for i, p in enumerate(data.get("hits", [])):
            img = p.get("image_front_url", "")
            if not img:
                continue
            results.append({"index": i, "name": p.get("product_name", ""),
                            "brand": p.get("brands", ""), "quantity": p.get("quantity", ""),
                            "image_url": img, "code": p.get("code", "")})
        return results
    except Exception as e:
        print(f"[OFF] Text Fehler '{search_term}': {e}")
    return []


# ── Worker: OFF lookup (1/min) ────────────────────────────────────────────────

def _run_off_lookup_tick():
    pending = load_lookup_pending()
    if not pending:
        return
    item = pending[0]
    product_id = item["product_id"]
    grocy_name = item["grocy_name"]
    search_term = item.get("search_term") or grocy_name
    barcode = item.get("barcode")
    print(f"[OFF] Suche {product_id} '{grocy_name}' term='{search_term}'")
    results = off_lookup_ean(barcode) if barcode else []
    if not results:
        results = off_search_text(search_term)
    remove_lookup_pending(product_id)
    if not results:
        print(f"[OFF] Kein Treffer für {product_id}")
        done = load_lookup_done()
        done.append({"id": make_job_id(), "product_id": product_id, "grocy_name": grocy_name,
                     "search_term": search_term, "off_results": [], "decision": None,
                     "auto_accepted": False, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        save_lookup_done(done)
        return
    if len(results) == 1:
        r = results[0]
        print(f"[OFF] Eindeutig {product_id}: '{r['name']}' → image_job")
        add_image_job({"id": make_job_id(), "type": "product_image", "grocy_id": product_id,
                       "image_url": r["image_url"], "source": "off",
                       "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return
    print(f"[OFF] {len(results)} Treffer für {product_id} → lookup_done")
    done = load_lookup_done()
    done.append({"id": make_job_id(), "product_id": product_id, "grocy_name": grocy_name,
                 "search_term": search_term, "off_results": results, "decision": None,
                 "auto_accepted": False, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    save_lookup_done(done)

def start_off_lookup_worker():
    def loop():
        time.sleep(90)
        while True:
            _lookup_trigger.clear()
            try:
                _run_off_lookup_tick()
            except Exception as e:
                print(f"[OFF] Fehler: {e}")
            _lookup_trigger.wait(timeout=60)
    threading.Thread(target=loop, daemon=True).start()
    print("[OFF] Lookup-Worker gestartet (1/min)")


# ── Worker: image download (1/min) ────────────────────────────────────────────────

async def _upload_product_picture(grocy_id: int, image_bytes: bytes, ext: str = "jpg") -> bool:
    filename = f"product_{grocy_id}.{ext}"
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        await client.delete(f"{GROCY_BASE}/files/productpictures/{fname_b64}")
        upr = await grocy_put(client, f"/files/productpictures/{fname_b64}",
                              content=image_bytes, headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return False
        ur = await grocy_put(client, f"/objects/products/{grocy_id}",
                             json_body={"picture_file_name": filename})
        return ur.status_code in (200, 204)

async def _upload_recipe_picture(grocy_id: int, image_bytes: bytes, ext: str = "jpg") -> bool:
    filename = f"recipe_{grocy_id}.{ext}"
    fname_b64 = base64.b64encode(filename.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        await client.delete(f"{GROCY_BASE}/files/recipepictures/{fname_b64}")
        upr = await grocy_put(client, f"/files/recipepictures/{fname_b64}",
                              content=image_bytes, headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return False
        ur = await grocy_put(client, f"/objects/recipes/{grocy_id}",
                             json_body={"picture_file_name": filename})
        return ur.status_code in (200, 204)

async def _process_one_image_job(job: dict) -> tuple[bool, str | None]:
    """Returns (success, error_message)."""
    job_type = job.get("type")
    grocy_id = job.get("grocy_id")
    if job_type == "product_image":
        image_url = job.get("image_url")
        if not image_url:
            return False, "Keine image_url im Job"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(image_url, headers={"User-Agent": OFF_USER_AGENT},
                                     follow_redirects=True)
            if not r.is_success:
                return False, f"HTTP {r.status_code} beim Bilddownload"
            image_bytes = r.content
        except Exception as e:
            return False, f"Download Fehler: {e}"
        ext = image_url.split(".")[-1].split("?")[0].lower()
        if ext not in ("jpg", "jpeg", "png", "webp"):
            ext = "jpg"
        success = await _upload_product_picture(grocy_id, image_bytes, ext)
        if success:
            print(f"[Image] Produktbild gesetzt product_id={grocy_id}")
            return True, None
        return False, "Grocy Upload fehlgeschlagen"
    if job_type == "rezept":
        image_url = job.get("image_url")
        if image_url:
            # Bild-Link: direkt herunterladen (kein OneDrive nötig)
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(image_url, headers={"User-Agent": RECIPE_IMAGE_USER_AGENT},
                                         follow_redirects=True)
                if not r.is_success:
                    return False, f"HTTP {r.status_code} beim Bilddownload"
                if "text/html" in r.headers.get("content-type", "").lower():
                    return False, "Link liefert eine Webseite statt eines Bildes"
                image_bytes = r.content
            except Exception as e:
                return False, f"Download Fehler: {e}"
            if len(image_bytes) > RECIPE_IMAGE_MAX_BYTES:
                return False, f"Bild zu groß ({len(image_bytes)} bytes)"
            ext = image_url.split(".")[-1].split("?")[0].lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg"
            success = await _upload_recipe_picture(grocy_id, image_bytes, ext)
            if success:
                print(f"[Image] Rezeptbild per Link gesetzt recipe_id={grocy_id}")
                return True, None
            return False, "Grocy Upload fehlgeschlagen"
        bildname = job.get("bildname")
        if not bildname:
            return False, "Kein bildname und keine image_url im Job"
        try:
            image_bytes, real_name = await onedrive.fetch(bildname)
        except OneDriveError as e:
            return False, f"OneDrive: {e}"
        except Exception as e:
            return False, f"OneDrive Verbindungsfehler: {type(e).__name__} {e}"
        ext = os.path.splitext(real_name)[1].lstrip(".").lower() or "jpg"
        if await _upload_recipe_picture(grocy_id, image_bytes, ext):
            print(f"[Image] Rezeptbild gesetzt recipe_id={grocy_id} ueber OneDrive (Datei: {real_name})")
            return True, None
        return False, "Grocy Upload fehlgeschlagen"
    return False, f"Unbekannter Job-Typ: {job_type}"


async def _run_image_tick() -> dict:
    """Process image jobs until one succeeds or all jobs in this tick are exhausted.

    All jobs are tried regardless of needs_user_decision.
    needs_user_decision=true is purely informational (tells Claude to notify
    the user), but does NOT prevent retries. The file may have been uploaded
    to OneDrive in the meantime.
    Only dismiss_failed_image_job() actually removes a job.
    """
    global _last_image_result

    all_jobs = load_image_jobs()
    if not all_jobs:
        return {}

    # Snapshot job IDs for this tick to avoid infinite loops
    job_ids_this_tick = [j.get("id") for j in all_jobs]
    result = {}

    for job_id in job_ids_this_tick:
        # Re-read from disk each iteration (previous iteration may have modified)
        current_jobs = load_image_jobs()
        current_job = next((j for j in current_jobs if j.get("id") == job_id), None)
        if current_job is None:
            continue  # Already removed

        attempt_nr = current_job.get("failed_attempts", 0) + 1
        flag = " [needs_user_decision]" if current_job.get("needs_user_decision") else ""
        print(f"[Image] Job {job_id} type={current_job.get('type')} attempt={attempt_nr}{flag}")

        try:
            success, error_msg = await _process_one_image_job(current_job)
        except Exception as e:
            success, error_msg = False, str(e)

        if success:
            remove_image_job(job_id)
            result = {"processed": job_id, "success": True}
            _last_image_result = result
            return result  # Done for this tick

        # Failure: increment counter, set needs_user_decision after threshold
        jobs_updated = load_image_jobs()
        for j in jobs_updated:
            if j.get("id") == job_id:
                j["failed_attempts"] = j.get("failed_attempts", 0) + 1
                j["last_error"] = error_msg
                if j["failed_attempts"] >= NOTIFY_AFTER_ATTEMPTS and not j.get("needs_user_decision"):
                    j["needs_user_decision"] = True
                    print(f"[Image] Job {job_id}: needs_user_decision gesetzt nach {j['failed_attempts']} Versuchen")
                break
        save_image_jobs(jobs_updated)
        result = {"processed": job_id, "success": False, "error": error_msg}
        print(f"[Image] Job {job_id} Fehler ({attempt_nr}): {error_msg} — nächster Job")

    _last_image_result = result
    return result


def start_image_worker():
    def loop():
        time.sleep(60)
        while True:
            _image_trigger.clear()
            try:
                asyncio.run(_run_image_tick())
            except Exception as e:
                print(f"[Image] Fehler: {e}")
            _image_trigger.wait(timeout=60)
    threading.Thread(target=loop, daemon=True).start()
    print("[Image] Image-Worker gestartet (1/min)")


# ── MCP tools ─────────────────────────────────────────────────────

mcp = MCPServer(
    version=VERSION,
    name="MCP Shopping Products",
    instructions=(
        "Tools for Grocy products, recipes, shopping lists, product groups and data maintenance.\n\n"
        "TAGS: recipes and products carry maintenance tags (see get_tag_definitions). A tag only means\n"
        "  'step completed / data present'. Derived tags are set automatically; manual tags via set_tags():\n"
        "  'Eingriff erforderlich' (+ reason) locks an item for the routines until Jonathan acts.\n"
        "  Task lists: list_recipes_by_tags(), list_products_by_tags(), list_interventions(); counts: get_tag_overview().\n\n"
        "PRODUCT FLOW:\n"
        "1. list_products_by_tags(must_not_have=['Stammdaten gepflegt','Eingriff erforderlich']) or\n"
        "   get_products_for_name_review() -> update_product_full() (sets tag 'Stammdaten gepflegt',\n"
        "   which queues the product for the image lookup)\n"
        "2. get_products_for_lookup_decision() -> set_lookup_decision() or skip_lookup_job()\n"
        "3. get_products_pending_lookup() - INFO ONLY\n"
        "MERGE: merge_products(product_id_to_remove, product_id_to_keep); update_product_full()\n"
        "  auto-merges on UNIQUE name conflict.\n\n"
        "STUCK JOBS: list_image_jobs() shows needs_user_decision=true jobs. The worker STILL RETRIES\n"
        "  these every minute. Inform the user of the error and the bildname/URL.\n"
        "  dismiss_failed_image_job(job_id) only removes a job if the user explicitly asks.\n"
        "  A new queue_*_image_job() for the same product/recipe replaces the old job.\n\n"
        "MANUAL PRODUCT IMAGES: if OFF has no good match, find a direct image URL on the web (must end in\n"
        "  .jpg/.jpeg/.png/.webp, not a webpage link) and call queue_product_image_job(product_id, image_url).\n"
        "  The add-on downloads and attaches it; jobs are tagged source='claude_web'.\n\n"
        "RECIPES: get_recipe(), update_recipe(), create_or_update_recipe(), add/update/remove_recipe_ingredient().\n"
        "  Pictures: upload_recipe_picture_base64() for an image the user shares directly,\n"
        "  queue_recipe_image_job(recipe_id, image_url=...) for a web link or bildname=... for a OneDrive photo.\n"
        "  Category: get_recipe_type_options(), then create_or_update_recipe(rezeptart=...).\n\n"
        "CHANGING A PRODUCT UNIT: get_product(product_id) shows the current values (use them for\n"
        "  update_product_full), find_recipes_by_product(product_id) lists every recipe position that\n"
        "  must be adjusted afterwards with update_recipe_ingredient().\n\n"
        "HELPERS: list_locations(), list_product_groups(), search_quantity_units()"
    ),
)

# ── MCP SDK 2.x: Fehlermeldungen von Tools wie in 1.x an das Modell durchreichen ──
# In 2.x sieht das Modell bei normalen Exceptions nur "Error executing tool <name>".
# Dieser Wrapper macht aus jeder Exception einen ToolError mit der Originalmeldung.
import functools as _functools
import inspect as _inspect
from mcp.server.mcpserver.exceptions import ToolError as _ToolError
from mcp.shared.exceptions import MCPError as _MCPError


def _visible_errors(fn):
    if _inspect.iscoroutinefunction(fn):
        @_functools.wraps(fn)
        async def _wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (_ToolError, _MCPError):
                raise
            except Exception as e:
                raise _ToolError(str(e) or type(e).__name__) from e
    else:
        @_functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (_ToolError, _MCPError):
                raise
            except Exception as e:
                raise _ToolError(str(e) or type(e).__name__) from e
    return _wrapper


_mcp_tool_orig = mcp.tool


def _mcp_tool(*args, **kwargs):
    decorator = _mcp_tool_orig(*args, **kwargs)
    return lambda fn: decorator(_visible_errors(fn))


mcp.tool = _mcp_tool


@mcp.tool()
async def merge_products(product_id_to_remove: int, product_id_to_keep: int) -> dict:
    """Merge two duplicate Grocy products.
    Transfers all barcodes, stock and shopping list entries to product_id_to_keep,
    then deletes product_id_to_remove. Also transfers search_term if needed."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{GROCY_BASE}/stock/products/{product_id_to_remove}/merge/{product_id_to_keep}"
        )
        if r.status_code not in (200, 204):
            return {"success": False,
                    "error": f"Grocy merge fehlgeschlagen: {r.status_code} {r.text[:100]}"}
    terms = load_search_terms()
    if str(product_id_to_keep) not in terms and str(product_id_to_remove) in terms:
        terms[str(product_id_to_keep)] = terms[str(product_id_to_remove)]
    terms.pop(str(product_id_to_remove), None)
    save_search_terms(terms)
    print(f"[Merge] {product_id_to_remove} -> {product_id_to_keep} erfolgreich")
    return {"success": True, "merged_into": product_id_to_keep, "removed": product_id_to_remove}


@mcp.tool()
async def upload_recipe_picture_base64(recipe_id: int, image_base64: str, ext: str = "jpg") -> dict:
    """Upload a recipe picture directly via base64, bypassing OneDrive entirely.
    Use this when the OneDrive filename workflow is too cumbersome (e.g. Claude
    was given the image directly by the user). Claude should resize/compress
    the image before base64-encoding to keep upload size reasonable - this
    call is token-cost bound (large base64 strings are slow to send).

    Args:
        recipe_id: Grocy recipe ID to attach the picture to.
        image_base64: Raw base64-encoded image data (no data: URI prefix).
        ext: File extension, default jpg."""
    try:
        image_bytes = base64.b64decode(image_base64)
    except Exception as e:
        return {"success": False, "error": f"Base64 Dekodierung fehlgeschlagen: {e}"}
    if len(image_bytes) > 5_000_000:
        return {"success": False,
                "error": f"Bild zu groß ({len(image_bytes)} bytes) - bitte weiter verkleinern"}
    success = await _upload_recipe_picture(recipe_id, image_bytes, ext)
    if success:
        return {"success": True, "recipe_id": recipe_id, "bytes": len(image_bytes)}
    return {"success": False, "error": "Grocy Upload fehlgeschlagen"}


@mcp.tool()
async def get_products_for_lookup_decision() -> dict:
    """CLAUDE'S TASK: Pick the right OpenFoodFacts result for each product."""
    done = load_lookup_done()
    needs_decision = [j for j in done
                      if j.get("decision") is None and len(j.get("off_results", [])) > 0]
    return {"count": len(needs_decision), "jobs": needs_decision}


@mcp.tool()
async def get_products_pending_lookup() -> dict:
    """INFO ONLY. Pipeline status."""
    pending = load_lookup_pending()
    done = load_lookup_done()
    image_jobs = load_image_jobs()
    no_results = [j for j in done if j.get("decision") is None
                  and len(j.get("off_results", [])) == 0]
    needs_attention = [j for j in image_jobs if j.get("needs_user_decision")]
    product_image_jobs = [j for j in image_jobs if j.get("type") == "product_image"]
    return {
        "queued_for_lookup": len(pending),
        "lookup_pending": [{"product_id": i["product_id"], "grocy_name": i["grocy_name"],
                            "search_term": i["search_term"]} for i in pending],
        "no_off_results_count": len(no_results),
        "no_off_results": [{"job_id": j["id"], "product_id": j["product_id"],
                            "grocy_name": j["grocy_name"]} for j in no_results],
        "image_download_pending": len(product_image_jobs),
        "needs_attention": [{"job_id": j["id"], "type": j["type"], "grocy_id": j["grocy_id"],
                             "bildname": j.get("bildname"), "image_url": j.get("image_url"),
                             "source": j.get("source", "off"),
                             "failed_attempts": j.get("failed_attempts", 0),
                             "last_error": j.get("last_error")} for j in needs_attention],
    }


@mcp.tool()
async def list_lookup_jobs() -> dict:
    """Alias for get_products_for_lookup_decision()."""
    done = load_lookup_done()
    needs_decision = [j for j in done
                      if j.get("decision") is None and len(j.get("off_results", [])) > 0]
    return {"pending_count": len(needs_decision), "jobs": needs_decision,
            "lookup_pending_count": len(load_lookup_pending())}


@mcp.tool()
async def set_search_term(product_id: int, search_term: str) -> dict:
    """Update only the search term for a product (stored locally)."""
    set_search_term_local(product_id, search_term)
    return {"success": True, "product_id": product_id, "search_term": search_term}


@mcp.tool()
async def set_lookup_decision(job_id: str, decision_index: int) -> dict:
    """Write Claude's decision index for an OFF lookup job."""
    done = load_lookup_done()
    for job in done:
        if job.get("id") == job_id:
            results = job.get("off_results", [])
            chosen = next((r for r in results if r.get("index") == decision_index), None)
            if not chosen:
                return {"success": False, "error": f"Index {decision_index} nicht gefunden"}
            job["decision"] = decision_index
            job["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_lookup_done(done)
            add_image_job({"id": make_job_id(), "type": "product_image",
                           "grocy_id": job["product_id"], "image_url": chosen["image_url"],
                           "source": "off",
                           "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            remove_lookup_done(job_id)
            return {"success": True, "job_id": job_id, "product_id": job["product_id"],
                    "chosen": chosen["name"], "image_job_created": True}
    return {"success": False, "error": f"Job {job_id} nicht gefunden"}


@mcp.tool()
async def skip_lookup_job(job_id: str) -> dict:
    """Skip an OFF lookup job (no suitable result)."""
    if not any(j.get("id") == job_id for j in load_lookup_done()):
        return {"success": False, "error": f"Job {job_id} nicht gefunden"}
    remove_lookup_done(job_id)
    return {"success": True, "job_id": job_id}


@mcp.tool()
async def list_image_jobs() -> dict:
    """List all pending image jobs.
    Jobs with needs_user_decision=true have failed 3+ times but are still
    retried every minute. The worker does NOT block on these.
    Each job's "source" field is "off" (automatic OpenFoodFacts pipeline) or
    "claude_web" (Claude queued it manually via queue_product_image_job after
    a web search) so it stays visible where a picture came from.
    Use dismiss_failed_image_job() only if the user explicitly wants to remove a job."""
    jobs = load_image_jobs()
    needs_attention = [j for j in jobs if j.get("needs_user_decision")]
    active = [j for j in jobs if not j.get("needs_user_decision")]
    return {
        "job_count": len(jobs),
        "active_count": len(active),
        "needs_attention_count": len(needs_attention),
        "active_jobs": active,
        "needs_attention_jobs": needs_attention,
    }


@mcp.tool()
async def dismiss_failed_image_job(job_id: str) -> dict:
    """Permanently remove an image job from the queue.
    Only use when the user explicitly asks to remove a job.
    needs_user_decision jobs are still retried automatically - dismissing
    stops all future attempts for this job."""
    jobs = load_image_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        return {"success": False, "error": f"Job {job_id} nicht gefunden"}
    remove_image_job(job_id)
    return {"success": True, "job_id": job_id, "grocy_id": job.get("grocy_id"),
            "last_error": job.get("last_error"), "note": "Job entfernt. Keine weiteren Versuche."}


@mcp.tool()
async def get_image_job(job_id: str) -> dict:
    """Read a single image job by id."""
    for job in load_image_jobs():
        if job.get("id") == job_id:
            return {"found": True, "job": job}
    return {"found": False, "job_id": job_id, "note": "Nicht in Queue."}


@mcp.tool()
async def queue_recipe_image_job(recipe_id: int, bildname: str = "", image_url: str = "") -> dict:
    """Queue a recipe image job. Two sources, give exactly one of them:
    - bildname: filename of a photo in the OneDrive handy upload (as before).
    - image_url: direct link to the image file itself (ends in .jpg/.jpeg/.png/.webp,
      e.g. og:image of the recipe page), NOT a link to a webpage. The add-on image
      worker downloads it later and sets it as the recipe picture; Claude never
      handles image bytes. These jobs are tagged source="claude_web".
    If both are given, image_url wins."""
    if not recipe_id or not (bildname or image_url):
        return {"success": False, "error": "recipe_id und bildname ODER image_url Pflicht"}
    if image_url and not image_url.split("?")[0].lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return {"success": False,
                "error": "image_url sieht nicht wie ein direkter Bildlink aus "
                         "(muss auf .jpg/.jpeg/.png/.webp enden, kein Webseiten-Link)"}
    job = {"id": make_job_id(), "type": "rezept", "grocy_id": recipe_id,
           "source": "claude_web" if image_url else "off",
           "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if bildname:
        job["bildname"] = bildname
    if image_url:
        job["image_url"] = image_url
    add_image_job(job)
    return {"success": True, "job_id": job["id"], "recipe_id": recipe_id, "source": job["source"]}


@mcp.tool()
async def queue_product_image_job(product_id: int, image_url: str) -> dict:
    """Queue a product image job from a direct image URL that Claude found itself
    (e.g. via web_search + web_fetch on a retailer/manufacturer page), for cases
    where OpenFoodFacts has no usable match (raw produce, cleaning products,
    regional brands, etc.).

    image_url MUST be a direct link to the image file itself (ends in
    .jpg/.jpeg/.png/.webp - e.g. an og:image meta tag or an <img src> found by
    fetching a product page), NOT a link to a webpage that merely displays the
    image. Claude only ever passes the URL - the actual download and upload to
    Grocy happens in this add-on's existing image worker, exactly like the
    OpenFoodFacts pipeline. Claude never receives or handles the image bytes.

    The job is tagged source="claude_web" (vs "off" for automatic OFF jobs) so
    list_image_jobs()/get_image_job() always show whether a picture came from
    the automatic pipeline or was manually sourced and confirmed by Claude."""
    if not product_id or not image_url:
        return {"success": False, "error": "product_id und image_url Pflicht"}
    if not image_url.split("?")[0].lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return {"success": False,
                "error": "image_url sieht nicht wie ein direkter Bildlink aus "
                         "(muss auf .jpg/.jpeg/.png/.webp enden, kein Webseiten-Link)"}
    job = {"id": make_job_id(), "type": "product_image", "grocy_id": product_id,
           "image_url": image_url, "source": "claude_web",
           "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    add_image_job(job)
    return {"success": True, "job_id": job["id"], "product_id": product_id, "source": "claude_web"}


@mcp.tool()
async def trigger_image_worker() -> dict:
    """Trigger image worker + OFF lookup worker immediately."""
    global _last_image_result
    _last_image_result = {}
    _image_trigger.set()
    _lookup_trigger.set()
    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(1)
        if _last_image_result:
            return {"triggered": True, "timed_out": False, **_last_image_result}
    return {"triggered": True, "timed_out": True}


@mcp.tool()
async def list_locations() -> dict:
    """List all Grocy storage locations."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/locations")
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}"}
        return {"results": [{"location_id": l["id"], "name": l.get("name"),
                             "description": l.get("description")} for l in r.json()]}


@mcp.tool()
async def list_product_groups(query: str = "") -> dict:
    """List Grocy product groups, optionally filtered by name."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {"query[]": f"name~{query}"} if query else None
        r = await grocy_get(client, "/objects/product_groups", params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}"}
        return {"results": [{"group_id": g["id"], "name": g.get("name"),
                             "description": g.get("description")} for g in r.json()]}


@mcp.tool()
async def create_product_group(name: str, description: str = "") -> dict:
    """Create a new Grocy product group."""
    async with httpx.AsyncClient(timeout=15) as client:
        body = {"name": name}
        if description:
            body["description"] = description
        r = await grocy_post(client, "/objects/product_groups", body)
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "group_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def update_product_group(group_id: int, name: str, description: str = "") -> dict:
    """Rename or update a product group."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_put(client, f"/objects/product_groups/{group_id}",
                            json_body={"name": name, "description": description})
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "group_id": group_id, "name": name}


@mcp.tool()
async def delete_product_group(group_id: int) -> dict:
    """Delete a Grocy product group."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{GROCY_BASE}/objects/product_groups/{group_id}")
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "group_id": group_id}


@mcp.tool()
async def add_product_barcode(product_id: int, barcode: str) -> dict:
    """Link a barcode to a Grocy product."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/product_barcodes",
                             {"product_id": product_id, "barcode": barcode})
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "product_id": product_id, "barcode": barcode}


@mcp.tool()
async def search_products(query: str) -> dict:
    """Substring search for Grocy products by name."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/products", params={"query[]": f"name~{query}"})
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}"}
        return {"results": [{"product_id": p["id"], "name": p.get("name")} for p in r.json()]}


@mcp.tool()
async def get_product(product_id: int) -> dict:
    """Read one product with group, location, stock/purchase unit (id and name),
    best-before days, current stock amount, description and the locally stored
    search_term. Use it before update_product_full() so no value has to be guessed."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, f"/objects/products/{product_id}")
        if r.status_code != 200:
            return {"found": False, "error": f"Grocy returned {r.status_code}"}
        p = r.json()
        units_r = await grocy_get(client, "/objects/quantity_units")
        unit_names = {u["id"]: u.get("name") for u in units_r.json()} if units_r.status_code == 200 else {}
        stock_amount = None
        sr = await grocy_get(client, f"/stock/products/{product_id}")
        if sr.status_code == 200:
            stock_amount = sr.json().get("stock_amount")
        return {
            "found": True, "product_id": p["id"], "name": p.get("name"),
            "description": p.get("description"),
            "product_group_id": p.get("product_group_id"),
            "location_id": p.get("location_id"),
            "qu_id_stock": p.get("qu_id_stock"),
            "qu_stock_name": unit_names.get(p.get("qu_id_stock")),
            "qu_id_purchase": p.get("qu_id_purchase"),
            "qu_purchase_name": unit_names.get(p.get("qu_id_purchase")),
            "default_best_before_days": p.get("default_best_before_days", 0),
            "picture_file_name": p.get("picture_file_name"),
            "stock_amount": stock_amount,
            "search_term": load_search_terms().get(str(product_id)),
        }


@mcp.tool()
async def find_recipes_by_product(product_id: int) -> dict:
    """List all recipe positions that use a product (recipe_id, recipe name,
    recipe_pos_id, amount, unit). Use it to adjust recipes after changing the
    unit of a product (then update_recipe_ingredient on each recipe_pos_id)."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos",
                             params={"query[]": f"product_id={product_id}"})
        if pr.status_code != 200:
            return {"results": [], "error": f"Grocy returned {pr.status_code}"}
        recipes_r = await grocy_get(client, "/objects/recipes")
        recipe_names = {rec["id"]: rec.get("name") for rec in recipes_r.json()} if recipes_r.status_code == 200 else {}
        units_r = await grocy_get(client, "/objects/quantity_units")
        unit_names = {u["id"]: u.get("name") for u in units_r.json()} if units_r.status_code == 200 else {}
        results = [{"recipe_id": pos["recipe_id"], "recipe_name": recipe_names.get(pos["recipe_id"]),
                    "recipe_pos_id": pos["id"], "amount": pos["amount"],
                    "qu_id": pos.get("qu_id"), "unit_name": unit_names.get(pos.get("qu_id"))}
                   for pos in pr.json()]
        return {"count": len(results), "results": results}


@mcp.tool()
async def create_product_simple(name: str) -> dict:
    """Create a new product with just a name."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/products", {
            "name": name, "location_id": LOCATION_ID,
            "qu_id_purchase": QU_PURCHASE, "qu_id_stock": QU_STOCK,
        })
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
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
    """List Grocy quantity units (pass empty for all)."""
    async with httpx.AsyncClient(timeout=15) as client:
        params = {"query[]": f"name~{query}"} if query else None
        r = await grocy_get(client, "/objects/quantity_units", params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}"}
        return {"results": [{"qu_id": u["id"], "name": u.get("name")} for u in r.json()]}


@mcp.tool()
async def create_quantity_unit(name: str, name_plural: str = "") -> dict:
    """Create a new quantity unit."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_post(client, "/objects/quantity_units",
                             {"name": name, "name_plural": name_plural or name})
        if r.status_code != 200:
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "qu_id": int(r.json()["created_object_id"]), "name": name}


@mcp.tool()
async def get_recipe_type_options() -> dict:
    """List the allowed values for the recipe 'Rezeptart' userfield (preset-list on entity=recipes).
    Use this to see which categories exist before setting rezeptart on a recipe."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/userfields", params={"query[]": "name=rezeptart"})
        if r.status_code != 200 or not r.json():
            return {"options": [], "error": f"Grocy returned {r.status_code}"}
        field = next((f for f in r.json() if f.get("entity") == "recipes"), None)
        if not field:
            return {"options": [], "error": "userfield 'rezeptart' on entity 'recipes' not found"}
        config = field.get("config") or ""
        options = [line for line in config.split("\n") if line.strip()]
        return {"options": options}


@mcp.tool()
async def create_or_update_recipe(name: str, description: str = "", base_servings: float = 1,
                                   rezeptart: str = "") -> dict:
    """Create or update a recipe. Optionally set 'rezeptart' (one value from
    get_recipe_type_options) to categorize the recipe, e.g. 'Auflauf', 'Kuchen', 'Suppen'."""
    html_desc = text_to_html(description)
    async with httpx.AsyncClient(timeout=15) as client:
        existing = await grocy_get(client, "/objects/recipes", params={"query[]": f"name={name}"})
        if existing.status_code == 200 and existing.json():
            recipe_id = existing.json()[0]["id"]
            ur = await grocy_put(client, f"/objects/recipes/{recipe_id}",
                                 json_body={"description": html_desc, "base_servings": base_servings})
            if ur.status_code not in (200, 204):
                return {"success": False, "error": f"Grocy returned {ur.status_code}"}
            if rezeptart:
                uf = await grocy_put(client, f"/userfields/recipes/{recipe_id}",
                                     json_body={"rezeptart": rezeptart})
                if uf.status_code not in (200, 204):
                    return {"success": True, "recipe_id": recipe_id, "name": name, "created": False,
                            "userfield_warning": f"Grocy returned {uf.status_code} setting rezeptart"}
            return {"success": True, "recipe_id": recipe_id, "name": name, "created": False}
        cr = await grocy_post(client, "/objects/recipes",
                              {"name": name, "description": html_desc, "base_servings": base_servings})
        if cr.status_code != 200:
            return {"success": False, "error": f"Grocy returned {cr.status_code}"}
        recipe_id = int(cr.json()["created_object_id"])
        if rezeptart:
            uf = await grocy_put(client, f"/userfields/recipes/{recipe_id}",
                                 json_body={"rezeptart": rezeptart})
            if uf.status_code not in (200, 204):
                return {"success": True, "recipe_id": recipe_id, "name": name, "created": True,
                        "userfield_warning": f"Grocy returned {uf.status_code} setting rezeptart"}
        return {"success": True, "recipe_id": recipe_id, "name": name, "created": True}


@mcp.tool()
async def search_recipes(query: str) -> dict:
    """Substring search for Grocy recipes by name."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_get(client, "/objects/recipes", params={"query[]": f"name~{query}"})
        if r.status_code != 200:
            return {"results": [], "error": f"Grocy returned {r.status_code}"}
        return {"results": [{"recipe_id": rec["id"], "name": rec.get("name"),
                             "base_servings": rec.get("base_servings")} for rec in r.json()]}


@mcp.tool()
async def get_recipe_ingredients(recipe_id: int) -> dict:
    """List all ingredients of a recipe with quantity units."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
        if pr.status_code != 200:
            return {"results": [], "error": f"Grocy returned {pr.status_code}"}
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
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "recipe_pos_id": int(r.json()["created_object_id"])}


@mcp.tool()
async def update_recipe_ingredient(recipe_pos_id: int, amount: float,
                                   qu_id: int | None = None) -> dict:
    """Change amount and/or unit of a recipe ingredient."""
    body = {"amount": amount}
    if qu_id is not None:
        body["qu_id"] = qu_id
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_put(client, f"/objects/recipes_pos/{recipe_pos_id}", json_body=body)
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "recipe_pos_id": recipe_pos_id, "amount": amount}


@mcp.tool()
async def remove_recipe_ingredient(recipe_pos_id: int) -> dict:
    """Remove an ingredient from a recipe."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{GROCY_BASE}/objects/recipes_pos/{recipe_pos_id}")
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"Grocy returned {r.status_code}"}
        return {"success": True, "recipe_pos_id": recipe_pos_id}


@mcp.tool()
async def add_recipe_to_shopping_list(recipe_id: int, multiplier: float = 1) -> dict:
    """Add all recipe ingredients to shopping list, scaled by multiplier."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
        if pr.status_code != 200:
            return {"success": False, "error": f"Grocy returned {pr.status_code}"}
        positions = pr.json()
        if not positions:
            return {"success": False, "error": "No ingredients"}
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
                return {"success": False, "error": f"{pos['product_id']}: {err}",
                        "added_so_far": added}
            name = None
            prod = await grocy_get(client, f"/objects/products/{pos['product_id']}")
            if prod.status_code == 200:
                name = prod.json().get("name")
            added.append({"product_id": pos["product_id"], "product_name": name, "amount": scaled})
        return {"success": True, "added": added}


# ── Ingress web UI ──────────────────────────────────────────────────

async def index(request: Request):
    with open("/static/index.html") as f:
        return HTMLResponse(f.read())

async def api_check_barcode(request: Request):
    data = await request.json()
    barcode = data.get("barcode")
    if not barcode:
        return JSONResponse({"found": False, "error": "Kein Barcode"})
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
                                     "message": f"Anlegen fehlgeschlagen: {pr.text}"})
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
                                     "message": f"Barcode fehlgeschlagen: {bcr.text}"})
        filename = f"scan_{barcode}.jpg"
        fname_b64 = base64.b64encode(filename.encode()).decode()
        upr = await grocy_put(client, f"/files/productpictures/{fname_b64}", content=photo,
                              headers={"Content-Type": "application/octet-stream"})
        if upr.status_code not in (200, 204):
            return JSONResponse({"status": "error", "message": f"Bild fehlgeschlagen: {upr.text}"})
        await grocy_put(client, f"/objects/products/{product_id}",
                        json_body={"picture_file_name": filename})
        return JSONResponse({"status": "ok", "product_id": product_id})


async def api_shopping_list(request: Request):
    """Return the shopping list grouped by product category (like Grocy's own grouping).
    Excludes items currently marked pending-delete (shown separately). Each group has
    a name and a list of items with list_item_id, product_id, name, amount, unit,
    picture_file_name. Also returns pending_delete: items swiped away, waiting to be
    permanently removed after PENDING_DELETE_HOURS, each with hours_left."""
    await _purge_expired_pending_deletes()  # opportunistic purge on every load
    pending_ids = {i["list_item_id"] for i in load_shopping_pending_delete()}
    pending_meta = {i["list_item_id"]: i["marked_at"] for i in load_shopping_pending_delete()}

    async with httpx.AsyncClient(timeout=15) as client:
        sl = await grocy_get(client, "/objects/shopping_list")
        if sl.status_code != 200:
            return JSONResponse({"groups": [], "pending_delete": [],
                                 "error": f"Grocy returned {sl.status_code}"})
        entries = sl.json()

        products_r = await grocy_get(client, "/objects/products")
        products_by_id = {p["id"]: p for p in products_r.json()} if products_r.status_code == 200 else {}

        groups_r = await grocy_get(client, "/objects/product_groups")
        group_names = {g["id"]: g.get("name", "Sonstiges") for g in groups_r.json()} if groups_r.status_code == 200 else {}

        units_r = await grocy_get(client, "/objects/quantity_units")
        unit_names = {u["id"]: u.get("name", "") for u in units_r.json()} if units_r.status_code == 200 else {}

    grouped: dict = {}
    pending_items = []
    ungrouped_label = "Sonstiges"
    for entry in entries:
        list_item_id = entry["id"]
        pid = entry.get("product_id")
        product = products_by_id.get(pid, {})
        qu_id = product.get("qu_id_purchase")
        item = {
            "list_item_id": list_item_id,
            "product_id": pid,
            "name": product.get("name", entry.get("note") or "Unbekanntes Produkt"),
            "amount": entry.get("amount", 1),
            "unit": unit_names.get(qu_id, ""),
            "picture_file_name": product.get("picture_file_name"),
            "note": entry.get("note", ""),
        }
        if list_item_id in pending_ids:
            marked_at_str = pending_meta.get(list_item_id)
            try:
                marked_at = time.mktime(time.strptime(marked_at_str, "%Y-%m-%dT%H:%M:%S"))
                hours_left = max(0, PENDING_DELETE_HOURS - (time.time() - marked_at) / 3600)
            except Exception:
                hours_left = PENDING_DELETE_HOURS
            item["hours_left"] = round(hours_left, 1)
            pending_items.append(item)
            continue
        group_id = product.get("product_group_id")
        group_name = group_names.get(group_id, ungrouped_label) if group_id else ungrouped_label
        grouped.setdefault(group_name, []).append(item)

    group_names_sorted = sorted(k for k in grouped if k != ungrouped_label)
    if ungrouped_label in grouped:
        group_names_sorted.append(ungrouped_label)

    result_groups = []
    for gname in group_names_sorted:
        items = sorted(grouped[gname], key=lambda i: i["name"].lower())
        result_groups.append({"category": gname, "items": items})

    pending_items.sort(key=lambda i: i["name"].lower())

    return JSONResponse({
        "groups": result_groups,
        "pending_delete": pending_items,
        "total_items": len(entries) - len(pending_items),
    })


async def api_shopping_list_remove(request: Request):
    """Mark a shopping list item as pending-delete (soft delete).
    The item is NOT removed from Grocy yet - it moves to the 'bald löschen' list
    and gets permanently deleted after PENDING_DELETE_HOURS unless restored."""
    data = await request.json()
    list_item_id = data.get("list_item_id")
    if not list_item_id:
        return JSONResponse({"status": "error", "message": "list_item_id fehlt"})
    mark_pending_delete(int(list_item_id))
    return JSONResponse({"status": "ok", "list_item_id": list_item_id})


async def api_shopping_list_restore(request: Request):
    """Restore a shopping list item from the pending-delete list back to the
    normal shopping list (undo a swipe)."""
    data = await request.json()
    list_item_id = data.get("list_item_id")
    if not list_item_id:
        return JSONResponse({"status": "error", "message": "list_item_id fehlt"})
    unmark_pending_delete(int(list_item_id))
    return JSONResponse({"status": "ok", "list_item_id": list_item_id})


# ── App assembly ─────────────────────────────────────────────────

# DNS-Rebinding-Schutz aus, damit Anfragen über LAN-IP/Proxy akzeptiert werden
mcp_app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)

app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/check-barcode", api_check_barcode, methods=["POST"]),
        Route("/api/book", api_book, methods=["POST"]),
        Route("/api/create-unknown", api_create_unknown, methods=["POST"]),
        Route("/api/product-picture/{filename}", api_product_picture, methods=["GET"]),
        Route("/api/shopping-list", api_shopping_list, methods=["GET"]),
        Route("/api/shopping-list/remove", api_shopping_list_remove, methods=["POST"]),
        Route("/api/shopping-list/restore", api_shopping_list_restore, methods=["POST"]),
    ] + mcp_app.routes,
    lifespan=mcp_app.router.lifespan_context,
)
