"""Pflege-Tags fuer Rezepte und Produkte, Produktpflege und Auto-Queue.

Grocy kennt keine Tags. Die Tags liegen in je einem Zusatzfeld (Checkliste):
  recipes:  rezept_tags  + rezept_eingriff_grund
  products: produkt_tags + produkt_eingriff_grund

Ein Tag bedeutet nur "dieser Schritt ist abgeschlossen" (es ist vorhanden), nicht
"es ist geprueft oder plausibel". Es gibt zwei Arten:
  * abgeleitete Tags: werden aus den Grocy-Daten berechnet und bei jedem Auflisten
    bzw. refresh_tags() automatisch gesetzt oder entfernt (kein manuelles Setzen).
  * manuelle Tags: "Eingriff erforderlich" (Rezept und Produkt: Routine darf nichts tun,
    Jonathan muss etwas erledigen) und bei Produkten "Stammdaten gepflegt" (setzt
    update_product_full).

Fehlt bei einem Rezept der Link, bekommt es "Eingriff erforderlich" mit dem Grund
"Link fehlt"; steht spaeter ein Link in der Beschreibung, entfaellt der Eingriff von selbst.

install(server) registriert die Werkzeuge am MCP-Server aus server.py:
  * Tag-Werkzeuge fuer Rezepte und Produkte (Listen, set_tags, Eingriffe, Uebersicht)
  * Rezept-Werkzeuge get_recipe / update_recipe
  * Produktpflege get_products_for_name_review / update_product_full (Tor: Tag "Stammdaten gepflegt")
und liefert die Funktion zurueck, die den Auto-Queue-Worker startet (Produkte mit dem Tag
"Stammdaten gepflegt", ohne Bild und mit Suchbegriff kommen in den Bild-Lookup).
"""

import asyncio
import re
import threading
import time

import httpx

RECIPE_FIELD = "rezept_tags"
RECIPE_REASON_FIELD = "rezept_eingriff_grund"
PRODUCT_FIELD = "produkt_tags"
PRODUCT_REASON_FIELD = "produkt_eingriff_grund"

TAG_IMAGE = "Bild hinterlegt"
TAG_INSTRUCTION = "Anleitung gepflegt"
TAG_PRODUCTS = "Produkte hinterlegt"
TAG_AMOUNTS = "Mengen hinterlegt"
TAG_LINK = "Link hinterlegt"
TAG_MASTERDATA = "Stammdaten gepflegt"
TAG_INTERVENTION = "Eingriff erforderlich"
REASON_LINK_MISSING = "Link fehlt"

RECIPE_DERIVED = [TAG_IMAGE, TAG_INSTRUCTION, TAG_PRODUCTS, TAG_AMOUNTS, TAG_LINK]
RECIPE_MANUAL = [TAG_INTERVENTION]
PRODUCT_DERIVED = [TAG_IMAGE]
PRODUCT_MANUAL = [TAG_MASTERDATA, TAG_INTERVENTION]

ENTITIES = {
    "recipes": {
        "field": RECIPE_FIELD, "reason": RECIPE_REASON_FIELD, "caption": "Pflege-Tags",
        "derived": RECIPE_DERIVED, "manual": RECIPE_MANUAL, "objects": "recipes",
    },
    "products": {
        "field": PRODUCT_FIELD, "reason": PRODUCT_REASON_FIELD, "caption": "Pflege-Tags",
        "derived": PRODUCT_DERIVED, "manual": PRODUCT_MANUAL, "objects": "products",
    },
}

PLACEHOLDER_MAX = 0.0001  # Menge 0,00001 = bewusst vage Angabe (Prise, etwas)
MIN_INSTRUCTION_CHARS = 40
AUTO_QUEUE_INTERVAL = 300  # Sekunden


def _split(value) -> list:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _join(tags) -> str:
    return ",".join(tags)


