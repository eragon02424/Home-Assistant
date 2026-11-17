# MCP Claude Code

HA-Add-on (MCP-Server), das headless Claude-Code-Jobs (`claude -p`) auf der
dauerhaft laufenden **Linux-VM** im eigenen Netz per SSH startet und
überwacht. Claude (Chat) vergibt Aufträge, Claude Code auf der VM führt sie
aus, z. B. ein Bild per Link vom Home Assistant laden, ansehen und auswerten.

**Nicht zu verwechseln mit** `mcp_visualstudioconector` (Windows-VM mit Visual
Studio, läuft nur auf Zuruf). Beide Add-ons werden bewusst getrennt gepflegt;
die Werkzeuge sind eine eigene Kopie, Änderungen müssen bei Bedarf in beiden
vorgenommen werden.

## Voraussetzungen auf der Linux-VM

1. Claude Code installiert (Standard: `/home/eragon02424/.npm-global/bin/claude`,
   steht nicht im SSH-Standardpfad, deshalb Vollpfad in `claude_binary`).
2. Claude Code **angemeldet** (`claude auth status` zeigt `loggedIn: true`).
3. SSH-Zugang per Schlüssel; der private Schlüssel liegt auf dem HA-Host
   (Standard: der Schlüssel der Sprach-Integration unter
   `/config/.ssh/claude_speaker_assistant`). `setsid` und `nohup` vorhanden
   (Debian/Ubuntu: ja).

## Add-on-Optionen

| Option | Bedeutung |
|---|---|
| `ssh_host` / `ssh_port` / `ssh_user` | Ziel (Linux-VM) |
| `ssh_key_path` | privater SSH-Schlüssel (im Add-on schreibgeschützt unter `/config`) |
| `workspace_path` | Arbeitsverzeichnis der Jobs auf der VM (wird bei Bedarf angelegt) |
| `claude_binary` | Vollpfad zu `claude` auf der VM |
| `login_check_minutes` | Abstand der Anmeldeprüfung (Standard 10) |

Der MCP-Token wird beim ersten Start erzeugt (`/data/mcp_token`) und bei
jedem Start im Add-on-Log ausgegeben.

## Werkzeuge

- `start_task(prompt, resume_session_id, allowed_tools, permission_mode, model)` → `{job_id}`
- `get_job_status(job_id)` → `RUNNING`, `DONE` (+ `exit_code`, `session_id`), `STOPPED` oder `LOST`
- `get_job_log(job_id, tail_lines)` → Ende der stream-json-Ausgabe
- `stop_task(job_id)` → beendet die Prozessgruppe
- `list_jobs()` → alle bekannten Jobs (übersteht Add-on-Neustarts)
- `check_login()` → fragt jetzt, ob Claude Code auf der VM angemeldet ist

## Anmeldeüberwachung

Im Hintergrund wird alle `login_check_minutes` Minuten `claude auth status` auf
der VM abgefragt. Ist Claude Code nicht angemeldet, legt das Add-on eine
Benachrichtigung in Home Assistant an („Claude Code neu anmelden“, mit den
Schritten dafür) und wiederholt sie höchstens alle 6 Stunden; nach erfolgreicher
Anmeldung wird sie geschlossen. Dieselbe Meldung kommt, wenn ein Job mit einem
Anmeldefehler („OAuth session expired“ o. ä.) endet.

## Sicherheit und Grenzen

- Jobs laufen standardmäßig mit `bypassPermissions`: Claude Code fragt bei
  nichts nach, sonst bliebe ein Headless-Job bei jeder Rückfrage hängen. Wer den
  MCP-Token hat, kann damit auf der VM beliebige Befehle als SSH-Benutzer
  ausführen lassen. Token nicht weitergeben; bei sensiblen Aufgaben
  `allowed_tools` enger fassen.
- Keine echte Rückfrage mitten im Lauf (Batch-Muster: Job beenden, danach mit
  `resume_session_id` fortsetzen).
- Ein Job, dessen Prozess ohne Abschluss verschwindet (z. B. VM-Neustart), wird
  als `LOST` gemeldet statt ewig als `RUNNING`.
- Job-Dateien liegen auf der VM unter `~/mcp_claude_code_jobs/<job_id>/` und
  werden nicht automatisch gelöscht.

## Versionshistorie

- 0.1.0 - Erste Version: Kopie des Visual-Studio-Connectors für Linux (Start per
  `setsid`/`nohup` statt Windows-Aufgabenplanung, kein `--ide`), Anmeldeüberwachung
  mit Benachrichtigung in Home Assistant, Erkennung verlorener Jobs, Werkzeug
  `check_login`, Parameter `model`.
