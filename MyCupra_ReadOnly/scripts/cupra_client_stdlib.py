#!/usr/bin/env python3
"""
MyCupra ReadOnly - Login & Download Client für das EU Data Act Portal
(eu-data-act.drivesomethinggreater.com)

Diese Variante verwendet ausschließlich die Python-Standardbibliothek
(urllib, http.cookiejar) - keine externen Pakete (kein pip install nötig).

Reiner Test-/CLI-Client - noch KEINE Home-Assistant-Integration.

Verwendung:
    python3 cupra_client_stdlib.py --email DEINE@EMAIL.de --password DEINPASSWORT --vin VINNUMMER

Optional:
    --output /pfad/zur/datei.zip   (Standard: aktuelles Verzeichnis)
    --list-only                    (nur die Liste der verfügbaren Dateien zeigen, nichts laden)
    --debug                        (ausführliche Log-Ausgaben)
"""

import argparse
import base64
import http.cookiejar
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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
    """Basis-Fehlerklasse. Wird bei fehlgeschlagenem Login oder Datenabruf ausgelöst."""


class CupraRetryableError(CupraLoginError):
    """Fehler, bei denen ein erneuter Versuch sinnvoll sein kann:
    Netzwerkprobleme (kurzer Internetausfall) oder ein vom Server abgelehnter/
    abgelaufener Token (HTTP 401), der durch einen frischen Login behoben wird."""


