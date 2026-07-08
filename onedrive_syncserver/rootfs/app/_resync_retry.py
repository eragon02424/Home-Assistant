def run_onedrive_sync(need_resync):
    """
    Run onedrive sync with OneDrive as master (download-only).
    --sync: fuehrt den eigentlichen Abgleich durch (Pflicht-Flag seit
    v2.5.x, ersetzt --synchronize).
    --download-only: laedt nur von OneDrive runter, laedt NIE lokale
    Aenderungen hoch.
    --cleanup-local-files: entfernt lokale Dateien deren OneDrive-Pendant
    geloescht wurde (das ist das gewuenschte "OneDrive ist Master"
    Verhalten - Loeschungen auf OneDrive propagieren lokal).

    ROBUSTHEIT: Falls der erste Versuch (ohne --resync, weil unser eigener
    Hash-Vergleich keine Aenderung sah) trotzdem mit "resync is required"
    fehlschlaegt - z.B. weil ein fruehrerer Lauf mit falschen Flags schon
    einen aenderungsbeduerftigen Zustand hinterlassen hat, den onedrive
    intern noch nicht als erledigt markiert hat - wird automatisch EINMAL
    mit --resync --resync-auth nachversucht.
    """
    def build_cmd(with_resync):
        cmd = [
            "onedrive",
            "--confdir", ONEDRIVE_CONFIG_DIR,
            "--sync",
            "--download-only",
            "--cleanup-local-files",
            "--verbose"
        ]
        if with_resync:
            cmd += ["--resync", "--resync-auth"]
        return cmd

    def run_once(with_resync):
        cmd = build_cmd(with_resync)
        log(f"[onedrive] Starte: {' '.join(cmd)}")
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            output_lines = []
            for line in proc.stdout:
                log(line.rstrip())
                output_lines.append(line)
            proc.wait(timeout=600)
            full_output = "".join(output_lines)
            if proc.returncode != 0:
                err_msg = "".join(output_lines[-30:]).strip() or f"Exit-Code {proc.returncode} ohne Ausgabe"
                return False, err_msg, full_output
            return True, None, full_output
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
            return False, "Sync timeout after 600s", ""
        except Exception as e:
            return False, str(e), ""

    ok, err, output = run_once(need_resync)
    if not ok and not need_resync and "resync is required" in output.lower():
        log("[onedrive] Automatischer Nachversuch mit --resync (onedrive verlangte es zur Laufzeit)...")
        ok, err, output = run_once(True)
    if not ok:
        log(f"[WARN] onedrive Sync fehlgeschlagen: {err}")
        return False, err
    return True, None
