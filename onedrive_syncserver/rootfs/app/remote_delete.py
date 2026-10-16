#!/usr/bin/env python3
"""OneDrive SyncServer - Gezielte Remote-Loeschung (rekursiv)

Hintergrund: Der normale Upload-Pass laeuft IMMER mit --no-remote-delete
(sicherheitskritisch, Vorfall 09.07.2026, siehe sync_manager.py). Fuer
Ordner, die der Nutzer ausdruecklich mit "delete_remote": true markiert,
wird lokales Loeschen/Verschieben ZUSAETZLICH gezielt per Graph-API
DELETE nach OneDrive uebertragen (die Dateien landen im OneDrive-
Papierkorb).

Seit v1.42.0 gilt das Flag fuer den GESAMTEN Unterbaum des markierten
Ordners. Es wird wie sync/filter/delete_after an Unterordner vererbt;
einzelne Unterordner koennen mit "delete_remote": false ausgenommen
werden.

Zusaetzliche Ordner-Schluessel (ebenfalls vererbt, optional):
  delete_remote_dry_run  true = nur protokollieren, nichts loeschen
  delete_remote_max      Notbremse: maximale Loeschungen pro Lauf
                         (Standard 100)

SICHERUNGEN - alle fail-closed, im Zweifel wird NICHT geloescht:
1. Das Manifest enthaelt nur Dateien, die beim letzten Lauf lokal UND
   online vorhanden waren. Online neue oder nie heruntergeladene Dateien
   koennen daher nie als "lokal geloescht" gelten.
2. Nur Verzeichnisse ohne lokale Aufraeum-Regeln zaehlen (sync an,
   filter all, delete_after never, kein custom_local_path). Sonst wuerde
   unser eigener Cleanup-Schritt als Nutzer-Loeschung gewertet (genau
   die Ursache des Vorfalls).
3. Der lokale Wurzelordner muss existieren.
4. Notbremse: mehr als delete_remote_max Loeschungen pro Lauf, oder mehr
   als die Haelfte eines groesseren Manifests (ab 30 Eintraegen) ->
   nichts wird geloescht, Warnung, Manifest bleibt unveraendert.
5. Vor jedem Loeschen prueft eine Abfrage per Pfad, dass dort noch
   dieselbe Datei (gleiche Item-ID) liegt und dass sie online seit der
   letzten Pruefung nicht veraendert wurde.
6. Trockenlauf: protokolliert nur, was geloescht wuerde.
"""

import hashlib
import json
import os
import urllib.parse

DEFAULT_MAX_PER_RUN = 100
RATIO_MIN_MANIFEST = 30      # Verhaeltnis-Notbremse greift erst ab dieser Manifestgroesse
RATIO_MAX_MISSING = 0.5      # mehr als die Haelfte fehlt -> Notbremse
PAGE_SIZE = 200
MAX_DEPTH = 12
SELECT_FIELDS = "id,name,folder,file,lastModifiedDateTime"


class RemoteListError(Exception):
    """Online-Ordnerinhalt konnte nicht vollstaendig gelesen werden."""


def manifest_file_for(manifest_dir, path):
    """Gleicher Dateiname wie bisher, damit vorhandene Manifeste weiter gelten."""
    safe_name = hashlib.sha256(path.encode()).hexdigest()[:16]
    return os.path.join(manifest_dir, f"{safe_name}.json")


def find_roots(config):
    """Ordner mit ausdruecklich gesetztem delete_remote. Liegt ein markierter
    Ordner unterhalb eines ebenfalls markierten, wird er nicht doppelt
    verarbeitet (der obere deckt ihn ab)."""
    explicit = sorted(p for p, c in config.items()
                      if isinstance(c, dict) and c.get("delete_remote"))
    roots = []
    for p in explicit:
        parts = p.split("/")
        if any("/".join(parts[:i]) in explicit for i in range(1, len(parts))):
            continue
        roots.append(p)
    return roots


def is_eligible(dir_path, config, effective_config):
    """Darf in diesem Verzeichnis online geloescht werden? Nur wenn das
    Flag gilt UND keine lokale Aufraeum-Regel Dateien entfernen kann."""
    eff = effective_config(dir_path, config)
    return (bool(eff.get("delete_remote"))
            and bool(eff.get("sync"))
            and eff.get("filter") == "all"
            and eff.get("delete_after") == "never"
            and not eff.get("custom_local_path"))


def _entry(value):
    """Altes Manifestformat: Name -> Item-ID (String). Neues Format:
    Pfad -> {"id": ..., "m": lastModifiedDateTime}."""
    if isinstance(value, dict):
        return {"id": value.get("id"), "m": value.get("m")}
    return {"id": value, "m": None}


