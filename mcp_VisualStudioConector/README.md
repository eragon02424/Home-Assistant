# MCP Visual Studio Connector

HA Add-on, das headless Claude-Code-Jobs (`claude -p ... --ide`) auf einer
entfernten Windows-Maschine startet, die Visual Studio 2026 mit der
Erweiterung `firish/claude_code_vs` laufen hat. Dadurch stehen dem Job
zusätzlich zum normalen Claude-Code-Werkzeugsatz auch `vs-debug`
(Debugger, Test Explorer) und `vs-semantic` (Roslyn-Codenavigation) zur
Verfügung - verifiziert per manuellem Test am 2026-07-25/26, erneut bei
einer VM-Neuinstallation am 2026-07-30, und nach Umzug auf einen lokalen
Git-Klon ebenfalls am 2026-07-30.

## Voraussetzungen auf dem Windows-Zielrechner (VM)

1. Visual Studio 2026 + `firish/claude_code_vs` ("Claude Code for Visual
   Studio") installiert, Projekt/Workspace geöffnet, Claude-Code-Panel
   einmal geöffnet (startet die lokale IDE-Bridge).
2. Im Panel: **Auto-connect to IDE = Yes** (einmalig per `/ide` im
   interaktiven `claude`-Terminal einrichten, siehe Chat-Verlauf).
3. Claude Code CLI installiert und mit Pro/Max-Abo oder API-Key
   eingeloggt (`claude auth status`).
4. Git-CLI installiert (`git --version` in einer normalen PowerShell
   testen) - ohne Git-CLI kann Claude Code zwar Dateien bearbeiten, aber
   keine `git add`/`commit`/`push`-Befehle ausführen (die Visual-Studio-
   eigene Git-Integration bringt einen eigenen, nicht im PATH sichtbaren
   Git-Client mit, der dafür NICHT ausreicht).
5. OpenSSH-Server aktiv (`Start-Service sshd`,
   `Set-Service sshd -StartupType Automatic`), Firewall-Regel für Port 22.
6. Nur **einen** aktiven Netzwerkadapter je virtuellem Switch verwenden
   (NAT + Bridged gleichzeitig hat bei uns zu doppelten/verzögerten
   Antworten geführt) - Bridged empfohlen, damit die VM eine normale
   IP im Heimnetz bekommt.
7. Ein SSH-Schlüsselpaar für den Zugriff durch dieses Add-on (empfohlen,
   statt Passwort im Klartext) - öffentlichen Schlüssel in
   `C:\ProgramData\ssh\administrators_authorized_keys` auf der VM
   eintragen (bei Admin-Konten NICHT `~/.ssh/authorized_keys`!), privaten
   Schlüssel über den HA File Server unter `/config/mcp_vs_ssh/mcp_vs_key`
   ablegen.
8. **Workspace-Pfad: lokaler Git-Klon empfohlen** (z. B.
   `C:\Users\<user>\Desktop\<projekt>`), NICHT ein VMware-Shared-Folder-
   UNC-Pfad. Grund (verifiziert am 2026-07-30): Der VMware-HGFS-Treiber
   unterstützt kein echtes `fchmod`, wodurch Claude Codes natives
   Write/Edit-Tool dort fehlschlägt und langsamer auf PowerShell
   ausweicht - zusätzlich funktioniert Git-CLI auf einem reinen Host-Mount
   ohnehin meist nicht sinnvoll. Auf einem lokalen Klon (`git clone` in
   der VM, Sync einfach über das ohnehin genutzte Git-Repo) funktioniert
   alles sauber und schneller.
   Falls trotzdem ein UNC-Pfad genutzt werden muss: SSH-Sitzungen sehen
   KEINE per `net use`/VMware zugewiesenen Laufwerksbuchstaben (`Z:` o.ä.)
   - das ist sitzungsgebunden (verifiziert mit `Get-PSDrive` in einer
   frischen SSH-Sitzung: kein `Z:` vorhanden). Dann zwingend den
   UNC-Pfad statt `Z:\...` verwenden.

## Add-on-Optionen (config.yaml)

| Option | Bedeutung |
|---|---|
| `ssh_host` | IP der Windows-VM |
| `ssh_port` | SSH-Port (Standard 22) |
| `ssh_user` | Windows-Benutzername |
| `workspace_path` | Projektpfad auf der VM, in dem `.mcp.json` liegt - **lokaler Pfad empfohlen** (z. B. `C:\Users\<user>\Desktop\<projekt>`), siehe Punkt 8 oben |
| `claude_binary` | Meist einfach `claude`, falls nicht im PATH: Vollpfad angeben |

## Tools

- `start_task(prompt, resume_session_id="", allowed_tools="", permission_mode="bypassPermissions")`
  → `{job_id}`
  - **Wichtig:** Default für `permission_mode` ist bewusst
    `bypassPermissions`, nicht `acceptEdits` - im Headless-Betrieb kann
    JEDE interaktive Rückfrage von Claude Code (z. B. bei potenziell
    riskanten Bash-Befehlen) niemals beantwortet werden und lässt den Job
    sonst für immer unsichtbar in `RUNNING` hängen.
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
  eine bewusste Abwägung für zuverlässigen Headless-Betrieb, aber bei
  sensiblen Aufgaben ggf. `allowed_tools` enger fassen.
- Vorsicht bei TIA-Portal-Operationen wie `OpenWithUpgrade()`: falls im
  Projektcode ein pauschaler `catch (EngineeringException)` das Upgrade
  automatisch auslöst, kann das versehentlich ein Projekt unwiderruflich
  hochstufen, obwohl der eigentliche Fehler ganz anders lag. Bei solchen
  Aufgaben im Prompt explizit "nur an Projektkopien testen" vorgeben.

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
- 0.1.9 - SyntaxWarning behoben (Escape-Sequenz im Docstring).
- 0.1.10 - Workspace auf lokalen Git-Klon umgestellt statt VMware-UNC-
  Pfad: behebt `fchmod`-Fehler beim nativen Write/Edit-Tool und
  ermöglicht Git-CLI-Nutzung (`git status`/`commit`/`push`) direkt aus
  Claude-Code-Jobs heraus.