def _plain(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    return re.sub(r"\s+", " ", text).strip()


def _all_tags(entity: str) -> list:
    cfg = ENTITIES[entity]
    return cfg["derived"] + cfg["manual"]


def install(server):
    """Registriert die Werkzeuge am MCP-Server und gibt start_auto_queue_worker zurueck."""
    mcp = server.mcp
    grocy_get = server.grocy_get
    grocy_put = server.grocy_put
    state = {"ready": False}

    # ── Zusatzfelder anlegen bzw. Optionsliste aktuell halten ────────────────
    async def ensure_fields(client):
        if state["ready"]:
            return
        r = await grocy_get(client, "/objects/userfields")
        existing = {(f["entity"], f["name"]): f for f in r.json()} if r.status_code == 200 else {}
        for entity, cfg in ENTITIES.items():
            config = "\n".join(_all_tags(entity))
            tag_field = existing.get((entity, cfg["field"]))
            if tag_field is None:
                await client.post(f"{server.GROCY_BASE}/objects/userfields", json={
                    "entity": entity, "name": cfg["field"], "caption": cfg["caption"],
                    "type": "preset-checklist", "config": config,
                    "show_as_column_in_tables": 0, "sort_number": 10})
            elif (tag_field.get("config") or "") != config:
                await grocy_put(client, f"/objects/userfields/{tag_field['id']}", json_body={"config": config})
            if existing.get((entity, cfg["reason"])) is None:
                await client.post(f"{server.GROCY_BASE}/objects/userfields", json={
                    "entity": entity, "name": cfg["reason"], "caption": "Eingriff: Grund",
                    "type": "text-single-line", "show_as_column_in_tables": 0, "sort_number": 11})
        state["ready"] = True

    # ── Ableitung der automatischen Tags ─────────────────────────────────────
    def derive_recipe(rec, positions) -> list:
        tags = []
        desc = rec.get("description") or ""
        if rec.get("picture_file_name"):
            tags.append(TAG_IMAGE)
        if re.search(r"https?://", desc):
            tags.append(TAG_LINK)
        rest = re.sub(r"https?://\S+", "", _plain(desc)).strip()
        if len(rest) >= MIN_INSTRUCTION_CHARS:
            tags.append(TAG_INSTRUCTION)
        if positions:
            tags.append(TAG_PRODUCTS)
        if any(float(p.get("amount") or 0) > PLACEHOLDER_MAX for p in positions):
            tags.append(TAG_AMOUNTS)
        return tags

    def derive_product(prod) -> list:
        return [TAG_IMAGE] if prod.get("picture_file_name") else []

    async def load_entity(client, entity):
        """Alle Objekte der Entitaet inkl. Zusatzfelder; bei Rezepten nur type=normal."""
        r = await grocy_get(client, f"/objects/{entity}")
        if r.status_code != 200:
            raise RuntimeError(f"Grocy returned {r.status_code} fuer {entity}")
        items = r.json()
        if entity == "recipes":
            items = [x for x in items if x.get("type", "normal") == "normal"]
        return items

    async def positions_by_recipe(client):
        r = await grocy_get(client, "/objects/recipes_pos")
        out = {}
        if r.status_code == 200:
            for p in r.json():
                out.setdefault(p["recipe_id"], []).append(p)
        return out

    async def barcodes_by_product(client):
        r = await grocy_get(client, "/objects/product_barcodes")
        out = {}
        if r.status_code == 200:
            for entry in r.json():
                out.setdefault(entry["product_id"], []).append(entry["barcode"])
        return out

    def target_state(entity, item, positions_map):
        """Gibt (aktuelle Tags, Ziel-Tags, neuer Grund oder None) zurueck."""
        cfg = ENTITIES[entity]
        uf = item.get("userfields") or {}
        current = _split(uf.get(cfg["field"]))
        manual = [t for t in current if t in cfg["manual"]]
        new_reason = None
        if entity == "recipes":
            derived = derive_recipe(item, positions_map.get(item["id"], []))
            cur_reason = (uf.get(cfg["reason"]) or "").strip()
            if TAG_LINK in derived and TAG_INTERVENTION in manual and cur_reason == REASON_LINK_MISSING:
                manual.remove(TAG_INTERVENTION)  # Link ist da, Eingriff erledigt
                new_reason = ""
        else:
            derived = derive_product(item)
        ordered = [t for t in _all_tags(entity) if t in derived or t in manual]
        return current, ordered, new_reason

    async def refresh(client, entity) -> dict:
        await ensure_fields(client)
        cfg = ENTITIES[entity]
        items = await load_entity(client, cfg["objects"])
        pos_map = await positions_by_recipe(client) if entity == "recipes" else {}
        changed = 0
        for item in items:
            current, ordered, new_reason = target_state(entity, item, pos_map)
            if new_reason is None and set(current) == set(ordered) and len(current) == len(set(current)):
                continue
            body = {cfg["field"]: _join(ordered)}
            if new_reason is not None:
                body[cfg["reason"]] = new_reason
            r = await grocy_put(client, f"/userfields/{entity}/{item['id']}", json_body=body)
            if r.status_code in (200, 204):
                changed += 1
                item.setdefault("userfields", {}).update(body)
        return {"entity": entity, "total": len(items), "changed": changed}

    def validate_tags(entity, names, what):
        known = _all_tags(entity)
        bad = [n for n in names if n not in known]
        if bad:
            raise ValueError(f"Unbekannte Tags in {what}: {bad}. Erlaubt: {known}")

    async def collect(entity, must_have, must_not_have, refresh_first):
        validate_tags(entity, must_have or [], "must_have")
        validate_tags(entity, must_not_have or [], "must_not_have")
        cfg = ENTITIES[entity]
        async with httpx.AsyncClient(timeout=60) as client:
            if refresh_first:
                await refresh(client, entity)
            items = await load_entity(client, cfg["objects"])
        rows = []
        for item in items:
            uf = item.get("userfields") or {}
            tags = _split(uf.get(cfg["field"]))
            if any(t not in tags for t in (must_have or [])):
                continue
            if any(t in tags for t in (must_not_have or [])):
                continue
            rows.append((item, tags, uf.get(cfg["reason"]) or ""))
        return rows

    async def set_product_flag(client, product_id, tag, on):
        """Setzt oder entfernt einen Produkt-Tag, andere Tags bleiben erhalten."""
        await ensure_fields(client)
        r = await grocy_get(client, f"/userfields/products/{product_id}")
        tags = _split(r.json().get(PRODUCT_FIELD)) if r.status_code == 200 else []
        if on and tag not in tags:
            tags.append(tag)
        if not on:
            tags = [t for t in tags if t != tag]
        ordered = [t for t in _all_tags("products") if t in tags]
        await grocy_put(client, f"/userfields/products/{product_id}", json_body={PRODUCT_FIELD: _join(ordered)})

    # ── Auto-Queue: Produkte mit Stammdaten, ohne Bild, in den OFF-Lookup ────
    async def auto_queue_by_tag():
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                pr = await grocy_get(client, "/objects/products")
                if pr.status_code != 200:
                    return
                products = pr.json()
                barcodes = await barcodes_by_product(client)
            known = ({i["product_id"] for i in server.load_lookup_pending()}
                     | {i["product_id"] for i in server.load_lookup_done()}
                     | {j["grocy_id"] for j in server.load_image_jobs() if j.get("type") == "product_image"})
            search_terms = server.load_search_terms()
            added = 0
            for p in products:
                pid = p["id"]
                if p.get("picture_file_name") or pid in known:
                    continue
                if TAG_MASTERDATA not in _split((p.get("userfields") or {}).get(PRODUCT_FIELD)):
                    continue
                term = search_terms.get(str(pid))
                if not term:
                    continue
                codes = barcodes.get(pid, [])
                server.add_lookup_pending(pid, p.get("name", ""), term, codes[0] if codes else None)
                added += 1
            if added:
                print(f"[AutoQ] {added} Produkt(e) in lookup_pending")
        except Exception as e:
            print(f"[AutoQ] Fehler: {e}")

    def start_auto_queue_worker():
        def loop():
            time.sleep(120)
            while True:
                asyncio.run(auto_queue_by_tag())
                time.sleep(AUTO_QUEUE_INTERVAL)
        threading.Thread(target=loop, daemon=True).start()
        print(f"[AutoQ] Auto-Queue-Worker gestartet (alle {AUTO_QUEUE_INTERVAL // 60}min)")

    # ── Produktpflege ────────────────────────────────────────────────────────
    @mcp.tool()
    async def get_products_for_name_review() -> dict:
        """CLAUDE'S TASK: Set name, group, location, units, MHD and search_term.
        Liefert Produkte ohne Tag 'Stammdaten gepflegt' und ohne 'Eingriff erforderlich'."""
        async with httpx.AsyncClient(timeout=30) as client:
            pr = await grocy_get(client, "/objects/products")
            if pr.status_code != 200:
                return {"count": 0, "results": [], "error": f"Grocy returned {pr.status_code}"}
            products = pr.json()
            barcodes = await barcodes_by_product(client)
        search_terms = server.load_search_terms()
        results = []
        for p in products:
            tags = _split((p.get("userfields") or {}).get(PRODUCT_FIELD))
            if TAG_MASTERDATA in tags or TAG_INTERVENTION in tags:
                continue
            pid = p["id"]
            results.append({
                "product_id": pid, "name": p.get("name"), "description": p.get("description"),
                "product_group_id": p.get("product_group_id"),
                "location_id": p.get("location_id"),
                "qu_id_stock": p.get("qu_id_stock"),
                "qu_id_purchase": p.get("qu_id_purchase"),
                "default_best_before_days": p.get("default_best_before_days", 0),
                "picture_file_name": p.get("picture_file_name"),
                "barcodes": barcodes.get(pid, []),
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
        reviewed=true setzt den Tag 'Stammdaten gepflegt' (reiht das Produkt fuer den Bild-Lookup ein),
        reviewed=false entfernt ihn.
        Locations: 2=Kuehlschrank, 3=Vorratsschrank, 4=Gefrierschrank, 5=Lagerschrank"""
        if search_term:
            server.set_search_term_local(product_id, search_term)
        body = {"name": name, "description": description,
                "default_best_before_days": default_best_before_days}
        for key, value in (("product_group_id", product_group_id), ("location_id", location_id),
                           ("qu_id_stock", qu_id_stock), ("qu_id_purchase", qu_id_purchase)):
            if value is not None:
                body[key] = value
        async with httpx.AsyncClient(timeout=30) as client:
            r = await grocy_put(client, f"/objects/products/{product_id}", json_body=body)
            if r.status_code in (200, 204):
                await set_product_flag(client, product_id, TAG_MASTERDATA, reviewed)
                return {"success": True, "product_id": product_id, "name": name,
                        "search_term": search_term, "reviewed": reviewed}
            if r.status_code == 400 and "UNIQUE constraint" in r.text:
                existing_r = await grocy_get(client, "/objects/products",
                                             params={"query[]": f"name={name}"})
                if existing_r.status_code == 200 and existing_r.json():
                    existing_id = existing_r.json()[0]["id"]
                    if existing_id != product_id:
                        merge_r = await client.post(
                            f"{server.GROCY_BASE}/stock/products/{product_id}/merge/{existing_id}"
                        )
                        if merge_r.status_code in (200, 204):
                            terms = server.load_search_terms()
                            if str(existing_id) not in terms and search_term:
                                terms[str(existing_id)] = search_term
                            terms.pop(str(product_id), None)
                            server.save_search_terms(terms)
                            print(f"[Merge] Auto-merge {product_id} -> {existing_id} ('{name}')")
                            return {"success": True, "merged": True, "product_id": existing_id,
                                    "removed_id": product_id, "name": name, "search_term": search_term,
                                    "note": f"Duplikat {product_id} in {existing_id} zusammengefuehrt."}
                        return {"success": False,
                                "error": f"Auto-merge fehlgeschlagen: {merge_r.status_code}"}
            return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text[:100]}"}

    # ── Tag-Werkzeuge ────────────────────────────────────────────────────────
    @mcp.tool()
    async def get_tag_definitions() -> dict:
        """Alle Pflege-Tags je Entitaet (recipes, products). 'derived' werden automatisch aus
        den Grocy-Daten gesetzt (nicht manuell setzbar), 'manual' setzt Claude mit set_tags()
        bzw. (Stammdaten gepflegt) update_product_full().
        Ein Tag bedeutet nur: Schritt abgeschlossen / Daten vorhanden, keine Plausibilitaetspruefung."""
        return {"recipes": {"derived": RECIPE_DERIVED, "manual": RECIPE_MANUAL,
                            "regeln": {
                                TAG_IMAGE: "Rezept hat ein Bild",
                                TAG_LINK: "Quell-Link (http) steht in der Beschreibung",
                                TAG_INSTRUCTION: f"Beschreibung hat neben dem Link mindestens {MIN_INSTRUCTION_CHARS} Zeichen Text",
                                TAG_PRODUCTS: "mindestens eine Zutat (Produkt) ist hinterlegt",
                                TAG_AMOUNTS: "mindestens eine Zutat hat eine echte Menge (> 0,0001); 0,00001 ist die vage Platzhaltermenge",
                                TAG_INTERVENTION: ("manuell: Routine darf nichts tun, Jonathan muss etwas erledigen (Grund Pflicht). "
                                                   f"Fehlender Link: Grund '{REASON_LINK_MISSING}', entfaellt automatisch, sobald ein Link in der Beschreibung steht")}},
                "products": {"derived": PRODUCT_DERIVED, "manual": PRODUCT_MANUAL,
                             "regeln": {
                                 TAG_MASTERDATA: "manuell, gesetzt von update_product_full(reviewed=true): Stammdaten gepflegt",
                                 TAG_IMAGE: "Produkt hat ein Bild",
                                 TAG_INTERVENTION: "manuell: Rueckfrage an Jonathan, nichts am Produkt aendern (Grund Pflicht)"}}}

    @mcp.tool()
    async def refresh_tags(entity: str = "all") -> dict:
        """Berechnet die automatischen Tags neu und schreibt Aenderungen nach Grocy.
        entity: 'recipes', 'products' oder 'all'. Manuelle Tags bleiben unveraendert
        (Ausnahme: 'Eingriff erforderlich' mit Grund 'Link fehlt' entfaellt, sobald ein Link in der Beschreibung steht)."""
        names = list(ENTITIES) if entity == "all" else [entity]
        if any(n not in ENTITIES for n in names):
            raise ValueError("entity muss 'recipes', 'products' oder 'all' sein")
        out = []
        async with httpx.AsyncClient(timeout=120) as client:
            for n in names:
                out.append(await refresh(client, n))
        return {"results": out}

    @mcp.tool()
    async def get_tag_overview() -> dict:
        """Anzahl je Tag fuer Rezepte und Produkte (nach automatischer Aktualisierung)."""
        result = {}
        async with httpx.AsyncClient(timeout=120) as client:
            for entity, cfg in ENTITIES.items():
                await refresh(client, entity)
                items = await load_entity(client, cfg["objects"])
                counts = {t: 0 for t in _all_tags(entity)}
                for it in items:
                    for t in _split((it.get("userfields") or {}).get(cfg["field"])):
                        counts[t] = counts.get(t, 0) + 1
                result[entity] = {"total": len(items), "tags": counts}
        return result

    @mcp.tool()
    async def list_recipes_by_tags(must_have: list[str] | None = None,
                                   must_not_have: list[str] | None = None,
                                   limit: int = 20, refresh_first: bool = True) -> dict:
        """Rezepte, die ALLE Tags in must_have tragen und KEINEN aus must_not_have.
        Beispiel 'Rezepte ohne Bild, bei denen nichts blockiert ist':
        must_have=['Link hinterlegt'], must_not_have=['Bild hinterlegt','Eingriff erforderlich'].
        Rezepte ohne Link: must_not_have=['Link hinterlegt','Eingriff erforderlich'].
        Liefert 'count' (alle Treffer) und die ersten 'limit' Treffer (kleinste ID zuerst)."""
        rows = await collect("recipes", must_have, must_not_have, refresh_first)
        rows.sort(key=lambda r: r[0]["id"])
        items = []
        for rec, tags, reason in rows[:max(limit, 0)]:
            m = re.search(r"https?://[^\s<\"']+", rec.get("description") or "")
            items.append({"recipe_id": rec["id"], "name": rec.get("name"), "tags": tags,
                          "intervention_reason": reason or None,
                          "link": m.group(0) if m else None,
                          "rezeptart": (rec.get("userfields") or {}).get("rezeptart")})
        return {"count": len(rows), "shown": len(items), "recipes": items}

    @mcp.tool()
    async def list_products_by_tags(must_have: list[str] | None = None,
                                    must_not_have: list[str] | None = None,
                                    limit: int = 30, refresh_first: bool = True) -> dict:
        """Produkte, die ALLE Tags in must_have tragen und KEINEN aus must_not_have.
        Beispiele: Stammdatenpflege offen = must_not_have=['Stammdaten gepflegt','Eingriff erforderlich'];
        Bild fehlt = must_have=['Stammdaten gepflegt'], must_not_have=['Bild hinterlegt','Eingriff erforderlich'].
        Je Treffer 'image_state': none | lookup_pending | decision_open | image_job_active."""
        rows = await collect("products", must_have, must_not_have, refresh_first)
        rows.sort(key=lambda r: r[0]["id"])
        pending = {i["product_id"] for i in server.load_lookup_pending()}
        done = {j["product_id"]: j for j in server.load_lookup_done()}
        jobs = {j["grocy_id"] for j in server.load_image_jobs() if j.get("type") == "product_image"}
        terms = server.load_search_terms()
        items = []
        for prod, tags, reason in rows[:max(limit, 0)]:
            pid = prod["id"]
            if pid in jobs:
                image_state = "image_job_active"
            elif pid in done and done[pid].get("decision") is None:
                image_state = "decision_open"
            elif pid in pending:
                image_state = "lookup_pending"
            else:
                image_state = "none"
            items.append({"product_id": pid, "name": prod.get("name"), "tags": tags,
                          "intervention_reason": reason or None, "image_state": image_state,
                          "search_term": terms.get(str(pid))})
        return {"count": len(rows), "shown": len(items), "products": items}

    @mcp.tool()
    async def set_tags(entity: str, item_id: int, add: list[str] | None = None,
                       remove: list[str] | None = None, reason: str | None = None) -> dict:
        """Setzt oder entfernt MANUELLE Tags an einem Rezept (entity='recipes') oder Produkt
        (entity='products'). Manuell: 'Eingriff erforderlich' (Rezept und Produkt) und bei Produkten
        'Stammdaten gepflegt' (normalerweise setzt das update_product_full). Beim Setzen von
        'Eingriff erforderlich' ist reason Pflicht (was muss Jonathan tun? Fehlender Rezept-Link: 'Link fehlt');
        beim Entfernen wird der Grund geleert.
        Automatische Tags (Bild, Link, Anleitung, Produkte, Mengen) werden aus den
        Daten abgeleitet und koennen nicht von Hand gesetzt werden."""
        if entity not in ENTITIES:
            raise ValueError("entity muss 'recipes' oder 'products' sein")
        cfg = ENTITIES[entity]
        add, remove = add or [], remove or []
        validate_tags(entity, add + remove, "add/remove")
        derived_used = [t for t in add + remove if t in cfg["derived"]]
        if derived_used:
            raise ValueError(f"{derived_used} werden automatisch aus den Daten gesetzt (refresh_tags). "
                             f"Manuell setzbar: {cfg['manual']}")
        if TAG_INTERVENTION in add and not (reason or "").strip():
            raise ValueError("Fuer 'Eingriff erforderlich' ist reason Pflicht.")
        async with httpx.AsyncClient(timeout=30) as client:
            await ensure_fields(client)
            r = await grocy_get(client, f"/userfields/{entity}/{item_id}")
            if r.status_code != 200:
                return {"success": False, "error": f"Grocy returned {r.status_code}"}
            uf = r.json()
            tags = _split(uf.get(cfg["field"]))
            for t in add:
                if t not in tags:
                    tags.append(t)
            tags = [t for t in tags if t not in remove]
            body = {cfg["field"]: _join([t for t in _all_tags(entity) if t in tags])}
            if TAG_INTERVENTION in remove:
                body[cfg["reason"]] = ""
            elif reason is not None:
                body[cfg["reason"]] = reason.strip()
            w = await grocy_put(client, f"/userfields/{entity}/{item_id}", json_body=body)
            if w.status_code not in (200, 204):
                return {"success": False, "error": f"Grocy returned {w.status_code}: {w.text[:100]}"}
        return {"success": True, "entity": entity, "id": item_id, "tags": _split(body[cfg["field"]]),
                "intervention_reason": body.get(cfg["reason"], uf.get(cfg["reason"]))}

    @mcp.tool()
    async def list_interventions() -> dict:
        """Alles, was Jonathan erledigen muss (Tag 'Eingriff erforderlich') mit Grund,
        getrennt nach Rezepten und Produkten."""
        rec = await collect("recipes", [TAG_INTERVENTION], [], True)
        prod = await collect("products", [TAG_INTERVENTION], [], False)
        return {"recipes": [{"recipe_id": r["id"], "name": r.get("name"), "reason": reason}
                            for r, _, reason in sorted(rec, key=lambda x: x[0]["id"])],
                "products": [{"product_id": p["id"], "name": p.get("name"), "reason": reason}
                             for p, _, reason in sorted(prod, key=lambda x: x[0]["id"])]}

    # ── Rezept-Werkzeuge ─────────────────────────────────────────────────────
    @mcp.tool()
    async def get_recipe(recipe_id: int) -> dict:
        """Ein Rezept komplett: Name, Portionen, Beschreibung (Klartext und HTML), Links,
        Bild, Rezeptart, Tags, Eingriffsgrund, Anzahl Zutaten und Zutaten mit Mengen."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await grocy_get(client, f"/objects/recipes/{recipe_id}")
            if r.status_code != 200:
                return {"found": False, "error": f"Grocy returned {r.status_code}"}
            rec = r.json()
            uf = (await grocy_get(client, f"/userfields/recipes/{recipe_id}")).json()
            ing = await grocy_get(client, "/objects/recipes_pos", params={"query[]": f"recipe_id={recipe_id}"})
            prods = {p["id"]: p["name"] for p in (await grocy_get(client, "/objects/products")).json()}
            units = {u["id"]: u["name"] for u in (await grocy_get(client, "/objects/quantity_units")).json()}
        desc = rec.get("description") or ""
        return {"found": True, "recipe_id": recipe_id, "name": rec.get("name"),
                "base_servings": rec.get("base_servings"), "description_html": desc,
                "description_text": _plain(desc),
                "links": re.findall(r"https?://[^\s<\"']+", desc),
                "picture_file_name": rec.get("picture_file_name"),
                "rezeptart": uf.get("rezeptart"), "tags": _split(uf.get(RECIPE_FIELD)),
                "intervention_reason": uf.get(RECIPE_REASON_FIELD) or None,
                "ingredients": [{"recipe_pos_id": p["id"], "product_id": p["product_id"],
                                 "product": prods.get(p["product_id"]), "amount": p.get("amount"),
                                 "unit": units.get(p.get("qu_id"))} for p in (ing.json() if ing.status_code == 200 else [])]}

    @mcp.tool()
    async def update_recipe(recipe_id: int, name: str | None = None, description: str | None = None,
                            base_servings: float | None = None, rezeptart: str | None = None) -> dict:
        """Aendert ein Rezept ueber seine ID (nur die uebergebenen Felder). description ist Klartext
        (Zeilenumbrueche werden zu HTML); der Quell-Link gehoert an den Anfang. Tags werden danach
        automatisch aus den Daten neu berechnet."""
        body = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = server.text_to_html(description)
        if base_servings is not None:
            body["base_servings"] = base_servings
        async with httpx.AsyncClient(timeout=30) as client:
            if body:
                r = await grocy_put(client, f"/objects/recipes/{recipe_id}", json_body=body)
                if r.status_code not in (200, 204):
                    return {"success": False, "error": f"Grocy returned {r.status_code}: {r.text[:100]}"}
            if rezeptart is not None:
                u = await grocy_put(client, f"/userfields/recipes/{recipe_id}", json_body={"rezeptart": rezeptart})
                if u.status_code not in (200, 204):
                    return {"success": False, "error": f"rezeptart: Grocy returned {u.status_code}"}
            await refresh(client, "recipes")
            tags = _split((await grocy_get(client, f"/userfields/recipes/{recipe_id}")).json().get(RECIPE_FIELD))
        return {"success": True, "recipe_id": recipe_id, "tags": tags}

    return start_auto_queue_worker