def load_manifest(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for rel, value in raw.items():
        entry = _entry(value)
        if entry["id"]:
            result[rel] = entry
    return result


def save_manifest(path, manifest):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f)
    os.replace(tmp, path)


def _dir_of(root_path, rel):
    parent = os.path.dirname(rel)
    return root_path if not parent else f"{root_path}/{parent}"


def collect_local_files(root_dir, root_path, config, effective_config):
    """Relative Pfade aller lokalen Dateien in zulaessigen Verzeichnissen."""
    result = set()
    for cur, _dirs, files in os.walk(root_dir):
        rel_dir = os.path.relpath(cur, root_dir)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        full_dir = root_path if not rel_dir else f"{root_path}/{rel_dir}"
        if not is_eligible(full_dir, config, effective_config):
            continue
        for name in files:
            result.add(name if not rel_dir else f"{rel_dir}/{name}")
    return result


def list_remote_files(session, graph_base, root_path):
    """Alle Dateien online unterhalb von root_path: relativer Pfad -> Eintrag.
    Folgt der Seitenaufteilung (@odata.nextLink). Wirft RemoteListError, wenn
    irgendeine Abfrage fehlschlaegt (dann wird das Manifest NICHT ersetzt)."""
    files = {}

    def walk(url, params, prefix, depth, is_root):
        if depth > MAX_DEPTH:
            raise RemoteListError("Verschachtelung zu tief")
        first = True
        while url:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 404 and is_root and first:
                return  # Ordner existiert online nicht (mehr): nichts zu verwalten
            if resp.status_code != 200:
                raise RemoteListError(f"Status {resp.status_code} bei '{prefix or root_path}'")
            data = resp.json()
            for item in data.get("value", []):
                name = item.get("name")
                rel = f"{prefix}{name}"
                if "folder" in item:
                    walk(f"{graph_base}/me/drive/items/{item['id']}/children",
                         {"$top": PAGE_SIZE, "$select": SELECT_FIELDS},
                         rel + "/", depth + 1, False)
                elif "file" in item:
                    files[rel] = {"id": item["id"], "m": item.get("lastModifiedDateTime")}
            url = data.get("@odata.nextLink")
            params = None  # nextLink enthaelt die Parameter bereits
            first = False

    encoded = urllib.parse.quote(root_path)
    walk(f"{graph_base}/me/drive/root:/{encoded}:/children",
         {"$top": PAGE_SIZE, "$select": SELECT_FIELDS}, "", 0, True)
    return files


def _verify_item(session, graph_base, full_path, entry):
    """Pruefung per Pfad direkt vor dem Loeschen. Rueckgabe (Zustand, Detail):
    ok | gone | mismatch | modified | error"""
    url = f"{graph_base}/me/drive/root:/{urllib.parse.quote(full_path)}"
    resp = session.get(url, params={"$select": SELECT_FIELDS}, timeout=30)
    if resp.status_code == 404:
        return "gone", None
    if resp.status_code != 200:
        return "error", f"Status {resp.status_code}"
    item = resp.json()
    if item.get("id") != entry["id"]:
        return "mismatch", "unter diesem Pfad liegt online eine andere Datei"
    if "file" not in item:
        return "mismatch", "online ist es keine Datei"
    if entry.get("m") and item.get("lastModifiedDateTime") != entry["m"]:
        return "modified", "online seit der letzten Pruefung veraendert"
    return "ok", None


def _limit(value):
    try:
        number = int(value)
        return number if number >= 0 else DEFAULT_MAX_PER_RUN
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_RUN


