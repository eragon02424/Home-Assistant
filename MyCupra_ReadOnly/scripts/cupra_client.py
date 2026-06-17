#!/usr/bin/env python3
"""
MyCupra ReadOnly - Login & Download Client für das EU Data Act Portal
(eu-data-act.drivesomethinggreater.com)

Dieses Skript bildet den Browser-Login-Flow für Cupra/SEAT-Konten nach
und lädt die aktuellste "Home Assistant"-Datendatei für ein Fahrzeug herunter.

Reiner Test-/CLI-Client - noch KEINE Home-Assistant-Integration.
Schritt 1 unseres Plans: erst Login+Download zuverlässig zum Laufen bringen,
danach kommt die Datenauswertung und zum Schluss die HA Custom-Integration
mit Config-Flow.

Verwendung:
    python3 cupra_client.py --email DEINE@EMAIL.de --password DEINPASSWORT --vin VINNUMMER

Optional:
    --output /pfad/zur/datei.zip   (Standard: aktuelles Verzeichnis)
    --list-only                    (nur die Liste der verfügbaren Dateien zeigen, nichts laden)
    --debug                        (ausführliche Log-Ausgaben)
"""

import argparse
import base64
import json
import logging
import re
import sys
from html import unescape
from urllib.parse import urlencode

import requests

logger = logging.getLogger("cupra_client")

# ---------------------------------------------------------------------------
# Feste Konstanten des OAuth-Flows (über manuelle Tests am 17.06.2026 verifiziert)
# ---------------------------------------------------------------------------
CLIENT_ID = "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com"
SCOPE = "openid cars profile"
STATE = "de__en__CUPRA"
REDIRECT_URI = "https://eu-data-act.drivesomethinggreater.com/login"
IDENTITY_BASE = "https://identity.vwgroup.io"
PORTAL_BASE = "https://eu-data-act.drivesomethinggreater.com"

# "Home Assistant" Daueranfrage - liefert alle 15 Minuten eine neue ZIP-Datei.
# Diese Identifier-ID bleibt über alle Generierungen hinweg gleich; nur der
# Dateiname (z.B. 20260617151005_VIN.zip) ändert sich pro Generierung.
DEFAULT_REQUEST_IDENTIFIER = "6s1d9sz06nzg7hbkpvg5z11p9q29u18s"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class CupraLoginError(Exception):
    """Wird bei fehlgeschlagenem Login ausgelöst (z.B. falsches Passwort)."""


