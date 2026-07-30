# MCP Visual Studio Connector

HA Add-on, das headless Claude-Code-Jobs (`claude -p ... --ide`) auf einer
entfernten Windows-Maschine startet, die Visual Studio 2026 mit der
Erweiterung `firish/claude_code_vs` laufen hat. Dadurch stehen dem Job
zusätzlich zum normalen Claude-Code-Werkzeugsatz auch `vs-debug`
(Debugger, Test Explorer) und `vs-semantic` (Roslyn-Codenavigation) zur
Verfügung - verifiziert per manuellem Test am 2026-07-25/26, und erneut
bei einer VM-Neuinstallation am 2026-07-30.

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
   `C:\ProgramData\ssh\administrators_authorized_keys` auf der VM
   eintragen (bei Admin-Konten NICHT `~/.ssh/authorized_keys`!), privaten
   Schlüssel über den HA File Server unter `/config/mcp_vs_ssh/mcp_vs_key`
   ablegen.
7. **Workspace-Pfad zwingend als UNC-Pfad angeben**
   (`\\vmware-host\Shared Folders\...`), NICHT als Laufwerksbuchstabe
   (`Z:\...`). Per `net use`/VMware zugewiesene Laufwerksbuchstaben sind
   sitzungsgebunden - eine frische SSH-Sitzung sieht sie nicht (verifiziert
   mit `Get-PSDrive` in einer neuen SSH-Sitzung: kein `Z:` vorhanden).

## Add-on-Optionen (config.yaml)

| Option | Bedeutung |
|---|---|
| `ssh_host` | IP der Windows-VM |
| `ssh_port` | SSH-Port (Standard 22) |
| `ssh_user` | Windows-Benutzername |
| `workspace_path` | Projektpfad auf der VM, in dem `.mcp.json` liegt - **als UNC-Pfad**, z. B. `\\vmware-host\Shared Folders\01 Data\...` (siehe Punkt 7 oben) |
| `claude_binary` | Meist einfach `claude`, falls nicht im PATH: Vollpfad angeben |

## Tools

- `start_task(prompt, resume_session_id="", allowed_tools="", permission_mode="bypassPermissions")`
  → `{job_id}`
  - **Wichtig:** Default für `permission_mode` ist bewusst
    `bypassPermissions`, nicht `acceptEdits` - da der Workspace zwingend
    über einen UNC-Pfad läuft (siehe oben), stuft Claude Code Lese-/Glob-
    Zugriffe darauf als potenziellen Netzwerkzugriff ein und fragt dafür
    nach. Diese Rückfrage kann im Headless-Betrieb NIE beantwortet werden
    und lässt den Job sonst für immer unsichtbar in `RUNNING` hängen.
- `get_job_status(job_id)` → `RUNNING` oder `DONE` (+ `exit_code`,
  `session_id` fürs Resume)
- `get_job_log(job_id, tail_lines=200)` → gepufferte stream-json-Ausgabe
- `stop_task(job_id)` → best-effort Abbruch
- `list_jobs()` → alle seit Add-on-Start bekannten Jobs

## Bekannte Einschränkungen

- Keine echte Rückfrage-Unterstützung mitten im Lauf (Batch-Pattern:
  Job bricht ab, danach `start_task` mit `resume_session_id` erneut
  aufrufen).
- Job-Registry liegt nur lokal in `/data/jobs.json` - nach einem VM-
  Neustart sind zuvor laufende Jobs nicht mehr auffindbar.
- Visual Studio muss auf der VM bereits offen sein, sonst ist keine
  `vs-debug`/`vs-semantic`-Anbindung möglich (reiner Claude-Code-
  Funktionsumfang bleibt trotzdem verfügbar).
- `bypassPermissions` als Default heißt: Claude Code fragt bei NICHTS
  mehr nach, auch nicht bei potenziell riskanten Bash-Befehlen. Das ist
  hier eine bewusste Abwägung (Workspace-Zugriff ist sonst komplett
  unbrauchbar), aber bei sensiblen Aufgaben ggf. `allowed_tools` enger
  fassen.

## Versionshistorie

- 0.1.0 - Erste Version: Job-Queue-Pattern (start/status/log/stop) über
  SSH zu Windows, `--ide`-Flag für automatische IDE-Bridge-Verbindung.
- 0.1.1 - SSH-Key-Pfad von `/data` auf `/config` verschoben (über HA File
  Server erreichbar).
- 0.1.2 - Bugfix: `$env:TEMP` wurde in einfachen Anführungszeichen nicht
  expandiert.
- 0.1.3 - Workspace-Pfad auf UNC-Pfad umgestellt (VMware Shared Folder,
  sitzungsunabhängig statt Laufwerksbuchstabe).
- 0.1.4 - WMI-`ReturnValue` wird ausgewertet statt blind vertraut.
- 0.1.5 - try/catch um run.ps1, Fehler landen jetzt sichtbar im Log statt
  Jobs unsichtbar hängen zu lassen.
- 0.1.6 - **Kernfix**: Prozessstart per Windows Scheduled Task statt
  Start-Process/WMI - einzige zuverlässige Methode, die eine SSH-
  Sitzungsbeendigung übersteht (Windows OpenSSH killt sonst das
  Job-Objekt samt aller Kindprozesse).
- 0.1.7 - Encoding-Fix für claude-Output (doppelte UTF-16-Kodierung).
- 0.1.8 - Default `permission_mode` auf `bypassPermissions` geändert, da
  UNC-Pfad-Zugriffe sonst per unbeantwortbarer Rückfrage blockiert wurden.
