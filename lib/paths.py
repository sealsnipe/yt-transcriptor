from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"


def resolve_transcripts_dir(path: str | None = None) -> Path:
    if path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
    else:
        candidate = DEFAULT_TRANSCRIPTS_DIR
    return candidate