class CupraPermanentError(CupraLoginError):
    """Fehler, bei denen ein erneuter Versuch garantiert wieder fehlschlägt:
    falsches Passwort, falsche VIN, ungültiger Anfrage-Identifier. Hier soll
    sofort und klar abgebrochen werden, statt wiederholt zu versuchen (würde
    nur Zeit verschwenden und könnte wie ein Brute-Force-Versuch wirken)."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Verhindert automatisches Folgen von Redirects, damit wir jeden Schritt
    selbst steuern können (genau wie curl ohne -L)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CupraClient:
    # Sicherheitspuffer: wir betrachten den Token schon als "abgelaufen", wenn
    # weniger als diese Anzahl Sekunden Restgültigkeit übrig sind. Verhindert,
    # dass ein Token mitten in einer mehrteiligen Anfrage (Liste -> Download)
    # plötzlich ungültig wird.
    TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60

    # Timeout pro einzelnem HTTP-Request. Verhindert, dass das Skript bei
    # Netzwerkproblemen (z.B. Server antwortet gar nicht) endlos hängen bleibt.
    REQUEST_TIMEOUT_SECONDS = 15

    # Gestaffelte Backoff-Strategie bei vorübergehenden Fehlern (Netzwerk,
    # abgelehnter/abgelaufener Token). Jedes Tupel ist (Anzahl Versuche, Wartezeit
    # in Sekunden zwischen diesen Versuchen). Die letzte Stufe wird unbegrenzt oft
    # wiederholt (dauerhafter Betrieb), bis es entweder klappt oder ein dauerhafter
    # Fehler auftritt (falsches Passwort, falsche VIN - siehe CupraPermanentError).
    RETRY_SCHEDULE = [
        (10, 10),       # 10 Versuche, alle 10 Sekunden
        (10, 60),       # 10 Versuche, alle 1 Minute
        (10, 600),      # 10 Versuche, alle 10 Minuten
        (10, 1200),     # 10 Versuche, alle 20 Minuten
    ]
    RETRY_FINAL_DELAY_SECONDS = 3600  # danach: unbegrenzt, stündlich

    def __init__(self, email: str, password: str, vin: str,
                 request_identifier: str = DEFAULT_REQUEST_IDENTIFIER,
                 retry_speedup: float = 1.0):
        self.email = email
        self.password = password
        self.vin = vin
        self.request_identifier = request_identifier
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            NoRedirectHandler(),
        )
        # Unix-Timestamp, wann der aktuelle access_token abläuft. None = noch nie eingeloggt.
        self._token_expires_at = None
        # Nur für Tests: Faktor >1 verkürzt alle Retry-Wartezeiten proportional,
        # damit man die gestaffelte Backoff-Strategie nicht stundenlang live
        # abwarten muss. Im Normalbetrieb (1.0) ohne Effekt.
        self._retry_speedup = retry_speedup

    # ------------------------------------------------------------------
    # Low-level Request-Helfer
    # ------------------------------------------------------------------
    def _request(self, method, url, data=None, headers=None, allow_404=False):
        """Führt einen HTTP-Request aus und gibt (status, response_headers, body) zurück.
        Redirects (3xx) werden NICHT automatisch verfolgt - status wird einfach
        zurückgegeben, der Aufrufer entscheidet was zu tun ist."""
        req_headers = {"User-Agent": USER_AGENT}
        if headers:
            req_headers.update(headers)

        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

        try:
            resp = self.opener.open(req, timeout=self.REQUEST_TIMEOUT_SECONDS)
            status = resp.status
            # WICHTIG: resp.headers NICHT mit dict(...) umwandeln - das zerstört
            # die Case-Insensitivität von HTTP-Headern (z.B. "location" vs "Location").
            # resp.headers ist ein email.message.Message-Objekt mit eingebautem,
            # case-insensitivem .get() - das behalten wir bei.
            resp_headers = resp.headers
            content = resp.read()
            return status, resp_headers, content
        except urllib.error.HTTPError as e:
            # 3xx und 4xx landen wegen NoRedirectHandler / urllib hier als "Fehler",
            # obwohl sie für uns gültige, erwartete Antworten sind.
            status = e.code
            resp_headers = e.headers
            content = e.read()
            if status in (301, 302, 303, 307, 308) or allow_404:
                return status, resp_headers, content
            error_text = content[:300].decode('utf-8', errors='replace')
            if status == 401:
                # Token vom Server abgelehnt/invalidiert - ein erneuter Login
                # behebt das typischerweise, daher retry-fähig.
                raise CupraRetryableError(f"HTTP 401 bei {url} (Token ungültig): {error_text}")
            raise CupraLoginError(f"HTTP {status} bei {url}: {error_text}")
        except urllib.error.URLError as e:
            # Netzwerkfehler ohne HTTP-Antwort: DNS-Fehler, Verbindung abgelehnt,
            # Timeout, kein Internet etc. Retry-fähig, da sich solche Probleme oft
            # innerhalb von Sekunden bis Minuten selbst beheben (z.B. Router-Neustart).
            raise CupraRetryableError(f"Netzwerkfehler bei {url}: {e.reason}")

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
                result[name] = m.group(1)
        return result

    @staticmethod
    def _extract_js_model_fields(html: str) -> dict:
        """
        Extrahiert csrf_token/hmac/relayState aus dem window._IDK.templateModel
        JS-Objekt, wie es auf der Passwort-Seite (login/authenticate) vorkommt.
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

    def _get_cookie(self, name: str):
        for cookie in self.cookie_jar:
            if cookie.name == name:
                return cookie.value
        return None

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        """Dekodiert (ohne Signaturprüfung - reicht für unseren Zweck, da wir den
        Token nur zur Ablaufzeit-Anzeige auslesen, nicht zur Autorisierung selbst
        verwenden) den Payload-Teil eines JWT."""
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    def is_logged_in(self) -> bool:
        """True, wenn ein aktuell noch gültiger access_token vorhanden ist
        (mit Sicherheitspuffer, siehe TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS)."""
        if self._token_expires_at is None:
            return False
        if not self._get_cookie("access_token"):
            return False
        return time.time() < (self._token_expires_at - self.TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS)

    def ensure_logged_in(self) -> None:
        """Loggt nur ein, wenn noch kein gültiger Token vorhanden ist.
        Das ist die Methode, die vor jedem Datenzugriff aufgerufen werden sollte -
        sie spart unnötige Logins, wenn der bestehende Token noch ausreichend
        lange gültig ist."""
        if self.is_logged_in():
            logger.debug("Bestehender Token ist noch gültig, kein erneuter Login nötig.")
            return
        logger.debug("Kein gültiger Token vorhanden, Login wird durchgeführt.")
        self.login()

    def _retry_delays(self):
        """Generator, der die Wartezeiten gemäß RETRY_SCHEDULE liefert und danach
        unbegrenzt RETRY_FINAL_DELAY_SECONDS weiterliefert (dauerhafter Betrieb)."""
        for count, delay in self.RETRY_SCHEDULE:
            for _ in range(count):
                yield delay / self._retry_speedup
        while True:
            yield self.RETRY_FINAL_DELAY_SECONDS / self._retry_speedup

    def _with_retry(self, func, *args, **kwargs):
        """Führt func aus und versucht es bei CupraRetryableError (Netzwerkfehler,
        abgelehnter/abgelaufener Token) gestaffelt und UNBEGRENZT erneut (siehe
        RETRY_SCHEDULE) - läuft so lange weiter, bis es entweder klappt oder ein
        CupraPermanentError auftritt. Vor jedem Retry wird der Token verworfen,
        damit ensure_logged_in() beim nächsten Versuch sicher neu einloggt.

        CupraPermanentError (falsches Passwort, falsche VIN, ungültiger Identifier)
        wird NICHT wiederholt, da ein erneuter Versuch garantiert wieder fehlschlägt -
        hier soll sofort und klar abgebrochen werden.

        ACHTUNG: diese Methode kann (bei dauerhaften Netzwerkproblemen) sehr lange
        blockieren (Stunden). Für den Einsatz in Home Assistant muss das in einem
        eigenen Hintergrund-Mechanismus laufen, der den HA-Hauptthread nicht blockiert
        - nicht direkt im synchronen Update-Pfad des Coordinators aufrufen."""
        attempt = 0
        for delay in self._retry_delays():
            attempt += 1
            try:
                return func(*args, **kwargs)
            except CupraPermanentError:
                raise
            except CupraRetryableError as e:
                logger.warning(
                    "Versuch %d fehlgeschlagen (%s) - erneuter Versuch in %ds, "
                    "Token wird dafür verworfen.",
                    attempt, e, delay,
                )
                self._token_expires_at = None  # Erzwingt frischen Login beim Retry
                time.sleep(delay)

    # ------------------------------------------------------------------
    # Login-Flow, Schritt für Schritt - exakt wie im Browser nachgebildet
    # und am 17.06.2026 manuell verifiziert.
    # ------------------------------------------------------------------
    def login(self) -> None:
        logger.info("Schritt 1/9: Authorize-Request")
        authorize_params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": SCOPE,
            "state": STATE,
            "redirect_uri": REDIRECT_URI,
            "prompt": "login",
        }
        url = f"{IDENTITY_BASE}/oidc/v1/authorize?{urllib.parse.urlencode(authorize_params)}"
        status, headers, _ = self._request("GET", url)
        if status != 302:
            raise CupraLoginError(f"Authorize fehlgeschlagen: HTTP {status}")
        signin_url = headers["Location"]
        logger.debug("Signin-URL: %s", signin_url)

        logger.info("Schritt 2/9: Signin-Seite laden (E-Mail-Formular)")
        status, headers, body = self._request("GET", signin_url)
        html = body.decode("utf-8")
        fields = self._extract_hidden_inputs(html)
        if not fields.get("_csrf"):
            raise CupraLoginError(
                "Konnte CSRF-Token von der Signin-Seite nicht extrahieren. "
                "Möglich: VW Group hat die Login-Seitenstruktur geändert "
                "(nicht zwingend ein Problem mit den Zugangsdaten)."
            )

        logger.info("Schritt 3/9: E-Mail senden (Identifier-POST)")
        post_url = signin_url.split("?")[0].replace("/signin/", "/") + "/login/identifier"
        status, headers, _ = self._request(
            "POST", post_url,
            data={
                "_csrf": fields["_csrf"],
                "relayState": fields["relayState"],
                "hmac": fields["hmac"],
                "email": self.email,
            },
        )
        if status != 303:
            raise CupraLoginError(f"E-Mail-Schritt fehlgeschlagen: HTTP {status}")
        authenticate_url = IDENTITY_BASE + headers["Location"]
        logger.debug("Authenticate-URL: %s", authenticate_url)

        logger.info("Schritt 4/9: Passwort-Seite laden")
        status, headers, body = self._request("GET", authenticate_url)
        html = body.decode("utf-8")
        pw_fields = self._extract_js_model_fields(html)
        if not pw_fields.get("_csrf"):
            raise CupraLoginError(
                "Konnte CSRF-Token von der Passwort-Seite nicht extrahieren. "
                "Möglich: VW Group hat die Login-Seitenstruktur geändert "
                "(nicht zwingend ein Problem mit den Zugangsdaten)."
            )

        logger.info("Schritt 5/9: Passwort senden (Authenticate-POST)")
        authenticate_post_url = authenticate_url.split("?")[0]
        status, headers, body = self._request(
            "POST", authenticate_post_url,
            data={
                "_csrf": pw_fields["_csrf"],
                "relayState": pw_fields["relayState"],
                "hmac": pw_fields["hmac"],
                "email": self.email,
                "password": self.password,
            },
        )
        if status == 303:
            location = headers.get("Location", "")
            if "error=" in location:
                error_match = re.search(r"error=([\w.]+)", location)
                error_code = error_match.group(1) if error_match else "unbekannt"
                raise CupraPermanentError(f"Login abgelehnt: {error_code}")
            raise CupraLoginError(f"Unerwarteter 303-Redirect ohne Fehler-Code: {location}")
        if status != 302:
            raise CupraLoginError(f"Passwort-Schritt fehlgeschlagen: HTTP {status}")
        sso_url = headers["Location"]

        logger.info("Schritt 6/9: SSO-Redirect folgen")
        status, headers, _ = self._request("GET", sso_url)
        if status != 302:
            raise CupraLoginError(f"SSO-Schritt fehlgeschlagen: HTTP {status}")
        consent_url = headers["Location"]

        logger.info("Schritt 7/9: Consent-Redirect folgen")
        status, headers, _ = self._request("GET", consent_url)
        if status != 302:
            raise CupraLoginError(f"Consent-Schritt fehlgeschlagen: HTTP {status}")
        callback_success_url = headers["Location"]

        logger.info("Schritt 8/9: Callback/success -> Authorization Code holen")
        status, headers, _ = self._request("GET", callback_success_url)
        if status != 302:
            raise CupraLoginError(f"Callback-Schritt fehlgeschlagen: HTTP {status}")
        portal_login_url = headers["Location"]

        logger.info("Schritt 9/9: Code beim Portal einlösen (access_token holen)")
        status, headers, _ = self._request("GET", portal_login_url)
        if status != 302:
            raise CupraLoginError(f"Portal-Login fehlgeschlagen: HTTP {status}")
        callbacklogin_url = headers["Location"]

        status, headers, _ = self._request("GET", callbacklogin_url)
        if status != 302:
            raise CupraLoginError(f"Portal-Callback fehlgeschlagen: HTTP {status}")

        if not self._get_cookie("access_token"):
            raise CupraLoginError(
                "Login durchlaufen, aber kein access_token Cookie erhalten - "
                "unerwarteter Zustand, bitte Flow erneut prüfen."
            )

        # Ablaufzeit aus dem Token selbst auslesen (exp-Claim), damit
        # ensure_logged_in() spätere unnötige Logins vermeiden kann.
        try:
            payload = self._decode_jwt_payload(self._get_cookie("access_token"))
            self._token_expires_at = payload["exp"]
            logger.debug(
                "Token gültig bis %s (in %d Sekunden)",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._token_expires_at)),
                self._token_expires_at - time.time(),
            )
        except Exception as e:
            # Sollte das Token-Format sich mal ändern, ist das kein Show-Stopper -
            # wir loggen dann beim nächsten Aufruf einfach sicherheitshalber neu ein.
            logger.warning("Konnte Token-Ablaufzeit nicht auslesen (%s) - "
                           "ensure_logged_in() wird beim nächsten Aufruf neu einloggen.", e)
            self._token_expires_at = None

        logger.info("Login erfolgreich. access_token Cookie gesetzt.")

    # ------------------------------------------------------------------
    # Daten abrufen (erst Liste, dann gezielter Download)
    # ------------------------------------------------------------------
    def list_files(self) -> list:
        """Liefert die Liste der verfügbaren Dateien für die Home-Assistant-Anfrage.
        Loggt automatisch (erneut) ein, falls kein gültiger Token vorhanden ist.
        Versucht es bei vorübergehenden Fehlern (Netzwerk, abgelehnter Token)
        gestaffelt und unbegrenzt erneut (siehe RETRY_SCHEDULE)."""
        return self._with_retry(self._list_files_once)

    def _list_files_once(self) -> list:
        self.ensure_logged_in()
        url = (
            f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
            f"{self.vin}/{self.request_identifier}/list"
        )
        status, headers, body = self._request("GET", url, headers={"type": "partial"})
        if status == 400:
            # 400 bedeutet i.d.R. ungültige VIN oder ungültiger Identifier -
            # ein erneuter Versuch würde garantiert wieder fehlschlagen.
            raise CupraPermanentError(
                f"Datei-Liste konnte nicht geladen werden (VIN/Identifier prüfen): "
                f"{body[:300].decode('utf-8', errors='replace')}"
            )
        if status != 200:
            raise CupraLoginError(f"Datei-Liste konnte nicht geladen werden: HTTP {status}")
        return json.loads(body)

    def download_latest(self):
        """Lädt die neueste verfügbare ZIP-Datei herunter. Gibt (bytes, filename) zurück.
        Versucht es bei vorübergehenden Fehlern gestaffelt und unbegrenzt erneut."""
        return self._with_retry(self._download_latest_once)

    def _download_latest_once(self):
        files = self._list_files_once()
        if not files:
            raise CupraLoginError("Keine Dateien in der Liste verfügbar.")
        files_sorted = sorted(files, key=lambda f: f["createdOn"], reverse=True)
        latest = files_sorted[0]
        logger.info("Neueste Datei: %s (erstellt %s, %s Bytes)",
                    latest["name"], latest["createdOn"], latest.get("size"))

        url = (
            f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
            f"{self.vin}/{self.request_identifier}/download"
        )
        status, headers, body = self._request(
            "GET", url,
            headers={"type": "partial", "filename": latest["name"]},
        )
        if status != 200:
            raise CupraLoginError(f"Download fehlgeschlagen: HTTP {status}")
        return body, latest["name"]


