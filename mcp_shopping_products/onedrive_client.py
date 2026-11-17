"""OneDrive-Abruf fuer den Einkaufsscanner.

Holt Fotos fuer Rezeptbilder ueber den Endpunkt 'scanner' des OneDrive SyncServers
(Token-API, Port 8776). Der Scanner liest aus OneDrive nur; das Ergebnis geht nach Grocy,
nicht zurueck nach OneDrive.

Ablauf je Foto: Suche nach dem Dateinamen -> Download-Auftrag -> Link abholen (Pruefsumme
wird verglichen). Steht der Endpunkt auf 'nur mit Bestaetigung', wartet der Auftrag auf die
Freigabe; der Bild-Worker versucht es dann beim naechsten Takt (1/min) erneut, derselbe
Auftrag wird wiederverwendet.
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
        print(f"[OneDrive] Token-API {self.url or '(nicht gesetzt)'}, Token {'gesetzt' if token else 'FEHLT'}")

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        resp = await client.request(method, self.url + path, headers=self.headers, json=body)
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:120]}
        return resp.status_code, data

    async def find_path(self, client: httpx.AsyncClient, filename: str) -> str:
        """Sucht den Dateinamen; fehlt er exakt, wird mit gaengigen Endungen versucht (raffaello.jpg -> raffaello.jpg.png)."""
        for cand in [filename] + [filename + ext for ext in FALLBACK_EXTENSIONS]:
            status, data = await self._request(client, "POST", "/v1/search", {"filename": cand})
            if status in (401, 403):
                raise OneDriveError(f"Endpunkt verweigert den Zugriff (HTTP {status}): {data.get('error')}")
            if status >= 400:
                raise OneDriveError(f"Suche fehlgeschlagen (HTTP {status}): {data.get('error')}")
            hits = data.get("locations") or []
            if hits:
                return max(hits, key=lambda h: h.get("modified") or "")["path"]
            if data.get("search_incomplete") and cand == filename:
                raise OneDriveError("Suche unvollstaendig (Unterordner-Liste noch nicht fertig), spaeter erneut")
        raise OneDriveError(f"Datei nicht in OneDrive gefunden: {filename}")

    async def fetch(self, filename: str, max_wait: float = 60) -> tuple[bytes, str]:
        """Liefert (Dateiinhalt, tatsaechlicher Dateiname)."""
        if not self.url or not self.headers:
            raise OneDriveError("onedrive_api_url oder onedrive_token fehlt in den Add-on-Optionen")
        async with httpx.AsyncClient(timeout=60) as client:
            path = await self.find_path(client, filename)
            status, job = await self._request(client, "POST", "/v1/jobs", {"action": "download", "path": path})
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
                status, job = await self._request(client, "GET", f"/v1/jobs/{job['job_id']}")
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
