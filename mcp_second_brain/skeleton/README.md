# Second Brain – Konventionen

Diese Datei liest Claude zu Beginn (Tool `sb_overview`). Hier stehen die Regeln, wie Wissen abgelegt wird. Jonathan kann sie jederzeit anpassen.

## Ordner

| Ordner | Inhalt |
|---|---|
| `00_Inbox/` | Unsortiertes, schnell Notiertes – wird später einsortiert |
| `10_Projekte/` | Vorhaben mit Ziel und Ende (z. B. Hundebox, Balkonkraftwerk, Openness-MCP) |
| `20_Bereiche/` | Dauerhafte Themen ohne Ende (Home Assistant, Auto, Finanzen, Arbeit, Hunde) |
| `30_Wissen/` | Nachschlagewissen, Anleitungen, Befehle, Datenblätter-Notizen |
| `40_Archiv/` | Abgeschlossene Projekte und veraltetes Wissen |

## Dateien

- Markdown, ein Thema pro Datei, Dateiname klein mit Bindestrichen: `balkonkraftwerk.md`
- Erste Zeile: `# Titel`, danach kurze Zusammenfassung (2–3 Sätze, aktueller Stand)
- Abschnitt `## Log` am Ende: datierte Einträge `- JJJJ-MM-TT: …` (neueste unten)
- Verweise auf andere Notizen: `[[dateiname]]`

## Regeln für Claude

1. Vor dem Anlegen suchen (`sb_search`) – bestehende Notiz ergänzen statt Duplikat.
2. Dauerhaft Relevantes selbstständig ablegen: Entscheidungen, Konfigurationen, Maße, IPs/Ports, Versionen, gelöste Fehler, Projektstände.
3. Nicht ablegen: Passwörter, Tokens, API-Keys, Kontonummern, Gesundheitsdaten.
4. Zusammenfassung oben aktuell halten, Verlauf in `## Log`.
5. Unklare Zuordnung → `00_Inbox/`.
