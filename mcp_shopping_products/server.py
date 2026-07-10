"""MCP Shopping Products for Home Assistant v3.4.0

v3.4.0 changes:
- Image worker tick now iterates through ALL active jobs per minute until
  one succeeds. Failed jobs (not yet stuck) are skipped and their counter
  incremented, then the next job is tried. This guarantees at least one
  successful download per minute as long as any active job exists.
  Previously the tick stopped after the first job regardless of outcome.

v3.3.0: merge_products tool, update_product_full auto-merge on name conflict
v3.2.0: failed image job handling (needs_user_decision after 3 attempts)
v3.1.0: three separate review tools, auto-queue claude_reviewed gate
v3.0.0: three per-minute workers, OFF lookup, image download, auto-queue
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
DOWNLOAD_DIR = "/share/onedrive_downloads"

OFF_USER_AGENT = "GrocyMCP/3.4 (home-assistant-addon; contact=eragon02424)"
OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_EAN_URL = "https://world.openfoodfacts.org/api/v2/product/{ean}.json"

CLAUDE_REVIEWED_MARKER = "claude_reviewed:"
MAX_IMAGE_ATTEMPTS = 3

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
    """Process image jobs until one succeeds or all active jobs are exhausted.

    Per tick:
    - Stuck jobs (needs_user_decision=true) are always skipped.
    - Failed jobs (not yet stuck) are tried: on failure their counter is
      incremented (and marked stuck if >= MAX_IMAGE_ATTEMPTS), then the
      next job is tried immediately in the same tick.
    - The tick ends as soon as one job succeeds OR every active job has
      been attempted once.
    This guarantees at least one successful download per minute.
    """
    global _last_image_result

    # Snapshot active job IDs for this tick (to avoid infinite loops if
    # new jobs get added mid-tick)
    jobs_snapshot = [j for j in load_image_jobs() if not j.get("needs_user_decision", False)]
    if not jobs_snapshot:
        stuck_count = sum(1 for j in load_image_jobs() if j.get("needs_user_decision"))
        if stuck_count:
            print(f"[Image] {stuck_count} Job(s) blockiert, nichts zu tun")
        return {}

    attempted_ids = set()
    result = {}

    for job in jobs_snapshot:
        job_id = job.get("id", "unknown")
        if job_id in attempted_ids:
            continue
        attempted_ids.add(job_id)

        # Re-read the job from disk in case it was updated by a previous iteration
        current_jobs = load_image_jobs()
        current_job = next((j for j in current_jobs if j.get("id") == job_id), None)
        if current_job is None or current_job.get("needs_user_decision"):
            continue  # Already removed or stuck by previous iteration

        attempt_nr = current_job.get("failed_attempts", 0) + 1
        print(f"[Image] Job {job_id} type={current_job.get('type')} attempt={attempt_nr}")

        try:
            success, error_msg = await _process_one_image_job(current_job)
        except Exception as e:
            success, error_msg = False, str(e)

        if success:
            remove_image_job(job_id)
            result = {"processed": job_id, "success": True}
            _last_image_result = result
            return result  # Done for this tick

        # Failure: increment counter, maybe mark stuck, then continue to next job
        jobs_updated = load_image_jobs()
        for j in jobs_updated:
            if j.get("id") == job_id:
                j["failed_attempts"] = j.get("failed_attempts", 0) + 1
                j["last_error"] = error_msg
                if j["failed_attempts"] >= MAX_IMAGE_ATTEMPTS:
                    j["needs_user_decision"] = True
                    print(f"[Image] Job {job_id} blockiert nach {MAX_IMAGE_ATTEMPTS} Versuchen: {error_msg}")
                else:
                    print(f"[Image] Job {job_id} Fehler ({j['failed_attempts']}/{MAX_IMAGE_ATTEMPTS}): {error_msg} — nächster Job")
                break
        save_image_jobs(jobs_updated)
        result = {"processed": job_id, "success": False, "error": error_msg}
        # Continue loop to try next job

    # All active jobs tried, none succeeded
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
        "   If update_product_full returns 'merged': a duplicate existed and was merged.\n"
        "2. get_products_for_lookup_decision() -> set_lookup_decision() or skip_lookup_job()\n"
        "3. get_products_pending_lookup() - INFO ONLY\n\n"
        "MERGE: merge_products(product_id_to_remove, product_id_to_keep)\n"
        "  - update_product_full() auto-merges on UNIQUE name conflict.\n\n"
        "STUCK JOBS: list_image_jobs() shows stuck_jobs. Inform user, then\n"
        "  dismiss_failed_image_job(job_id) to remove.\n\n"
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
async def get_products_for_name_review() -> dict:
    """CLAUDE'S TASK: Set name, group, location, units, MHD and search_term.
    Returns products without 'claude_reviewed:' in description.
    Process each with update_product_full()."""
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
    stuck = [j for j in image_jobs if j.get("needs_user_decision")]
    product_image_jobs = [j for j in image_jobs if j.get("type") == "product_image"]
    return {
        "queued_for_lookup": len(pending),
        "lookup_pending": [{"product_id": i["product_id"], "grocy_name": i["grocy_name"],
                            "search_term": i["search_term"]} for i in pending],
        "no_off_results_count": len(no_results),
        "no_off_results": [{"job_id": j["id"], "product_id": j["product_id"],
                            "grocy_name": j["grocy_name"]} for j in no_results],
        "image_download_pending": len(product_image_jobs),
        "stuck_image_jobs": [{"job_id": j["id"], "type": j["type"], "grocy_id": j["grocy_id"],
                              "failed_attempts": j.get("failed_attempts", 0),
                              "last_error": j.get("last_error")} for j in stuck],
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
    """List all pending image jobs, separated into active and stuck.
    Stuck jobs (needs_user_decision=true) are skipped by the worker."""
    jobs = load_image_jobs()
    stuck = [j for j in jobs if j.get("needs_user_decision")]
    active = [j for j in jobs if not j.get("needs_user_decision")]
    return {"job_count": len(jobs), "active_count": len(active), "stuck_count": len(stuck),
            "active_jobs": active, "stuck_jobs": stuck}


@mcp.tool()
async def dismiss_failed_image_job(job_id: str) -> dict:
    """Remove a stuck image job after user confirmation."""
    jobs = load_image_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        return {"success": False, "error": f"Job {job_id} nicht gefunden"}
    if not job.get("needs_user_decision"):
        return {"success": False, "error": "Nur stuck Jobs können dismissed werden"}
    remove_image_job(job_id)
    return {"success": True, "job_id": job_id, "grocy_id": job.get("grocy_id"),
            "last_error": job.get("last_error"), "note": "Job entfernt. Produkt hat kein Bild."}


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


# ── App assembly ─────────────────────────────────────────

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
    start_image_worker()
    start_off_lookup_worker()
    start_auto_queue_worker()
    uvicorn.run(app, host="0.0.0.0", port=8770)
