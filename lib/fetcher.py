from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

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
) -> list[TranscriptSegment]:
    api = YouTubeTranscriptApi()
    if languages:
        result = api.fetch(video_id, languages=languages)
    else:
        result = api.fetch(video_id)

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
