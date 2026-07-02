from __future__ import annotations

from dataclasses import dataclass

from lib.chunks import TranscriptChunk, chunk_segments
from lib.cleaner import clean_plain_text, cleaned_timestamped_text
from lib.fetcher import (
    VideoMeta,
    extract_video_id,
    fetch_subtitle_segments,
    fetch_transcript,
    fetch_video_meta,
    segments_to_plain_text,
)
from lib.paths import PROJECT_ROOT
from lib.storage import save_agent_bundle
from lib.summary import build_agent_summary
from lib.topic import infer_topic


@dataclass
class ProcessResult:
    meta: VideoMeta
    topic: str
    clean_text: str
    removed_blocks: list[str]
    summary: dict
    chunks: list[TranscriptChunk]
    output_dir: str | None = None

    def stdout_payload(self) -> dict:
        return {
            "tool": "yt-transcriptor",
            "ok": True,
            "video_id": self.meta.video_id,
            "title": self.meta.title,
            "channel": self.meta.channel,
            "url": self.meta.url,
            "duration_seconds": self.meta.duration_seconds,
            "topic": self.topic,
            "dir": self.output_dir,
            "read_first": f"{self.output_dir}/agent.md" if self.output_dir else None,
            "agent_json": f"{self.output_dir}/agent.json" if self.output_dir else None,
            "brief": self.summary.get("brief"),
            "key_points": self.summary.get("key_points", []),
            "topics": self.summary.get("topics", []),
            "language": self.summary.get("language_hint"),
            "summary_source": self.summary.get("summary_source", "heuristic"),
            "long_form": self.summary.get("is_long_form", False),
            "chunk_count": len(self.chunks),
            "transcript_chars": len(self.clean_text),
        }


def process_video(
    url_or_id: str,
    *,
    topic: str | None = None,
    languages: list[str] | None = None,
    transcripts_path: str = "transcripts",
    save: bool = True,
    chunk_seconds: int = 900,
    use_llm: bool = True,
) -> ProcessResult:
    video_id = extract_video_id(url_or_id)
    meta = fetch_video_meta(video_id)
    segments = fetch_transcript(video_id, languages)

    # Some videos come back as one giant segment — useless for pointing a
    # video-QA model at a time range. Re-fetch per-cue timestamps via subtitles.
    if len(segments) <= 2 and meta.duration_seconds >= 180:
        fallback = fetch_subtitle_segments(video_id, languages)
        if len(fallback) > len(segments) * 3:
            segments = fallback

    raw_text = segments_to_plain_text(segments)
    clean_text, removed = clean_plain_text(raw_text)

    resolved_topic = topic or infer_topic(meta.title, clean_text)
    summary = build_agent_summary(
        clean_text, meta.title, meta.channel, meta.duration_seconds, use_llm=use_llm
    )
    chunks = (
        chunk_segments(segments, chunk_seconds=chunk_seconds)
        if meta.duration_seconds >= 1800
        else []
    )

    result = ProcessResult(
        meta=meta,
        topic=resolved_topic,
        clean_text=clean_text,
        removed_blocks=removed,
        summary=summary,
        chunks=chunks,
    )

    if save:
        out_dir = save_agent_bundle(
            topic=resolved_topic,
            meta=meta,
            clean_text=clean_text,
            timestamped_text=cleaned_timestamped_text(segments),
            summary=summary,
            chunks=chunks,
            removed_blocks=removed,
            language=",".join(languages) if languages else None,
            transcripts_path=transcripts_path,
            segments=segments,
        )
        result.output_dir = str(out_dir.relative_to(PROJECT_ROOT))

    return result
