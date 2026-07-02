from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.chunks import TranscriptChunk
from lib.fetcher import TranscriptSegment, VideoMeta, format_timestamp
from lib.paths import PROJECT_ROOT, resolve_transcripts_dir
from lib.summary import render_agent_md


def slugify(value: str, max_len: int = 60) -> str:
    value = value.lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value).strip("-")
    return value[:max_len] or "video"


def topic_path(topic: str) -> Path:
    parts = [slugify(part, 40) for part in topic.split("/") if part.strip()]
    return Path(*parts) if parts else Path("inbox")


def build_output_dir(
    transcripts_dir: Path,
    topic: str,
    meta: VideoMeta,
    when: datetime | None = None,
) -> Path:
    when = when or datetime.now(timezone.utc)
    date_prefix = when.strftime("%Y-%m-%d")
    title_slug = slugify(meta.title)
    folder_name = f"{date_prefix}_{title_slug}_{meta.video_id}"
    return transcripts_dir / topic_path(topic) / folder_name


def save_agent_bundle(
    *,
    topic: str,
    meta: VideoMeta,
    clean_text: str,
    timestamped_text: str,
    summary: dict[str, Any],
    chunks: list[TranscriptChunk],
    removed_blocks: list[str],
    language: str | None,
    transcripts_path: str | None = None,
    segments: list[TranscriptSegment] | None = None,
) -> Path:
    root = resolve_transcripts_dir(transcripts_path)
    out_dir = build_output_dir(root, topic, meta)
    out_dir.mkdir(parents=True, exist_ok=True)

    rel = lambda name: str((out_dir / name).relative_to(PROJECT_ROOT))

    chunk_paths: list[dict[str, Any]] = []
    if chunks:
        chunk_dir = out_dir / "chunks"
        chunk_dir.mkdir(exist_ok=True)
        for chunk in chunks:
            name = f"{chunk.index:03d}_{int(chunk.start_seconds)}s-{int(chunk.end_seconds)}s.md"
            path = chunk_dir / name
            body = f"# Chunk {chunk.index} ({chunk.start} – {chunk.end})\n\n{chunk.text}\n"
            path.write_text(body, encoding="utf-8")
            chunk_paths.append({"index": chunk.index, "start": chunk.start, "end": chunk.end, "file": f"chunks/{name}"})

    paths = {
        "agent": "agent.json",
        "agent_md": "agent.md",
        "transcript": "transcript.txt",
        "timestamped": "transcript_timestamped.txt",
    }
    if chunk_paths:
        paths["chunks"] = "chunks/"
    if segments:
        paths["segments"] = "segments.json"

    agent_payload = {
        "tool": "yt-transcriptor",
        **asdict(meta),
        "topic": topic,
        "language": language,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "duration_human": format_timestamp(meta.duration_seconds),
        "transcript_chars": len(clean_text),
        "removed_blocks_count": len(removed_blocks),
        "segment_count": len(segments) if segments else 0,
        "summary": summary,
        "chunks": chunk_paths,
        "paths": paths,
        "dir": str(out_dir.relative_to(PROJECT_ROOT)),
    }

    (out_dir / "agent.json").write_text(
        json.dumps(agent_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "agent.md").write_text(render_agent_md(summary, paths), encoding="utf-8")
    (out_dir / "transcript.txt").write_text(clean_text + "\n", encoding="utf-8")
    (out_dir / "transcript_timestamped.txt").write_text(timestamped_text + "\n", encoding="utf-8")

    if segments:
        # raw per-cue timestamps, machine-readable — the pointing source for video-QA
        (out_dir / "segments.json").write_text(
            json.dumps(
                [{"start": round(s.start, 2), "duration": round(s.duration, 2), "text": s.text} for s in segments],
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    if removed_blocks:
        (out_dir / "removed_noise.txt").write_text(
            "\n\n---\n\n".join(removed_blocks) + "\n",
            encoding="utf-8",
        )

    return out_dir


# Backwards-compatible alias
save_transcript_bundle = save_agent_bundle
