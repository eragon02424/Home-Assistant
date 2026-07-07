@app.route('/auth/complete', methods=['POST'])
def auth_complete():
    data = request.json
    response_url = data.get('response_url', '').strip()
    auth_log = f"{CONFIG_DIR}/auth_debug.log"

    def log(msg):
        with open(auth_log, 'a') as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    try:
        log(f"response_url empfangen, Laenge: {len(response_url)}")
        log(f"response_url beginnt mit: {response_url[:80]}")
        log(f"Sonderzeichen im Code: *={response_url.count('*')} !={response_url.count('!')} $={response_url.count('$')}")

        # Schreibe responseUrl
        with open(RESPONSE_URL_FILE, 'w') as f:
            f.write(response_url)
        log(f"responseUrl geschrieben nach: {RESPONSE_URL_FILE}")

        # Pruefe ob authUrl noch existiert
        if not os.path.exists(AUTH_URL_FILE):
            log("FEHLER: authUrl Datei existiert nicht mehr!")
            return jsonify({"success": False, "error": "authUrl fehlt – bitte Auth neu starten"}), 400
        log("authUrl Datei vorhanden")

        # Starte onedrive auth
        log("Starte onedrive --auth-files...")
        result = subprocess.run(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR,
             "--auth-files", f"{AUTH_URL_FILE}:{RESPONSE_URL_FILE}"],
            capture_output=True, text=True, timeout=60
        )
        log(f"onedrive exit code: {result.returncode}")
        log(f"onedrive stdout: {result.stdout[:500]}")
        log(f"onedrive stderr: {result.stderr[:500]}")

        if os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token"):
            log("SUCCESS: refresh_token erstellt")
            return jsonify({"success": True})

        log("FEHLER: refresh_token wurde nicht erstellt")
        return jsonify({"success": False, "error": result.stderr or result.stdout}), 400
    except Exception as e:
        log(f"EXCEPTION: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
