"""MCP Shopping Products for Home Assistant v3.8.0

v3.8.0 changes:
- Shopping list swipe-to-delete is now a SOFT delete. Swiping an item moves
  it to a "bald löschen" (pending-delete) list at the bottom instead of
  deleting it from Grocy immediately.
- Items sit in pending-delete for PENDING_DELETE_HOURS (2h). A background
  worker checks every 5min and permanently deletes expired items from Grocy.
- Swiping an item right within the pending-delete section restores it to
  the normal shopping list (undo).
- New endpoint POST /api/shopping-list/restore.
- api_shopping_list now returns "pending_delete" array with hours_left per item.

v3.7.0: shopping list tab, grouped by category, hard delete on swipe
v3.6.0: upload_recipe_picture_base64 tool (bypasses OneDrive for recipe pics)
v3.5.0: needs_user_decision is informational only, worker always retries

v3.4.0: image worker iterates through all active jobs until one succeeds
v3.3.0: merge_products tool, auto-merge on UNIQUE name conflict
v3.2.0: failed image job handling (needs_user_decision after 3 attempts)
v3.1.0: three separate review tools, auto-queue claude_reviewed gate
v3.0.0: three per-minute workers
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
DATA_DIR = "/data"
IMAGE_JOBS_FILE = f"{DATA_DIR}/image_jobs.json"
LOOKUP_PENDING_FILE = f"{DATA_DIR}/lookup_pending.json"
LOOKUP_DONE_FILE = f"{DATA_DIR}/lookup_done.json"
SEARCH_TERMS_FILE = f"{DATA_DIR}/search_terms.json"
SHOPPING_PENDING_DELETE_FILE = f"{DATA_DIR}/shopping_pending_delete.json"
DOWNLOAD_DIR = "/share/onedrive_downloads"

PENDING_DELETE_HOURS = 2  # Items sit here before being permanently removed from Grocy

OFF_USER_AGENT = "GrocyMCP/3.5 (home-assistant-addon; contact=eragon02424)"
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_EAN_URL = "https://world.openfoodfacts.org/api/v2/product/{ean}.json"

CLAUDE_REVIEWED_MARKER = "claude_reviewed:"
NOTIFY_AFTER_ATTEMPTS = 3  # After this many failures, set needs_user_decision=true
                             # Worker still retries - flag is informational only

_image_trigger = threading.Event()
_lookup_trigger = threading.Event()
_last_image_result: dict = {}


# ── File helpers ─────────────────────────────────────────────

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


# ── image_jobs ─────────────────────────────────────────────────

def load_image_jobs() -> list:
    return _load_json(IMAGE_JOBS_FILE, [])

def save_image_jobs(jobs: list):
    _save_json(IMAGE_JOBS_FILE, jobs)

def add_image_job(job: dict):
    jobs = load_image_jobs()
    for existing in jobs:
        if existing.get("grocy_id") == job.get("grocy_id") and existing.get("type") == job.get("type"):
            return
    job.setdefault("failed_attempts", 0)
    job.setdefault("needs_user_decision", False)
    job.setdefault("last_error", None)
    jobs.append(job)
    save_image_jobs(jobs)

def remove_image_job(job_id: str):
    save_image_jobs([j for j in load_image_jobs() if j.get("id") != job_id])


# ── lookup helpers ────────────────────────────────────────────

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


# ── shopping list pending-delete helpers ────────────────────────

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

def is_pending_delete(list_item_id: int) -> bool:
    return any(i["list_item_id"] == list_item_id for i in load_shopping_pending_delete())


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


# ── Grocy HTTP helpers ─────────────────────────────────────────

async def grocy_get(client, path, params=None):
    return await client.get(f"{GROCY_BASE}{path}", params=params)

async def grocy_post(client, path, json_body):
    return await client.post(f"{GROCY_BASE}{path}", json=json_body)

async def grocy_put(client, path, json_body=None, content=None, headers=None):
    return await client.put(f"{GROCY_BASE}{path}", json=json_body, content=content, headers=headers)

async def fetch_product_image_mcp(client, picture_file_name):
    fname_b64 = base64.b64encode(picture_file_name.encode()).decode()
    r = await grocy_get(client, f"/files/productpictures/{fname_b64}")
    if r.status_code == 200:
        return MCPImage(data=r.content, format="jpeg")
    return None

def text_to_html(text: str) -> str:
    if not text:
        return text
    return "".join(
        f"<p>{html.escape(para).replace(chr(10), '<br>')}</p>"
        for para in text.split("\n\n")
    )


# ── OpenFoodFacts helpers ──────────────────────────────────────

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


# ── Worker: OFF lookup (1/min) ────────────────────────────────────

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
                       "image_url": r["image_url"], "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
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


# ── Worker: image download (1/min) ────────────────────────────────

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
        bildname = job.get("bildname")
        if not bildname:
            return False, "Kein bildname im Job"
        local_path = os.path.join(DOWNLOAD_DIR, bildname)
        if not os.path.exists(local_path):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    r = await client.post(ONEDRIVE_PHOTO_URL, json={"filename": bildname})
                if not r.is_success or not r.json().get("success"):
                    err = r.json().get("error", r.text[:80]) if r.is_success else r.text[:80]
                    return False, f"OneDrive: {err}"
            except Exception as e:
                return False, f"OneDrive Verbindungsfehler: {e}"
        if not os.path.exists(local_path):
            return False, f"Datei nicht gefunden: {bildname}"
        ext = os.path.splitext(bildname)[1].lstrip(".").lower() or "jpg"
        with open(local_path, "rb") as f:
            image_bytes = f.read()
        success = await _upload_recipe_picture(grocy_id, image_bytes, ext)
        if success:
            print(f"[Image] Rezeptbild gesetzt recipe_id={grocy_id}")
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


# ── Auto-queue worker (every 5min) ──────────────────────────────

async def _auto_queue_products_for_lookup():
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            pr = await grocy_get(client, "/objects/products")
            if pr.status_code != 200:
                return
            products = pr.json()
            br = await grocy_get(client, "/objects/product_barcodes")
            barcodes_by_product = {}
            if br.status_code == 200:
                for entry in br.json():
                    barcodes_by_product.setdefault(entry["product_id"], []).append(entry["barcode"])
        pending_ids = {i["product_id"] for i in load_lookup_pending()}
        done_ids = {i["product_id"] for i in load_lookup_done()}
        image_job_ids = {j["grocy_id"] for j in load_image_jobs() if j.get("type") == "product_image"}
        search_terms = load_search_terms()
        added = 0
        for p in products:
            pid = p["id"]
            if p.get("picture_file_name"):
                continue
            if pid in pending_ids or pid in done_ids or pid in image_job_ids:
                continue
            if CLAUDE_REVIEWED_MARKER not in (p.get("description") or ""):
                continue
            st = search_terms.get(str(pid))
            if not st:
                continue
            barcodes = barcodes_by_product.get(pid, [])
            add_lookup_pending(pid, p.get("name", ""), st, barcodes[0] if barcodes else None)
            added += 1
        if added:
            print(f"[AutoQ] {added} Produkt(e) in lookup_pending")
    except Exception as e:
        print(f"[AutoQ] Fehler: {e}")

def start_auto_queue_worker():
    def loop():
        time.sleep(120)
        while True:
            try:
                asyncio.run(_auto_queue_products_for_lookup())
            except Exception as e:
                print(f"[AutoQ] Fehler: {e}")
            time.sleep(300)
    threading.Thread(target=loop, daemon=True).start()
    print("[AutoQ] Auto-Queue-Worker gestartet (alle 5min)")


# ── MCP tools ───────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shopping Products",
    instructions=(
        "Tools for Grocy products, recipes, shopping lists, product groups and data maintenance.\n\n"
        "THREE REVIEW TOOLS:\n"
        "1. get_products_for_name_review() -> update_product_full()\n"
        "2. get_products_for_lookup_decision() -> set_lookup_decision() or skip_lookup_job()\n"
        "3. get_products_pending_lookup() - INFO ONLY\n\n"
        "MERGE: merge_products(product_id_to_remove, product_id_to_keep)\n"
        "  - update_product_full() auto-merges on UNIQUE name conflict.\n\n"
        "STUCK JOBS: list_image_jobs() shows needs_user_decision=true jobs.\n"
        "  The worker STILL RETRIES these every minute (file may appear on OneDrive).\n"
        "  When you see needs_user_decision=true: inform the user of the error and\n"
        "  the bildname, so they can upload the file to OneDrive.\n"
        "  dismiss_failed_image_job(job_id) only removes a job if user explicitly asks.\n\n"
        "RECIPE PICTURES: prefer upload_recipe_picture_base64() when the user shares\n"
        "  an image directly with Claude - bypasses OneDrive entirely.\n\n"
        "HELPERS: list_locations(), list_product_groups(), search_quantity_units()"
    ),
)
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


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
async def get_products_for_name_review() -> dict:
    """CLAUDE'S TASK: Set name, group, location, units, MHD and search_term."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return {"count": 0, "results": [], "error": f"Grocy returned {pr.status_code}"}
        products = pr.json()
        br = await grocy_get(client, "/objects/product_barcodes")
        barcodes_by_product = {}
        if br.status_code == 200:
            for entry in br.json():
                barcodes_by_product.setdefault(entry["product_id"], []).append(entry["barcode"])
    search_terms = load_search_terms()
    results = []
    for p in products:
        desc = p.get("description") or ""
        if CLAUDE_REVIEWED_MARKER in desc:
            continue
        pid = p["id"]
        results.append({
            "product_id": pid, "name": p.get("name"), "description": desc,
            "product_group_id": p.get("product_group_id"),
            "location_id": p.get("location_id"),
            "qu_id_stock": p.get("qu_id_stock"),
            "qu_id_purchase": p.get("qu_id_purchase"),
            "default_best_before_days": p.get("default_best_before_days", 0),
            "picture_file_name": p.get("picture_file_name"),
            "barcodes": barcodes_by_product.get(pid, []),
            "search_term": search_terms.get(str(pid)),
        })
    return {"count": len(results), "results": results}


