@app.route('/auth/start', methods=['POST'])
def auth_start():
    """
    WICHTIG: Der onedrive CLI Prozess nutzt PKCE (code_verifier im Speicher).
    Er darf NICHT gekillt werden nachdem die authUrl-Datei erschienen ist -
    sonst geht der code_verifier verloren und der spaetere Code-Austausch
    schlaegt mit AADSTS70000 fehl, selbst bei gueltigem Code.
    Der Prozess muss am Leben bleiben und selbst auf responseUrl warten.
    """
    global _auth_proc
    if os.path.exists(AUTH_DEBUG_LOG):
        os.remove(AUTH_DEBUG_LOG)
    auth_log("=== Neuer Auth-Versuch (URL-Methode) ===")
    try:
        os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
        for f in [AUTH_URL_FILE, RESPONSE_URL_FILE]:
            if os.path.exists(f):
                os.remove(f)

        # Alten Prozess falls vorhanden beenden (neuer Versuch)
        if _auth_proc and _auth_proc.poll() is None:
            _auth_proc.kill()
            _auth_proc.wait()

        # Prozess bleibt bewusst am Leben - haelt den PKCE code_verifier
        _auth_proc = subprocess.Popen(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR,
             "--auth-files", f"{AUTH_URL_FILE}:{RESPONSE_URL_FILE}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(30):
            time.sleep(0.5)
            if os.path.exists(AUTH_URL_FILE):
                break
        if os.path.exists(AUTH_URL_FILE):
            with open(AUTH_URL_FILE) as f:
                url = f.read().strip()
            auth_log(f"authUrl erstellt, Laenge: {len(url)}, Prozess PID {_auth_proc.pid} bleibt aktiv")
            if url:
                return jsonify({"success": True, "url": url})
        auth_log("FEHLER: authUrl nicht erstellt")
        return jsonify({"success": False, "error": "authUrl nicht erstellt"}), 500
    except Exception as e:
        auth_log(f"EXCEPTION: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/auth/complete', methods=['POST'])
def auth_complete():
    """
    Schreibt die Antwort-URL in die Datei, die der noch laufende
    Auth-Prozess (aus /auth/start) beobachtet. Der Prozess selbst
    fuehrt dann den Code-Austausch mit seinem eigenen PKCE code_verifier
    durch und beendet sich danach von selbst.
    """
    global _auth_proc
    data = request.json
    response_url = data.get('response_url', '').strip()
    auth_log("=== auth/complete ===")
    auth_log(f"Laenge: {len(response_url)}, Anfang: {response_url[:80]}")
    try:
        if not os.path.exists(AUTH_URL_FILE):
            return jsonify({"success": False, "error": "Session abgelaufen - Link neu generieren", "error_short": "Session abgelaufen"}), 400
        if not _auth_proc or _auth_proc.poll() is not None:
            auth_log("FEHLER: Auth-Prozess laeuft nicht mehr (evtl. Server-Neustart) - Link neu generieren noetig")
            return jsonify({"success": False, "error": "Auth-Session verloren - bitte neuen Link generieren", "error_short": "Session verloren"}), 400

        with open(RESPONSE_URL_FILE, 'w') as f:
            f.write(response_url)
        auth_log("responseUrl geschrieben, warte auf laufenden Prozess...")

        # Der bereits laufende Prozess pollt selbst auf die responseUrl-Datei
        # und beendet sich nach dem Code-Austausch. Wir warten hier darauf.
        try:
            stdout, stderr = _auth_proc.communicate(timeout=30)
            auth_log(f"exit: {_auth_proc.returncode}, stdout: {stdout.decode(errors='replace')[:500]}, stderr: {stderr.decode(errors='replace')[:500]}")
        except subprocess.TimeoutExpired:
            auth_log("Timeout beim Warten auf Auth-Prozess")
            _auth_proc.kill()
            return jsonify({"success": False, "error": "Zeitueberschreitung beim Austausch", "error_short": "Timeout"}), 400

        if os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token"):
            auth_log("SUCCESS")
            _auth_proc = None
            return jsonify({"success": True})

        short_err = "Unbekannter Fehler"
        combined = (stdout.decode(errors='replace') + stderr.decode(errors='replace'))
        for line in combined.splitlines():
            if "AADSTS" in line or "Error Reason" in line:
                short_err = line.strip()[:80]
                break
        _auth_proc = None
        return jsonify({"success": False, "error": combined, "error_short": short_err}), 400
    except Exception as e:
        auth_log(f"EXCEPTION: {e}")
        return jsonify({"success": False, "error": str(e), "error_short": str(e)[:80]}), 500
