from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from youtube_transcript_api import YouTubeTranscriptApi


@dataclass
class VideoMeta:
    video_id: str
    title: str
    channel: str
    duration_seconds: int
    url: str


@dataclass
class TranscriptSegment:
    text: str
    start: float
    duration: float


def extract_video_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    patterns = [
        r"(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, s)
        if match:
            return match.group(1)
    return s


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fetch_transcript(
    video_id: str, languages: list[str] | None = None
) -> tuple[list[TranscriptSegment], str | None]:
    """Returns (segments, language_code) — the code is the track's ACTUAL language,
    needed so the json3 fallback doesn't pick an auto-translated track instead."""
    api = YouTubeTranscriptApi()
    if languages:
        result = api.fetch(video_id, languages=languages)
    else:
        result = api.fetch(video_id)
    language_code = getattr(result, "language_code", None)

    segments: list[TranscriptSegment] = []
    for seg in result:
        if hasattr(seg, "text"):
            segments.append(
                TranscriptSegment(text=seg.text, start=seg.start, duration=seg.duration)
            )
        else:
            segments.append(
                TranscriptSegment(
                    text=seg["text"],
                    start=seg["start"],
                    duration=seg.get("duration", 0),
                )
            )
    return segments, language_code


def fetch_subtitle_segments(
    video_id: str,
    languages: list[str] | None = None,
    prefer_language: str | None = None,
) -> list[TranscriptSegment]:
    """Fallback timestamp source: YouTube (auto-)subtitles via yt-dlp json3.

    The transcript API sometimes returns the whole video as one segment, which
    makes transcript_timestamped.txt useless for pointing a video-QA model at a
    time range. json3 subtitles always carry per-cue timestamps.

    prefer_language MUST be the video's original language when known: yt-dlp
    also downloads auto-TRANSLATED tracks (e.g. sub.de.json3 for an English
    video), and preferring the caller's UI language would silently replace the
    original transcript with a machine translation.
    """
    langs = languages or ["de", "en"]
    if prefer_language:
        langs = [prefer_language] + [l for l in langs if l != prefer_language]
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            sys.executable, "-m", "yt_dlp", "--skip-download", "--no-warnings",
            "--write-subs", "--write-auto-subs", "--sub-langs", ",".join(langs),
            "--sub-format", "json3", "-o", f"{tmp}/sub",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            return []
        files = list(Path(tmp).glob("sub.*.json3"))
        if not files:
            return []
        # prefer the caller's language order, else take what we got
        chosen = next(
            (f for lang in langs for f in files if f.name.startswith(f"sub.{lang}")),
            files[0],
        )
        try:
            data = json.loads(chosen.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    segments: list[TranscriptSegment] = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or [])
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                text=text,
                start=ev.get("tStartMs", 0) / 1000.0,
                duration=ev.get("dDurationMs", 0) / 1000.0,
            )
        )
    return segments


def fetch_video_meta(video_id: str) -> VideoMeta:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--no-warnings",
        "--print",
        "%(title)s\t%(channel)s\t%(duration)s",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        title, channel, duration = proc.stdout.strip().split("\t", 2)
        duration_seconds = int(duration or 0)
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        title = video_id
        channel = "unknown"
        duration_seconds = 0

    return VideoMeta(
        video_id=video_id,
        title=title,
        channel=channel,
        duration_seconds=duration_seconds,
        url=url,
    )


def segments_to_timestamped_text(segments: list[TranscriptSegment]) -> str:
    return "\n".join(
        f"{format_timestamp(seg.start)} {seg.text.strip()}" for seg in segments if seg.text.strip()
    )


def segments_to_plain_text(segments: list[TranscriptSegment]) -> str:
    return " ".join(seg.text.strip() for seg in segments if seg.text.strip())