@mcp.tool()
async def update_product_full(
    product_id: int,
    name: str,
    search_term: str,
    description: str = "",
    product_group_id: int | None = None,
    location_id: int | None = None,
    qu_id_stock: int | None = None,
    qu_id_purchase: int | None = None,
    default_best_before_days: int = 0,
    reviewed: bool = True,
) -> dict:
    """Full product update. AUTO-MERGE on UNIQUE name conflict.
    Locations: 2=Kühlschrank, 3=Vorratsschrank, 4=Gefrierschrank, 5=Lagerschrank"""
    if search_term:
        set_search_term_local(product_id, search_term)
    final_desc = description or ""
    if reviewed:
        marker = f"{CLAUDE_REVIEWED_MARKER}{time.strftime('%Y-%m-%d')}"
        final_desc = f"{final_desc}\n{marker}" if final_desc else marker
    body = {"name": name, "description": final_desc,
            "default_best_before_days": default_best_before_days}
    if product_group_id is not None:
        body["product_group_id"] = product_group_id
    if location_id is not None:
        body["location_id"] = location_id
    if qu_id_stock is not None:
        body["qu_id_stock"] = qu_id_stock
    if qu_id_purchase is not None:
        body["qu_id_purchase"] = qu_id_purchase
    async with httpx.AsyncClient(timeout=15) as client:
        r = await grocy_put(client, f"/objects/products/{product_id}", json_body=body)
        if r.status_code in (200, 204):
            return {"success": True, "product_id": product_id, "name": name,
                    "search_term": search_term, "reviewed": reviewed}
        if r.status_code == 400 and "UNIQUE constraint" in r.text:
            existing_r = await grocy_get(client, "/objects/products",
                                         params={"query[]": f"name={name}"})
            if existing_r.status_code == 200 and existing_r.json():
                existing_id = existing_r.json()[0]["id"]
                if existing_id != product_id:
                    merge_r = await client.post(
                        f"{GROCY_BASE}/stock/products/{product_id}/merge/{existing_id}"
                    )
                    if merge_r.status_code in (200, 204):
                        terms = load_search_terms()
                        if str(existing_id) not in terms and search_term:
                            terms[str(existing_id)] = search_term
                        terms.pop(str(product_id), None)
                        save_search_terms(terms)
                        print(f"[Merge] Auto-merge {product_id} → {existing_id} ('{name}')")
                        return {"success": True, "merged": True, "product_id": existing_id,
                                "removed_id": product_id, "name": name, "search_term": search_term,
                                "note": f"Duplikat {product_id} in {existing_id} zusammengeführt."}
                    return {"success": False,
                            "error": f"Auto-merge fehlgeschlagen: {merge_r.status_code}"}
        return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text[:100]}"}


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
                             "bildname": j.get("bildname"),
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
async def queue_recipe_image_job(recipe_id: int, bildname: str) -> dict:
    """Queue a recipe image job (OneDrive source)."""
    if not bildname or not recipe_id:
        return {"success": False, "error": "recipe_id und bildname Pflicht"}
    job = {"id": make_job_id(), "type": "rezept", "grocy_id": recipe_id,
           "bildname": bildname, "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    add_image_job(job)
    return {"success": True, "job_id": job["id"]}


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
async def get_next_unnamed_product() -> list:
    """Return the next product still using its barcode as placeholder name."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return [{"found": False}]
        products = pr.json()
        br = await grocy_get(client, "/objects/product_barcodes")
        barcodes_by_product = {}
        if br.status_code == 200:
            for entry in br.json():
                barcodes_by_product.setdefault(entry["product_id"], []).append(entry["barcode"])
        candidates = [p for p in products if p.get("name") and
                      p["name"] in barcodes_by_product.get(p["id"], [])]
        if not candidates:
            return [{"found": False}]
        product = candidates[0]
        info = {"found": True, "product_id": product["id"], "barcode": product["name"]}
        pfn = product.get("picture_file_name")
        if pfn:
            img = await fetch_product_image_mcp(client, pfn)
            if img:
                return [info, img]
            info["picture_error"] = "Could not fetch"
        else:
            info["picture_error"] = "No picture"
        return [info]


@mcp.tool()
async def get_next_product_without_barcode() -> list:
    """Return the next product with no barcode linked."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return [{"found": False}]
        products = pr.json()
        br = await grocy_get(client, "/objects/product_barcodes")
        ids_with_bc = {e["product_id"] for e in br.json()} if br.status_code == 200 else set()
        candidates = [p for p in products if p["id"] not in ids_with_bc]
        if not candidates:
            return [{"found": False}]
        product = candidates[0]
        info = {"found": True, "product_id": product["id"], "name": product.get("name")}
        pfn = product.get("picture_file_name")
        if pfn:
            img = await fetch_product_image_mcp(client, pfn)
            if img:
                return [info, img]
        return [info]


@mcp.tool()
async def get_next_product_without_picture() -> dict:
    """Return the next product with no picture_file_name set."""
    async with httpx.AsyncClient(timeout=15) as client:
        pr = await grocy_get(client, "/objects/products")
        if pr.status_code != 200:
            return {"found": False}
        candidates = [p for p in pr.json() if not p.get("picture_file_name")]
        if not candidates:
            return {"found": False}
        product = candidates[0]
        br = await grocy_get(client, "/objects/product_barcodes",
                             params={"query[]": f"product_id={product['id']}"})
        barcode = br.json()[0]["barcode"] if br.status_code == 200 and br.json() else None
        return {"found": True, "product_id": product["id"],
                "name": product.get("name"), "barcode": barcode}


@mcp.tool()
async def update_product(product_id: int, name: str, description: str = "",
                         product_group_id: int | None = None) -> dict:
    """Quick name/description/group update."""
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
async def create_or_update_recipe(name: str, description: str = "", base_servings: float = 1) -> dict:
    """Create or update a recipe."""
    html_desc = text_to_html(description)
    async with httpx.AsyncClient(timeout=15) as client:
        existing = await grocy_get(client, "/objects/recipes", params={"query[]": f"name={name}"})
        if existing.status_code == 200 and existing.json():
            recipe_id = existing.json()[0]["id"]
            ur = await grocy_put(client, f"/objects/recipes/{recipe_id}",
                                 json_body={"description": html_desc, "base_servings": base_servings})
            if ur.status_code not in (200, 204):
                return {"success": False, "error": f"Grocy returned {ur.status_code}"}
            return {"success": True, "recipe_id": recipe_id, "name": name, "created": False}
        cr = await grocy_post(client, "/objects/recipes",
                              {"name": name, "description": html_desc, "base_servings": base_servings})
        if cr.status_code != 200:
            return {"success": False, "error": f"Grocy returned {cr.status_code}"}
        return {"success": True, "recipe_id": int(cr.json()["created_object_id"]),
                "name": name, "created": True}


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


# ── Ingress web UI ─────────────────────────────────────────

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


# ── App assembly ─────────────────────────────────────────

mcp_app = mcp.streamable_http_app()

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

if __name__ == "__main__":
    start_image_worker()
    start_off_lookup_worker()
    start_auto_queue_worker()
    start_shopping_purge_worker()
    uvicorn.run(app, host="0.0.0.0", port=8770)