def _process_root(session, config, root_path, share_dir, manifest_dir,
                  graph_base, effective_config, log):
    warnings = []
    root_dir = os.path.join(share_dir, root_path)
    if not os.path.isdir(root_dir):
        msg = f"'{root_path}': lokaler Ordner fehlt - nichts geloescht"
        log(f"[delete_remote] {msg}")
        return 0, [msg]

    manifest_path = manifest_file_for(manifest_dir, root_path)
    old = load_manifest(manifest_path)
    local = collect_local_files(root_dir, root_path, config, effective_config)
    eff = effective_config(root_path, config)
    dry_run = bool(eff.get("delete_remote_dry_run"))
    max_per_run = _limit(eff.get("delete_remote_max"))

    candidates = {}
    for rel, entry in old.items():
        if rel in local:
            continue
        if not is_eligible(_dir_of(root_path, rel), config, effective_config):
            continue  # Verzeichnis hat inzwischen lokale Aufraeum-Regeln: nie online loeschen
        candidates[rel] = entry

    brake = None
    if len(candidates) > max_per_run:
        brake = (f"'{root_path}': Notbremse - {len(candidates)} Dateien fehlen lokal, "
                 f"erlaubt sind {max_per_run} pro Lauf. Nichts geloescht "
                 f"(Grenze mit delete_remote_max anheben, wenn das gewollt ist)")
    elif len(old) >= RATIO_MIN_MANIFEST and len(candidates) > RATIO_MAX_MISSING * len(old):
        brake = (f"'{root_path}': Notbremse - {len(candidates)} von {len(old)} bekannten "
                 f"Dateien fehlen lokal (mehr als die Haelfte). Nichts geloescht. "
                 f"Bitte pruefen, ob der lokale Ordner vollstaendig ist")
    if brake:
        log(f"[delete_remote] {brake}")
        return 0, [brake]

    deleted = 0
    would_delete = 0
    finished = set()   # erledigt oder bewusst verworfen: raus aus dem Manifest
    retained = {}      # spaeter erneut versuchen bzw. im Trockenlauf behalten
    for rel in sorted(candidates):
        entry = candidates[rel]
        full = f"{root_path}/{rel}"
        try:
            state, detail = _verify_item(session, graph_base, full, entry)
        except Exception as e:
            state, detail = "error", str(e)

        if state == "gone":
            log(f"[delete_remote] Online schon weg: {full}")
            finished.add(rel)
        elif state in ("mismatch", "modified"):
            msg = f"Nicht geloescht ({detail}): {full}"
            log(f"[delete_remote] {msg}")
            warnings.append(msg)
            finished.add(rel)
        elif state == "error":
            msg = f"Pruefung fehlgeschlagen ({detail}), spaeter erneut: {full}"
            log(f"[delete_remote] {msg}")
            warnings.append(msg)
            retained[rel] = entry
        elif dry_run:
            log(f"[delete_remote][TROCKENLAUF] Wuerde online loeschen: {full}")
            would_delete += 1
            retained[rel] = entry
        else:
            try:
                resp = session.delete(f"{graph_base}/me/drive/items/{entry['id']}", timeout=30)
                ok = resp.status_code in (204, 404)
                detail = f"Status {resp.status_code}: {str(resp.text)[:200]}"
            except Exception as e:
                ok, detail = False, str(e)
            if ok:
                log(f"[delete_remote] Online geloescht (lokal entfernt): {full}")
                deleted += 1
                finished.add(rel)
            else:
                msg = f"Konnte nicht online loeschen ({detail}), spaeter erneut: {full}"
                log(f"[delete_remote] {msg}")
                warnings.append(msg)
                retained[rel] = entry

    # Manifest fuer den naechsten Lauf: nur Dateien, die jetzt lokal UND online da sind
    try:
        remote = list_remote_files(session, graph_base, root_path)
        new_manifest = {rel: info for rel, info in remote.items()
                        if rel in local and is_eligible(_dir_of(root_path, rel), config, effective_config)}
        new_manifest.update(retained)
        save_manifest(manifest_path, new_manifest)
    except Exception as e:
        log(f"[delete_remote] Manifest-Update fuer '{root_path}' fehlgeschlagen, behalte altes Manifest: {e}")
        save_manifest(manifest_path, {k: v for k, v in old.items() if k not in finished})

    if dry_run and would_delete:
        log(f"[delete_remote][TROCKENLAUF] '{root_path}': {would_delete} Datei(en) wuerden online geloescht")
    return deleted, warnings


def process_roots(session, config, share_dir, manifest_dir, graph_base,
                  effective_config, log, progress=None):
    """Verarbeitet alle markierten Wurzelordner. Rueckgabe: (geloeschte
    Dateien, Liste von Warnungen)."""
    roots = find_roots(config)
    if not roots:
        return 0, []
    os.makedirs(manifest_dir, exist_ok=True)

    total_deleted = 0
    warnings = []
    for root_path in roots:
        if progress:
            progress("delete_remote", f"Pruefe Ordner fuer gezielte Remote-Loeschung: {root_path}")
        try:
            deleted, warns = _process_root(session, config, root_path, share_dir, manifest_dir,
                                           graph_base, effective_config, log)
        except Exception as e:
            deleted = 0
            warns = [f"'{root_path}': {e}"]
            log(f"[delete_remote] Fehler bei '{root_path}': {e}")
        total_deleted += deleted
        warnings.extend(warns)

    if total_deleted:
        log(f"[delete_remote] {total_deleted} Datei(en) gezielt online geloescht")
    return total_deleted, warnings
