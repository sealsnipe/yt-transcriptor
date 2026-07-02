# yt-transcriptor

Agent-internes Tool: YouTube-Transkripte holen, bereinigen, zusammenfassen, ablegen.

## Aufruf

```bash
cd ~/projects/yt-transcriptor
source .venv/bin/activate
./yt-transcriptor "https://www.youtube.com/watch?v=VIDEO_ID"
```

Stdout ist immer kompaktes JSON mit `brief`, `key_points`, `dir`, `read_first`.

## Output (relativ zum Projektordner)

```
transcripts/<topic>/<datum>_<titel>_<id>/
  agent.json          # primär lesen
  agent.md            # Kurzüberblick
  transcript.txt      # bereinigter Volltext
  chunks/             # bei Videos >= 30 min, ~15-min-Blöcke
```

Topic wird auto-erkannt (`ai/...`, `hardware/...`, …) oder via `--topic`.

## Optionen

- `--topic`, `-T` – Themenordner erzwingen
- `--language`, `-l` – Default `de,en`
- `--text-only` – nur bereinigtes Transkript auf stdout
- `--no-save` – nichts schreiben

## Agent-Workflow

1. `./yt-transcriptor URL` ausführen
2. `read_first` (`agent.md`) oder `agent.json` lesen
3. Bei `long_form: true` gezielt `chunks/` statt `transcript.txt`

Späterer Umzug nach `~/tools/yt-transcriptor` – Pfade bleiben relativ.
