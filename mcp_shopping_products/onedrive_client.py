"""OneDrive-Abruf fuer den Einkaufsscanner (ab 3.15.0).

Holt Fotos fuer Rezeptbilder ueber den Endpunkt 'scanner' des OneDrive SyncServers
(Token-API, Port 8776) statt ueber den alten Endpunkt /api/photo (ohne Token, fester Ordner).
Der Scanner liest aus OneDrive nur; das Ergebnis geht nach Grocy, nicht zurueck nach OneDrive.

Ablauf je Foto: Suche nach dem Dateinamen -> Download-Auftrag -> Link abholen (Pruefsumme
wird verglichen). Steht der Endpunkt auf 'nur mit Bestaetigung', wartet der Auftrag auf die
Freigabe; der Worker versucht es dann beim naechsten Takt (1/min) erneut, derselbe Auftrag wird
wiederverwendet.

install() ersetzt in server.py nur _process_one_image_job (Rezept-Jobs mit bildname) und
reicht alles andere an die unveraenderte Originalfunktion weiter. Der alte OneDrive-Zweig
in server.py bleibt als ungenutzter Code stehen.
"""

import asyncio
import hashlib
import os
import time

import httpx

FALLBACK_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


class OneDriveError(Exception):
    """Fehler mit einer fuer den Job-Status lesbaren Meldung."""


class OneDriveClient:
    def __init__(self, api_url: str, token: str):
        self.url = (api_url or "").rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _post(self, client: httpx.AsyncClient, path: str, body: dict) -> tuple[int, dict]:
        resp = await client.post(self.url + path, headers=self.headers, json=body)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:120]}
        return resp.status_code, data

    async def _get(self, client: httpx.AsyncClient, path: str) -> tuple[int, dict]:
        resp = await client.get(self.url + path, headers=self.headers)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:120]}
        return resp.status_code, data

    async def find_path(self, client: httpx.AsyncClient, filename: str) -> tuple[str, bool]:
        """Suche den Dateinamen; fehlt er exakt, wird mit gaengigen Endungen versucht (raffaello.jpg -> raffaello.jpg.png)."""
        candidates = [filename] + [filename + ext for ext in FALLBACK_EXTENSIONS]
        for cand in candidates:
            status, data = await self._post(client, "/v1/search", {"filename": cand})
            if status in (401, 403):
                raise OneDriveError(f"Endpunkt verweigert den Zugriff (HTTP {status}): {data.get('error')}")
            if status >= 400:
                raise OneDriveError(f"Suche fehlgeschlagen (HTTP {status}): {data.get('error')}")
            hits = data.get("locations") or []
            if hits:
                newest = max(hits, key=lambda h: h.get("modified") or "")
                return newest["path"], cand != filename
            if data.get("search_incomplete") and cand == filename:
                raise OneDriveError("Suche unvollstaendig (Unterordner-Liste noch nicht fertig), spaeter erneut")
        raise OneDriveError(f"Datei nicht in OneDrive gefunden: {filename}")

    async def fetch(self, filename: str, max_wait: float = 60) -> tuple[bytes, str]:
        """Liefert (Dateiinhalt, tatsaechlicher Dateiname)."""
        if not self.url or not self.headers:
            raise OneDriveError("onedrive_api_url oder onedrive_token fehlt in den Add-on-Optionen")
        async with httpx.AsyncClient(timeout=60) as client:
            path, _ = await self.find_path(client, filename)
            status, job = await self._post(client, "/v1/jobs", {"action": "download", "path": path})
            if status >= 400:
                raise OneDriveError(f"Auftrag abgelehnt (HTTP {status}): {job.get('error')}")
            deadline = time.monotonic() + max_wait
            while True:
                state = job.get("status")
                if state == "ready":
                    break
                if state in ("rejected", "expired", "failed"):
                    raise OneDriveError(f"OneDrive-Auftrag {state}: {job.get('error', '')}".strip())
                if state == "waiting_for_approval":
                    raise OneDriveError("wartet auf Freigabe in Home Assistant (Endpunkt 'scanner' steht auf 'nur mit Bestaetigung')")
                if time.monotonic() > deadline:
                    raise OneDriveError("OneDrive-Auftrag dauert noch, naechster Takt versucht es erneut")
                await asyncio.sleep(max(1, min(float(job.get("wait_seconds") or 2), 5)))
                status, job = await self._get(client, f"/v1/jobs/{job['job_id']}")
                if status >= 400:
                    raise OneDriveError(f"Status nicht lesbar (HTTP {status}): {job.get('error')}")
            resp = await client.get(job["download_url"])
            if resp.status_code != 200:
                raise OneDriveError(f"Download-Link lieferte HTTP {resp.status_code}")
            data = resp.content
            expected = job.get("sha256")
            if expected and hashlib.sha256(data).hexdigest() != expected:
                raise OneDriveError("Pruefsumme der heruntergeladenen Datei stimmt nicht")
            return data, job.get("name") or os.path.basename(path)


def install(server, api_url: str, token: str) -> None:
    """Biegt den OneDrive-Weg der Rezept-Jobs in server.py auf den neuen Endpunkt um."""
    client = OneDriveClient(api_url, token)
    original = server._process_one_image_job

    async def patched(job: dict):
        if job.get("type") == "rezept" and not job.get("image_url") and job.get("bildname"):
            return await _recipe_from_onedrive(server, client, job)
        return await original(job)

    server._process_one_image_job = patched
    print(f"[Image] OneDrive-Weg: Token-API {api_url or '(nicht gesetzt)'}, Token {'gesetzt' if token else 'FEHLT'}")


async def _recipe_from_onedrive(server, client: OneDriveClient, job: dict) -> tuple[bool, str | None]:
    """Wie der alte Zweig in server.py: Foto holen, als Rezeptbild nach Grocy hochladen."""
    bildname = job["bildname"]
    grocy_id = job.get("grocy_id")
    try:
        data, real_name = await client.fetch(bildname)
    except OneDriveError as e:
        return False, f"OneDrive: {e}"
    except Exception as e:
        return False, f"OneDrive Verbindungsfehler: {type(e).__name__} {e}"
    ext = os.path.splitext(real_name)[1].lstrip(".").lower() or "jpg"
    if await server._upload_recipe_picture(grocy_id, data, ext):
        hinweis = "" if real_name == bildname else f" (Datei: {real_name})"
        print(f"[Image] Rezeptbild gesetzt recipe_id={grocy_id} ueber OneDrive{hinweis}")
        return True, None
    return False, "Grocy Upload fehlgeschlagen"
