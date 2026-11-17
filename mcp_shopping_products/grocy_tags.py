"""Pflege-Tags fuer Rezepte und Produkte (ab 3.16.0).

Grocy kennt keine Tags. Die Tags liegen in je einem Zusatzfeld (Checkliste):
  recipes:  rezept_tags  + rezept_eingriff_grund
  products: produkt_tags + produkt_eingriff_grund

Ein Tag bedeutet nur "dieser Schritt ist abgeschlossen" (es ist vorhanden), nicht
"es ist geprueft oder plausibel". Es gibt zwei Arten:
  * abgeleitete Tags: werden aus den Grocy-Daten berechnet und bei jedem Auflisten
    bzw. refresh_tags() automatisch gesetzt oder entfernt (kein manuelles Setzen).
  * manuelle Tags: "kein Link vorhanden" (Rezept) und "Eingriff erforderlich"
    (Rezept und Produkt: Routine darf nichts tun, Jonathan muss etwas erledigen).

install(server) registriert die Werkzeuge am bestehenden MCP-Server.
"""

import re

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
TAG_NO_LINK = "kein Link vorhanden"
TAG_MASTERDATA = "Stammdaten gepflegt"
TAG_INTERVENTION = "Eingriff erforderlich"

RECIPE_DERIVED = [TAG_IMAGE, TAG_INSTRUCTION, TAG_PRODUCTS, TAG_AMOUNTS, TAG_LINK]
RECIPE_MANUAL = [TAG_NO_LINK, TAG_INTERVENTION]
PRODUCT_DERIVED = [TAG_MASTERDATA, TAG_IMAGE]
PRODUCT_MANUAL = [TAG_INTERVENTION]

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
        tags = []
        if server.CLAUDE_REVIEWED_MARKER in (prod.get("description") or ""):
            tags.append(TAG_MASTERDATA)
        if prod.get("picture_file_name"):
            tags.append(TAG_IMAGE)
        return tags

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

    def target_tags(entity, item, positions_map):
        cfg = ENTITIES[entity]
        current = _split((item.get("userfields") or {}).get(cfg["field"]))
        manual = [t for t in current if t in cfg["manual"]]
        derived = derive_recipe(item, positions_map.get(item["id"], [])) if entity == "recipes" \
            else derive_product(item)
        if entity == "recipes" and TAG_LINK in derived and TAG_NO_LINK in manual:
            manual.remove(TAG_NO_LINK)  # es gibt doch einen Link
        ordered = [t for t in _all_tags(entity) if t in derived or t in manual]
        return current, ordered

    async def refresh(client, entity) -> dict:
        await ensure_fields(client)
        cfg = ENTITIES[entity]
        items = await load_entity(client, cfg["objects"])
        pos_map = await positions_by_recipe(client) if entity == "recipes" else {}
        changed = 0
        for item in items:
            current, ordered = target_tags(entity, item, pos_map)
            if set(current) == set(ordered) and len(current) == len(set(current)):
                continue
            r = await grocy_put(client, f"/userfields/{entity}/{item['id']}",
                                json_body={cfg["field"]: _join(ordered)})
            if r.status_code in (200, 204):
                changed += 1
                item.setdefault("userfields", {})[cfg["field"]] = _join(ordered)
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

    # ── Werkzeuge ────────────────────────────────────────────────────────────
    @mcp.tool()
    async def get_tag_definitions() -> dict:
        """Alle Pflege-Tags je Entitaet (recipes, products). 'derived' werden automatisch aus
        den Grocy-Daten gesetzt (nicht manuell setzbar), 'manual' setzt Claude mit set_tags().
        Ein Tag bedeutet nur: Schritt abgeschlossen / Daten vorhanden, keine Plausibilitaetspruefung."""
        return {"recipes": {"derived": RECIPE_DERIVED, "manual": RECIPE_MANUAL,
                            "regeln": {
                                TAG_IMAGE: "Rezept hat ein Bild",
                                TAG_LINK: "Quell-Link (http) steht in der Beschreibung",
                                TAG_INSTRUCTION: f"Beschreibung hat neben dem Link mindestens {MIN_INSTRUCTION_CHARS} Zeichen Text",
                                TAG_PRODUCTS: "mindestens eine Zutat (Produkt) ist hinterlegt",
                                TAG_AMOUNTS: "mindestens eine Zutat hat eine echte Menge (> 0,0001); 0,00001 ist die vage Platzhaltermenge",
                                TAG_NO_LINK: "manuell: geprueft, es gibt wirklich keinen Link; Rezept nicht auswertbar",
                                TAG_INTERVENTION: "manuell: Routine darf nichts tun, Jonathan muss etwas erledigen (Grund Pflicht)"}},
                "products": {"derived": PRODUCT_DERIVED, "manual": PRODUCT_MANUAL,
                             "regeln": {
                                 TAG_MASTERDATA: "Stammdaten gepflegt (reviewed-Marker in der Beschreibung)",
                                 TAG_IMAGE: "Produkt hat ein Bild",
                                 TAG_INTERVENTION: "manuell: Rueckfrage an Jonathan, nichts am Produkt aendern (Grund Pflicht)"}}}

    @mcp.tool()
    async def refresh_tags(entity: str = "all") -> dict:
        """Berechnet die automatischen Tags neu und schreibt Aenderungen nach Grocy.
        entity: 'recipes', 'products' oder 'all'. Manuelle Tags bleiben unveraendert
        (Ausnahme: 'kein Link vorhanden' entfaellt, sobald ein Link in der Beschreibung steht)."""
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
        must_have=['Link hinterlegt'], must_not_have=['Bild hinterlegt','kein Link vorhanden','Eingriff erforderlich'].
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
                state = "image_job_active"
            elif pid in done and done[pid].get("decision") is None:
                state = "decision_open"
            elif pid in pending:
                state = "lookup_pending"
            else:
                state = "none"
            items.append({"product_id": pid, "name": prod.get("name"), "tags": tags,
                          "intervention_reason": reason or None, "image_state": state,
                          "search_term": terms.get(str(pid))})
        return {"count": len(rows), "shown": len(items), "products": items}

    @mcp.tool()
    async def set_tags(entity: str, item_id: int, add: list[str] | None = None,
                       remove: list[str] | None = None, reason: str | None = None) -> dict:
        """Setzt oder entfernt MANUELLE Tags an einem Rezept (entity='recipes') oder Produkt
        (entity='products'). Manuell: Rezept 'kein Link vorhanden' und 'Eingriff erforderlich',
        Produkt 'Eingriff erforderlich'. Beim Setzen von 'Eingriff erforderlich' ist reason Pflicht
        (was muss Jonathan tun?); beim Entfernen wird der Grund geleert.
        Automatische Tags (Bild, Link, Anleitung, Produkte, Mengen, Stammdaten) werden aus den
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
        rec = await collect("recipes", [TAG_INTERVENTION], [], False)
        prod = await collect("products", [TAG_INTERVENTION], [], False)
        return {"recipes": [{"recipe_id": r["id"], "name": r.get("name"), "reason": reason}
                            for r, _, reason in sorted(rec, key=lambda x: x[0]["id"])],
                "products": [{"product_id": p["id"], "name": p.get("name"), "reason": reason}
                             for p, _, reason in sorted(prod, key=lambda x: x[0]["id"])]}

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
