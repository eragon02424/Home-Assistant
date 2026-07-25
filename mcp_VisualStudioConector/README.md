# MCP Visual Studio Connector

HA Add-on, das headless Claude-Code-Jobs (`claude -p ... --ide`) auf einer
entfernten Windows-Maschine startet, die Visual Studio 2026 mit der
Erweiterung `firish/claude_code_vs` laufen hat. Dadurch stehen dem Job
zusätzlich zum normalen Claude-Code-Werkzeugsatz auch `vs-debug`
(Debugger, Test Explorer) und `vs-semantic` (Roslyn-Codenavigation) zur
Verfügung - verifiziert per manuellem Test am 2026-07-25.

## Voraussetzungen auf dem Windows-Zielrechner (VM)

1. Visual Studio 2026 + `firish/claude_code_vs` ("Claude Code for Visual
   Studio") installiert, Projekt/Workspace geöffnet, Claude-Code-Panel
   einmal geöffnet (startet die lokale IDE-Bridge).
2. Im Panel: **Auto-connect to IDE = Yes** (einmalig per `/ide` im
   interaktiven `claude`-Terminal einrichten, siehe Chat-Verlauf).
3. Claude Code CLI installiert und mit Pro/Max-Abo oder API-Key
   eingeloggt (`claude auth status`).
4. OpenSSH-Server aktiv (`Start-Service sshd`,
   `Set-Service sshd -StartupType Automatic`), Firewall-Regel für Port 22.
5. Nur **einen** aktiven Netzwerkadapter je virtuellem Switch verwenden
   (NAT + Bridged gleichzeitig hat bei uns zu doppelten/verzögerten
   Antworten geführt) - Bridged empfohlen, damit die VM eine normale
   IP im Heimnetz bekommt.
6. Ein SSH-Schlüsselpaar für den Zugriff durch dieses Add-on (empfohlen,
   statt Passwort im Klartext) - öffentlichen Schlüssel in
   `C:\Users\<user>\.ssh\authorized_keys` auf der VM eintragen, privaten
   Schlüssel unter `/data/ssh_key/mcp_vs_key` in diesem Add-on ablegen.

## Add-on-Optionen (config.yaml)

| Option | Bedeutung |
|---|---|
| `ssh_host` | IP der Windows-VM |
| `ssh_port` | SSH-Port (Standard 22) |
| `ssh_user` | Windows-Benutzername |
| `workspace_path` | Projektpfad auf der VM, in dem `.mcp.json` liegt (Windows-Pfadformat, z. B. `C:\01 Data\...`) |
| `claude_binary` | Meist einfach `claude`, falls nicht im PATH: Vollpfad angeben |

## Tools

- `start_task(prompt, resume_session_id="", allowed_tools="", permission_mode="acceptEdits")`
  → `{job_id}`
- `get_job_status(job_id)` → `RUNNING` oder `DONE` (+ `exit_code`,
  `session_id` fürs Resume)
- `get_job_log(job_id, tail_lines=200)` → gepufferte stream-json-Ausgabe
- `stop_task(job_id)` → best-effort Abbruch
- `list_jobs()` → alle seit Add-on-Start bekannten Jobs

## Bekannte Einschränkungen (v0.1.0)

- Keine echte Rückfrage-Unterstützung mitten im Lauf (Batch-Pattern:
  Job bricht ab, danach `start_task` mit `resume_session_id` erneut
  aufrufen).
- Job-Registry liegt nur lokal in `/data/jobs.json` - nach einem VM-
  Neustart sind zuvor laufende Jobs nicht mehr auffindbar.
- Visual Studio muss auf der VM bereits offen sein, sonst ist keine
  `vs-debug`/`vs-semantic`-Anbindung möglich (reiner Claude-Code-
  Funktionsumfang bleibt trotzdem verfügbar).

## Versionshistorie

- 0.1.0 - Erste Version: Job-Queue-Pattern (start/status/log/stop) über
  SSH zu Windows, `--ide`-Flag für automatische IDE-Bridge-Verbindung.
