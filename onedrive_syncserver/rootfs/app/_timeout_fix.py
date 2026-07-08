@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    def run_sync():
        try:
            subprocess.run(["python3", "/app/sync_manager.py"], timeout=1800)
        except subprocess.TimeoutExpired:
            auth_log("Sync-Wrapper Timeout nach 1800s")
    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({"success": True})
