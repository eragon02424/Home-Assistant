def write_sync_list(config, all_folders):
    """
    Schreibt eine onedrive sync_list Datei mit Include/Exclude-Zeilen.

    WICHTIG: onedrive's sync_list ist eine reine POSITIVLISTE - sobald die
    Datei existiert, wird NUR synchronisiert was explizit als Include-Zeile
    drinsteht. Deshalb bekommt JEDER aktivierte Top-Level-Ordner immer eine
    explizite Include-Zeile (Root-Ebene wird bewusst als "nicht inklusiv"
    behandelt, nicht wie tiefere Ebenen die vom Elternordner erben) -
    andernfalls wuerden default-aktive Top-Ordner beim Schreiben der Datei
    versehentlich mit ausgeschlossen.
    Tiefere Ebenen bekommen nur an den tatsaechlichen Uebergaengen
    (aktiviert->deaktiviert bzw. umgekehrt) eine Zeile - das erlaubt auch
    verschachteltes Ein-/Ausschalten.
    Wenn nirgendwo eine Abweichung vom Standard (alles an) vorliegt, wird
    keine sync_list geschrieben - dann laeuft der volle Sync wie gewohnt.
    """
    if not all_folders:
        # Ordnerliste konnte nicht ermittelt werden - keine Einschraenkung
        # setzen, um nichts kaputt zu machen.
        return

    includes = []
    excludes = []
    for path in all_folders:
        cur_enabled = resolve_enabled(path, config)
        parts = path.split("/")
        if len(parts) == 1:
            # Root-Ebene: IMMER als "nicht inklusiv" behandeln, damit jeder
            # aktivierte Top-Ordner eine eigene Include-Zeile bekommt.
            parent_enabled = False
        else:
            parent_enabled = resolve_enabled("/".join(parts[:-1]), config)
        if cur_enabled and not parent_enabled:
            includes.append(f"{path}/*")
        elif not cur_enabled and parent_enabled:
            excludes.append(f"!{path}/*")

    if not excludes and len(includes) == len([f for f in all_folders if "/" not in f]):
        # Keine einzige Abweichung irgendwo - alles ist Standard (an).
        # includes enthaelt dann genau alle Top-Ordner (Normalfall), keine
        # Einschraenkung noetig.
        if os.path.exists(SYNC_LIST_FILE):
            os.remove(SYNC_LIST_FILE)
        print("[sync_list] Alle Ordner aktiv - keine Einschraenkung")
        return

    with open(SYNC_LIST_FILE, "w") as f:
        for line in includes:
            f.write(line + "\n")
        for line in excludes:
            f.write(line + "\n")
    print(f"[sync_list] {len(includes)} Einschluss-, {len(excludes)} Ausschluss-Regeln geschrieben")
