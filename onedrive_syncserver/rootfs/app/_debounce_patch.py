@app.route('/auth/device/start', methods=['POST'])
def device_auth_start():
    global _poll_thread, _poll_stop, _last_start_time
    try:
        # Debounce: verhindert, dass zwei fast gleichzeitige Klicks
        # (oder zwei offene Tabs/Ports) sich gegenseitig den Code ueberschreiben.
        now = time.time()
        if now - _last_start_time < 3:
            auth_log("Start ignoriert - zu kurz nach letztem Start (Debounce)")
            existing = load_device_state()
            if existing:
                return jsonify({"success": True, "debounced": True})
        _last_start_time = now

        os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
        result, err = ms_post(DEVICE_AUTH_URL, {"client_id": CLIENT_ID, "scope": SCOPE})
        if err:
            auth_log(f"Device Auth Fehler: {err}")
            return jsonify({"success": False, "error": err.get("error_description", str(err))}), 500
        auth_log(f"Device Code: user_code={result.get('user_code')}")
        state = {
            "user_code": result["user_code"],
            "device_code": result["device_code"],
            "verification_uri": result["verification_uri"],
            "expires_in": result["expires_in"],
            "interval": result.get("interval", 5),
            "created_at": time.time()
        }
        save_device_state(state)
        _poll_stop.set()
        if _poll_thread and _poll_thread.is_alive():
            _poll_thread.join(timeout=2)
        _poll_stop = threading.Event()
        expires_at = time.time() + result["expires_in"]
        _poll_thread = threading.Thread(
            target=poll_for_token,
            args=(result["device_code"], result.get("interval", 5), expires_at),
            daemon=True
        )
        _poll_thread.start()
        return jsonify({"success": True})
    except Exception as e:
        auth_log(f"EXCEPTION: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