class CupraClient:
    def __init__(self, email: str, password: str, vin: str,
                 request_identifier: str = DEFAULT_REQUEST_IDENTIFIER):
        self.email = email
        self.password = password
        self.vin = vin
        self.request_identifier = request_identifier
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # ------------------------------------------------------------------
    # Hilfsfunktionen zum Extrahieren von CSRF/HMAC/relayState aus HTML
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_hidden_inputs(html: str) -> dict:
        """Extrahiert _csrf/relayState/hmac aus klassischen <input type="hidden"> Feldern."""
        result = {}
        for name in ("_csrf", "relayState", "hmac"):
            m = re.search(rf'name="{name}"\s+value="([^"]*)"', html)
            if m:
                result[name] = unescape(m.group(1))
        return result

    @staticmethod
    def _extract_js_model_fields(html: str) -> dict:
        """
        Extrahiert csrf_token/hmac/relayState aus dem window._IDK.templateModel
        JS-Objekt, wie es auf der Passwort-Seite (login/authenticate) vorkommt.
        Format dort unterscheidet sich von den HTML-hidden-inputs:
          csrf_token: 'wert',          (einfache Anführungszeichen, Leerzeichen)
          "hmac":"wert",                (doppelte Anführungszeichen, kein Leerzeichen)
          "relayState":"wert",
        """
        result = {}
        m = re.search(r"csrf_token:\s*'([^']*)'", html)
        if m:
            result["_csrf"] = m.group(1)
        m = re.search(r'"hmac":"([^"]*)"', html)
        if m:
            result["hmac"] = m.group(1)
        m = re.search(r'"relayState":"([^"]*)"', html)
        if m:
            result["relayState"] = m.group(1)
        return result

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        """Dekodiert (ohne Signaturprüfung) den Payload-Teil eines JWT."""
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    # ------------------------------------------------------------------
    # Login-Flow, Schritt für Schritt - exakt wie im Browser nachgebildet
    # und am 17.06.2026 manuell verifiziert.
    # ------------------------------------------------------------------
    def login(self) -> None:
        logger.info("Schritt 1/8: Authorize-Request")
        authorize_params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": SCOPE,
            "state": STATE,
            "redirect_uri": REDIRECT_URI,
            "prompt": "login",
        }
        r = self.session.get(
            f"{IDENTITY_BASE}/oidc/v1/authorize",
            params=authorize_params,
            allow_redirects=False,
        )
        if r.status_code != 302:
            raise CupraLoginError(f"Authorize fehlgeschlagen: HTTP {r.status_code}")
        signin_url = r.headers["location"]
        logger.debug("Signin-URL: %s", signin_url)

        logger.info("Schritt 2/8: Signin-Seite laden (E-Mail-Formular)")
        r = self.session.get(signin_url)
        r.raise_for_status()
        fields = self._extract_hidden_inputs(r.text)
        if not fields.get("_csrf"):
            raise CupraLoginError("Konnte CSRF-Token von der Signin-Seite nicht extrahieren")

        logger.info("Schritt 3/8: E-Mail senden (Identifier-POST)")
        # signin_url enthält .../signin/{client_id}?relayState=... -> Pfad für identifier-POST ableiten
        post_url = signin_url.split("?")[0].replace("/signin/", "/") + "/login/identifier"
        r = self.session.post(
            post_url,
            data={
                "_csrf": fields["_csrf"],
                "relayState": fields["relayState"],
                "hmac": fields["hmac"],
                "email": self.email,
            },
            allow_redirects=False,
        )
        if r.status_code != 303:
            raise CupraLoginError(f"E-Mail-Schritt fehlgeschlagen: HTTP {r.status_code}")
        authenticate_url = IDENTITY_BASE + r.headers["location"]
        logger.debug("Authenticate-URL: %s", authenticate_url)

        logger.info("Schritt 4/8: Passwort-Seite laden")
        r = self.session.get(authenticate_url)
        r.raise_for_status()
        pw_fields = self._extract_js_model_fields(r.text)
        if not pw_fields.get("_csrf"):
            raise CupraLoginError("Konnte CSRF-Token von der Passwort-Seite nicht extrahieren")

        logger.info("Schritt 5/8: Passwort senden (Authenticate-POST)")
        authenticate_post_url = authenticate_url.split("?")[0]
        r = self.session.post(
            authenticate_post_url,
            data={
                "_csrf": pw_fields["_csrf"],
                "relayState": pw_fields["relayState"],
                "hmac": pw_fields["hmac"],
                "email": self.email,
                "password": self.password,
            },
            allow_redirects=False,
        )
        if r.status_code == 303:
            location = r.headers.get("location", "")
            if "error=" in location:
                error_match = re.search(r"error=([\w.]+)", location)
                error_code = error_match.group(1) if error_match else "unbekannt"
                raise CupraLoginError(f"Login abgelehnt: {error_code}")
        if r.status_code != 302:
            raise CupraLoginError(f"Passwort-Schritt fehlgeschlagen: HTTP {r.status_code}")
        sso_url = r.headers["location"]

        logger.info("Schritt 6/8: SSO-Redirect folgen")
        r = self.session.get(sso_url, allow_redirects=False)
        if r.status_code != 302:
            raise CupraLoginError(f"SSO-Schritt fehlgeschlagen: HTTP {r.status_code}")
        consent_url = r.headers["location"]

        logger.info("Schritt 7/8: Consent-Redirect folgen")
        r = self.session.get(consent_url, allow_redirects=False)
        if r.status_code != 302:
            raise CupraLoginError(f"Consent-Schritt fehlgeschlagen: HTTP {r.status_code}")
        callback_success_url = r.headers["location"]

        logger.info("Schritt 8/8: Callback/success -> Authorization Code holen")
        r = self.session.get(callback_success_url, allow_redirects=False)
        if r.status_code != 302:
            raise CupraLoginError(f"Callback-Schritt fehlgeschlagen: HTTP {r.status_code}")
        portal_login_url = r.headers["location"]

        # Code-Gültigkeit (zur Info/Debug) loggen - typischerweise 300 Sekunden
        code_match = re.search(r"code=([^&]+)", portal_login_url)
        if code_match:
            try:
                payload = self._decode_jwt_payload(code_match.group(1))
                logger.debug("Authorization Code Payload: %s", payload)
            except Exception:
                pass

        logger.info("Portal-Login: Code beim Portal einlösen")
        r = self.session.get(portal_login_url, allow_redirects=False)
        if r.status_code != 302:
            raise CupraLoginError(f"Portal-Login fehlgeschlagen: HTTP {r.status_code}")
        callbacklogin_url = r.headers["location"]

        logger.info("Portal-Callback: access_token Cookie abholen")
        r = self.session.get(callbacklogin_url, allow_redirects=False)
        if r.status_code != 302:
            raise CupraLoginError(f"Portal-Callback fehlgeschlagen: HTTP {r.status_code}")

        if "access_token" not in self.session.cookies.get_dict():
            raise CupraLoginError(
                "Login durchlaufen, aber kein access_token Cookie erhalten - "
                "unerwarteter Zustand, bitte Flow erneut prüfen."
            )

        logger.info("Login erfolgreich. access_token Cookie gesetzt.")

    # ------------------------------------------------------------------
    # Daten abrufen (erst Liste, dann gezielter Download)
    # ------------------------------------------------------------------
    def list_files(self) -> list:
        """Liefert die Liste der verfügbaren Dateien für die Home-Assistant-Anfrage."""
        url = (
            f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
            f"{self.vin}/{self.request_identifier}/list"
        )
        r = self.session.get(url, headers={"type": "partial"})
        if r.status_code != 200:
            raise CupraLoginError(
                f"Datei-Liste konnte nicht geladen werden: HTTP {r.status_code} - {r.text[:200]}"
            )
        return r.json()

    def download_latest(self) -> bytes:
        """Lädt die neueste verfügbare ZIP-Datei herunter und gibt die Rohbytes zurück."""
        files = self.list_files()
        if not files:
            raise CupraLoginError("Keine Dateien in der Liste verfügbar.")
        # Liste ist laut Portal-Beobachtung neueste-zuerst sortiert,
        # zur Sicherheit trotzdem explizit nach createdOn sortieren.
        files_sorted = sorted(files, key=lambda f: f["createdOn"], reverse=True)
        latest = files_sorted[0]
        logger.info("Neueste Datei: %s (erstellt %s, %s Bytes)",
                    latest["name"], latest["createdOn"], latest.get("size"))

        url = (
            f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
            f"{self.vin}/{self.request_identifier}/download"
        )
        r = self.session.get(
            url,
            headers={"type": "partial", "filename": latest["name"]},
        )
        if r.status_code != 200:
            raise CupraLoginError(
                f"Download fehlgeschlagen: HTTP {r.status_code} - {r.text[:200]}"
            )
        return r.content, latest["name"]


