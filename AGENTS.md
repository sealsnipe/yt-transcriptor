# yt-transcriptor

Für Cursor-Agent: YouTube-Videos transkribieren und archivieren.

## Befehl

```bash
cd ~/projects/yt-transcriptor && source .venv/bin/activate && ./yt-transcriptor "URL"
```

## Nach dem Lauf

1. JSON auf stdout parsen
2. `read_first` → `agent.md` lesen für Brief + Key points
3. Details: `agent.json` oder `transcript.txt`
4. Lange Videos (`long_form: true`): `chunks/` nutzen, nicht den ganzen `transcript.txt`

## Themen

Auto-Topic unter `transcripts/`. Override: `--topic ai/foo`.
