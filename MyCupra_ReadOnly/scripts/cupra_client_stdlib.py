#!/usr/bin/env python3
"""
MyCupra ReadOnly - Login & Download Client für das EU Data Act Portal
(eu-data-act.drivesomethinggreater.com)

Diese Variante verwendet ausschließlich die Python-Standardbibliothek
(urllib, http.cookiejar) - keine externen Pakete (kein pip install nötig).

Reiner Test-/CLI-Client - synchron mit custom_components/mycupra/cupra_client.py.

Verwendung:
    python3 cupra_client_stdlib.py --email DEINE@EMAIL.de --password DEINPASSWORT
    python3 cupra_client_stdlib.py --email DEINE@EMAIL.de --password DEINPASSWORT --vin VINNUMMER
    python3 cupra_client_stdlib.py --email ... --password ... --vin ... --list-only
    python3 cupra_client_stdlib.py --email ... --password ... --vin ... --debug
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

CLIENT_ID = "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com"
SCOPE = "openid cars profile"
STATE = "de__en__CUPRA"
REDIRECT_URI = "https://eu-data-act.drivesomethinggreater.com/login"
IDENTITY_BASE = "https://identity.vwgroup.io"
PORTAL_BASE = "https://eu-data-act.drivesomethinggreater.com"
DEFAULT_REQUEST_IDENTIFIER = ""
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class CupraLoginError(Exception):
    pass

class CupraRetryableError(CupraLoginError):
    pass

class CupraPermanentError(CupraLoginError):
    pass

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CupraClient:
    TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60
    REQUEST_TIMEOUT_SECONDS = 15
    RETRY_SCHEDULE = [(10, 10), (10, 60), (10, 600), (10, 1200)]
    RETRY_FINAL_DELAY_SECONDS = 3600

    def __init__(self, email, password, vin, request_identifier=DEFAULT_REQUEST_IDENTIFIER, retry_speedup=1.0):
        self.email = email
        self.password = password
        self.vin = vin
        self.request_identifier = request_identifier
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            NoRedirectHandler(),
        )
        self._token_expires_at = None
        self._retry_speedup = retry_speedup

    def _request(self, method, url, data=None, headers=None, allow_404=False):
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
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            content = e.read()
            if status in (301, 302, 303, 307, 308) or allow_404:
                return status, e.headers, content
            error_text = content[:300].decode('utf-8', errors='replace')
            if status == 401:
                raise CupraRetryableError(f"HTTP 401 bei {url}: {error_text}")
            raise CupraLoginError(f"HTTP {status} bei {url}: {error_text}")
        except urllib.error.URLError as e:
            raise CupraRetryableError(f"Netzwerkfehler bei {url}: {e.reason}")

    @staticmethod
    def _extract_hidden_inputs(html):
        result = {}
        for name in ("_csrf", "relayState", "hmac"):
            m = re.search(rf'name="{name}"\s+value="([^"]*)"', html)
            if m:
                result[name] = m.group(1)
        return result

    @staticmethod
    def _extract_js_model_fields(html):
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
    def _extract_marketing_consent_fields(html):
        """
        Extrahiert alle POST-Felder für .../consent/marketing/.../skip.
        Verifiziert anhand HAR-Aufzeichnung 18.06.2026.
        """
        result = {}
        m = re.search(r"csrf_token:\s*'([^']*)'", html)
        if m:
            result["_csrf"] = m.group(1)
        m = re.search(r'"documentKey":"([^"]*)"', html)
        if m:
            result["documentKey"] = m.group(1)
        m = re.search(r'"relayStateToken":"([^"]*)"', html)
        if m:
            result["relayState"] = m.group(1)
        m = re.search(r'"hmac":"([^"]*)"', html)
        if m:
            result["hmac"] = m.group(1)
        m = re.search(r'"countryOfJurisdiction":"([^"]*)"', html)
        if m:
            result["countryOfJurisdiction"] = m.group(1)
        m = re.search(r'"language":"([^"]*)"', html)
        if m:
            result["language"] = m.group(1)
        m = re.search(r'"callback":"(https://[^"]*)"', html)
        if m:
            result["callback"] = m.group(1)
        m = re.search(r'"step":"(\d+)"', html)
        if m:
            result["step"] = m.group(1)
        channel_map = {"email": "channelemail", "mail": "channelmail",
                       "phone": "channelphone", "app": "channelapp", "sms": "channelsms"}
        m = re.search(r'"marketChannels":\[(.*?)\]', html)
        if m:
            for cid, fname in channel_map.items():
                cm = re.search(rf'"channelId":"{cid}","channelType":"([^"]*)"', m.group(1))
                if cm:
                    result[fname] = "true" if cm.group(1) != "NOT_USED" else "false"
        return result

    def _handle_marketing_consent_if_present(self, current_url, body):
        html = body.decode("utf-8", errors="replace")
        url = current_url.split("?")[0].rstrip("/") + "/skip"
        guard = 0
        while guard < 5:
            guard += 1
            fields = self._extract_marketing_consent_fields(html)
            missing = [k for k in ("_csrf", "documentKey", "relayState", "hmac",
                                    "countryOfJurisdiction", "language", "callback")
                       if k not in fields]
            if missing:
                raise CupraLoginError(f"Consent-Schritt {guard}: Felder fehlen: {missing}")
            logger.info("Marketing-Consent (Durchlauf %d, %s) - lehne ab.", guard, fields["documentKey"])
            status, headers, body = self._request("POST", url, data={
                "_csrf": fields["_csrf"],
                "documentKey": fields["documentKey"],
                "relayState": fields["relayState"],
                "hmac": fields["hmac"],
                "countryOfJurisdiction": fields["countryOfJurisdiction"],
                "language": fields["language"],
                "callback": fields["callback"].replace(" ", "%20"),
                "channelemail": fields.get("channelemail", "false"),
                "channelmail": fields.get("channelmail", "false"),
                "channelphone": fields.get("channelphone", "false"),
                "channelapp": fields.get("channelapp", "false"),
                "channelsms": fields.get("channelsms", "false"),
            })
            if status == 302:
                return headers["Location"]
            if status == 200:
                html = body.decode("utf-8", errors="replace")
                nf = self._extract_marketing_consent_fields(html)
                ns = nf.get("step")
                if not ns:
                    raise CupraLoginError(f"Consent-Durchlauf {guard}: 'step' nicht gefunden.")
                url = re.sub(r'/\d+/skip$', f'/{ns}/skip', url)
                continue
            raise CupraLoginError(f"Unerwarteter Status {status} bei Consent-Durchlauf {guard}.")
        raise CupraLoginError("Consent-Schleife nach 5 Durchläufen nicht beendet.")

    def _get_cookie(self, name):
        for cookie in self.cookie_jar:
            if cookie.name == name:
                return cookie.value
        return None

    @staticmethod
    def _decode_jwt_payload(token):
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    def is_logged_in(self):
        if self._token_expires_at is None:
            return False
        if not self._get_cookie("access_token"):
            return False
        return time.time() < (self._token_expires_at - self.TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS)

    def ensure_logged_in(self):
        if self.is_logged_in():
            logger.debug("Token noch gültig.")
        else:
            self.login()
        if not self.request_identifier:
            self.request_identifier = self.fetch_request_identifier()

    def _retry_delays(self):
        for count, delay in self.RETRY_SCHEDULE:
            for _ in range(count):
                yield delay / self._retry_speedup
        while True:
            yield self.RETRY_FINAL_DELAY_SECONDS / self._retry_speedup

    def _with_retry(self, func, *args, **kwargs):
        attempt = 0
        for delay in self._retry_delays():
            attempt += 1
            try:
                return func(*args, **kwargs)
            except CupraPermanentError:
                raise
            except CupraRetryableError as e:
                logger.warning("Versuch %d fehlgeschlagen (%s) - Retry in %ds.", attempt, e, delay)
                self._token_expires_at = None
                time.sleep(delay)

    def login(self):
        logger.info("Schritt 1/9: Authorize-Request")
        params = {"client_id": CLIENT_ID, "response_type": "code", "scope": SCOPE,
                  "state": STATE, "redirect_uri": REDIRECT_URI, "prompt": "login"}
        url = f"{IDENTITY_BASE}/oidc/v1/authorize?{urllib.parse.urlencode(params)}"
        status, headers, _ = self._request("GET", url)
        if status != 302:
            raise CupraLoginError(f"Authorize fehlgeschlagen: HTTP {status}")
        signin_url = headers["Location"]

        logger.info("Schritt 2/9: Signin-Seite laden")
        status, headers, body = self._request("GET", signin_url)
        fields = self._extract_hidden_inputs(body.decode("utf-8"))
        if not fields.get("_csrf"):
            raise CupraLoginError("CSRF nicht extrahierbar (Signin).")

        logger.info("Schritt 3/9: E-Mail senden")
        post_url = signin_url.split("?")[0].replace("/signin/", "/") + "/login/identifier"
        status, headers, _ = self._request("POST", post_url, data={
            "_csrf": fields["_csrf"], "relayState": fields["relayState"],
            "hmac": fields["hmac"], "email": self.email,
        })
        if status != 303:
            raise CupraLoginError(f"E-Mail-Schritt fehlgeschlagen: HTTP {status}")
        authenticate_url = IDENTITY_BASE + headers["Location"]

        logger.info("Schritt 4/9: Passwort-Seite laden")
        status, headers, body = self._request("GET", authenticate_url)
        pw_fields = self._extract_js_model_fields(body.decode("utf-8"))
        if not pw_fields.get("_csrf"):
            raise CupraLoginError("CSRF nicht extrahierbar (Passwort).")

        logger.info("Schritt 5/9: Passwort senden")
        status, headers, body = self._request("POST", authenticate_url.split("?")[0], data={
            "_csrf": pw_fields["_csrf"], "relayState": pw_fields["relayState"],
            "hmac": pw_fields["hmac"], "email": self.email, "password": self.password,
        })
        if status == 303:
            loc = headers.get("Location", "")
            if "error=" in loc:
                m = re.search(r"error=([\w.]+)", loc)
                raise CupraPermanentError(f"Login abgelehnt: {m.group(1) if m else 'unbekannt'}")
            raise CupraLoginError(f"Unerwarteter 303: {loc}")
        if status != 302:
            raise CupraLoginError(f"Passwort-Schritt fehlgeschlagen: HTTP {status}")
        sso_url = headers["Location"]

        logger.info("Schritt 6/9: SSO-Redirect folgen")
        status, headers, body = self._request("GET", sso_url)
        if status == 200 and "/consent/marketing/" in sso_url:
            consent_url = self._handle_marketing_consent_if_present(sso_url, body)
        elif status != 302:
            raise CupraLoginError(f"SSO-Schritt fehlgeschlagen: HTTP {status}")
        else:
            consent_url = headers["Location"]

        logger.info("Schritt 7/9: Consent-Redirect folgen")
        status, headers, body = self._request("GET", consent_url)
        if status == 200 and "/consent/marketing/" in consent_url:
            callback_success_url = self._handle_marketing_consent_if_present(consent_url, body)
        elif status != 302:
            raise CupraLoginError(f"Consent-Schritt fehlgeschlagen: HTTP {status}")
        else:
            callback_success_url = headers["Location"]

        logger.info("Schritt 8/9: Callback/success -> Authorization Code")
        status, headers, _ = self._request("GET", callback_success_url)
        if status != 302:
            raise CupraLoginError(f"Callback fehlgeschlagen: HTTP {status}")
        portal_login_url = headers["Location"]

        logger.info("Schritt 9/9: Code beim Portal einlösen")
        status, headers, _ = self._request("GET", portal_login_url)
        if status != 302:
            raise CupraLoginError(f"Portal-Login fehlgeschlagen: HTTP {status}")
        status, headers, _ = self._request("GET", headers["Location"])
        if status != 302:
            raise CupraLoginError(f"Portal-Callback fehlgeschlagen: HTTP {status}")

        if not self._get_cookie("access_token"):
            raise CupraLoginError("Kein access_token Cookie nach Login erhalten.")

        try:
            payload = self._decode_jwt_payload(self._get_cookie("access_token"))
            self._token_expires_at = payload["exp"]
            logger.debug("Token gültig bis %s",
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._token_expires_at)))
        except Exception as e:
            logger.warning("Token-Ablaufzeit nicht auslesbar: %s", e)
            self._token_expires_at = None

        logger.info("Login erfolgreich.")

    def fetch_vins(self):
        """
        Liest alle Fahrzeug-VINs des eingeloggten Accounts aus dem Portal.
        Endpunkt: GET /proxy_api/vum/v2/users/me/relations (ohne VIN im Pfad).
        Fallback: VIN-Regex aus der HTML-Übersichtsseite.
        """
        if not self.is_logged_in():
            self.login()
        url = f"{PORTAL_BASE}/proxy_api/vum/v2/users/me/relations"
        status, headers, body = self._request("GET", url, allow_404=True)
        if status == 200:
            data = json.loads(body)
            vins = []
            if isinstance(data, list):
                vins = [e["vin"] for e in data if "vin" in e]
            elif isinstance(data, dict) and "vin" in data:
                vins = [data["vin"]]
            if vins:
                logger.info("VINs via API: %s", vins)
                return vins

        # Fallback: VIN aus der Übersichtsseite extrahieren
        status2, _, body2 = self._request("GET", f"{PORTAL_BASE}/de/en/user.html", allow_404=True)
        if status2 == 200:
            vins = list(set(re.findall(r'\b([A-HJ-NPR-Z0-9]{17})\b', body2.decode("utf-8", errors="replace"))))
            if vins:
                logger.info("VINs via HTML-Fallback: %s", vins)
                return vins

        raise CupraLoginError("Keine VINs gefunden.")

    def fetch_request_identifier(self):
        """
        Liest den Identifier der Daueranfrage (type=partial) aus dem Portal.
        Endpunkt: GET /proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial
        Verifiziert anhand HAR 18.06.2026. /metadata/all liefert die one-time Anfrage.
        """
        if not self.is_logged_in():
            self.login()
        url = f"{PORTAL_BASE}/proxy_api/euda-apim/datarequest/vehicles/{self.vin}/metadata/partial"
        status, headers, body = self._request("GET", url)
        if status != 200:
            raise CupraLoginError(f"Identifier nicht auslesbar: HTTP {status}")
        data = json.loads(body)
        identifier = data.get("Identifier")
        if not identifier:
            raise CupraLoginError("Kein Identifier gefunden - Daueranfrage im Portal anlegen.")
        logger.info("Daueranfrage: '%s' (Identifier: %s)", data.get("Name", "?"), identifier)
        return identifier

    def list_files(self):
        return self._with_retry(self._list_files_once)

    def _list_files_once(self):
        self.ensure_logged_in()
        url = (f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
               f"{self.vin}/{self.request_identifier}/list")
        status, headers, body = self._request("GET", url, headers={"type": "partial"})
        if status == 400:
            raise CupraPermanentError(f"VIN/Identifier prüfen: {body[:200].decode('utf-8', errors='replace')}")
        if status != 200:
            raise CupraLoginError(f"Dateiliste fehlgeschlagen: HTTP {status}")
        return json.loads(body)

    def download_latest(self):
        return self._with_retry(self._download_latest_once)

    def _download_latest_once(self):
        files = self._list_files_once()
        if not files:
            raise CupraLoginError("Keine Dateien verfügbar.")
        latest = sorted(files, key=lambda f: f["createdOn"], reverse=True)[0]
        logger.info("Neueste Datei: %s (%s Bytes)", latest["name"], latest.get("size"))
        url = (f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
               f"{self.vin}/{self.request_identifier}/download")
        status, headers, body = self._request("GET", url,
                                               headers={"type": "partial", "filename": latest["name"]})
        if status != 200:
            raise CupraLoginError(f"Download fehlgeschlagen: HTTP {status}")
        return body, latest["name"]


def main():
    parser = argparse.ArgumentParser(description="MyCupra ReadOnly CLI")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--vin", default="", help="VIN (optional - wird automatisch erkannt falls leer)")
    parser.add_argument("--output", default=".")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--fetch-vins", action="store_true", help="Nur VINs ausgeben")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = CupraClient(email=args.email, password=args.password, vin=args.vin)

    try:
        if args.fetch_vins:
            client.login()
            vins = client.fetch_vins()
            print("VINs:", vins)
            return

        if not client.vin:
            client.login()
            vins = client.fetch_vins()
            if not vins:
                logger.error("Keine VINs gefunden.")
                sys.exit(1)
            client.vin = vins[0]
            logger.info("VIN automatisch gewählt: %s", client.vin)

        if args.list_only:
            print(json.dumps(client.list_files(), indent=2, ensure_ascii=False))
        else:
            content, filename = client.download_latest()
            out = f"{args.output.rstrip('/')}/{filename}"
            with open(out, "wb") as f:
                f.write(content)
            logger.info("Gespeichert: %s (%d Bytes)", out, len(content))
    except CupraLoginError as e:
        logger.error("Fehler: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