def main():
    parser = argparse.ArgumentParser(description="MyCupra ReadOnly - Test-Client")
    parser.add_argument("--email", required=True, help="Cupra/SEAT Login E-Mail")
    parser.add_argument("--password", required=True, help="Cupra/SEAT Passwort")
    parser.add_argument("--vin", required=True, help="Fahrzeug-VIN")
    parser.add_argument("--output", default=".", help="Zielverzeichnis für die ZIP-Datei")
    parser.add_argument("--list-only", action="store_true",
                        help="Nur die Dateiliste zeigen, nichts herunterladen")
    parser.add_argument("--debug", action="store_true", help="Ausführliche Log-Ausgaben")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = CupraClient(email=args.email, password=args.password, vin=args.vin)

    try:
        client.login()
    except CupraLoginError as e:
        logger.error("Login fehlgeschlagen: %s", e)
        sys.exit(1)

    try:
        if args.list_only:
            files = client.list_files()
            print(json.dumps(files, indent=2, ensure_ascii=False))
        else:
            content, filename = client.download_latest()
            out_path = f"{args.output.rstrip('/')}/{filename}"
            with open(out_path, "wb") as f:
                f.write(content)
            logger.info("Datei gespeichert: %s (%d Bytes)", out_path, len(content))
    except CupraLoginError as e:
        logger.error("Datenabruf fehlgeschlagen: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