def main():
    parser = argparse.ArgumentParser(description="MyCupra ReadOnly - Test-Client (stdlib-only)")
    parser.add_argument("--email", required=True, help="Cupra/SEAT Login E-Mail")
    parser.add_argument("--password", required=True, help="Cupra/SEAT Passwort")
    parser.add_argument("--vin", required=True, help="Fahrzeug-VIN")
    parser.add_argument("--output", default=".", help="Zielverzeichnis für die ZIP-Datei")
    parser.add_argument("--list-only", action="store_true",
                        help="Nur die Dateiliste zeigen, nichts herunterladen")
    parser.add_argument("--double-call", action="store_true",
                        help="Testmodus: list_files() zweimal aufrufen, um zu prüfen ob "
                             "der zweite Aufruf den Login-Schritt überspringt (Token-Reuse-Test)")
    parser.add_argument("--request-id", default=DEFAULT_REQUEST_IDENTIFIER,
                        help="Identifier der Daueranfrage im Portal (Standard: die bekannte "
                             "'Home Assistant'-Anfrage). Zum Testen mit einer ungültigen ID "
                             "überschreibbar.")
    parser.add_argument("--retry-speedup", type=float, default=1.0,
                        help="Nur für Tests: Faktor >1 verkürzt alle Retry-Wartezeiten "
                             "proportional (z.B. 60 = Sekunden statt Minuten warten)")
    parser.add_argument("--debug", action="store_true", help="Ausführliche Log-Ausgaben")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = CupraClient(email=args.email, password=args.password, vin=args.vin,
                         request_identifier=args.request_id,
                         retry_speedup=args.retry_speedup)

    try:
        if args.double_call:
            logger.info("=== Erster Aufruf (sollte einloggen) ===")
            client.list_files()
            logger.info("=== Zweiter Aufruf (sollte Login NICHT wiederholen) ===")
            client.list_files()
            logger.info("Doppel-Aufruf-Test abgeschlossen.")
            return

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
        logger.error("Fehler: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Abgebrochen durch Benutzer (Strg+C).")
        sys.exit(130)


if __name__ == "__main__":
    main()
