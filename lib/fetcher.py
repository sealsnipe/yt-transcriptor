from __future__ import annotations

import json
import os
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
    if _ytfetch_enabled():
        result = _ytfetch_subs(video_id, langs, prefer_language)
        if result is not None:
            # a decisive answer (segments, or [] for a clean notfound). Only a
            # transient ytfetch failure (None) falls through to direct yt-dlp.
            return result
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

    return _parse_json3(data)


def _parse_json3(data: dict) -> list[TranscriptSegment]:
    """Parse a YouTube json3 subtitle document into timed segments. Shared by the
    direct yt-dlp path and the ytfetch path so both produce identical output."""
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


def _ytfetch_subs_fetch(
    video_id: str, langs: list[str], prefer: str | None
) -> tuple[int, list[TranscriptSegment] | None, str | None]:
    """Run `ytfetch subs --format json3` and parse the json3 file it points to.

    Returns (returncode, segments, lang). segments is None on any transport or
    parse failure (returncode is forced to 1 in that case); lang is the track's
    actual language from ytfetch's {path,lang,auto} payload. ytfetch returns the
    exact same json3 yt-dlp produces and guarantees the track is the original
    language (never a machine translation; a miss is exit 4 = notfound).

    `prefer` ASSERTS the video's original language (a mismatch -> notfound), so
    pass it only when the original is already known. Pass None to let ytfetch
    pick the original track itself — the primary path doesn't know it up front.
    """
    cmd = ["ytfetch", "subs", video_id, "--langs", ",".join(langs), "--format", "json3"]
    if prefer:
        cmd += ["--prefer", prefer]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 1, None, None
    if proc.returncode != 0:
        return proc.returncode, None, None
    try:
        payload = json.loads(proc.stdout)
        data = json.loads(Path(payload["path"]).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, KeyError):
        return 1, None, None
    return 0, _parse_json3(data), payload.get("lang")


def fetch_captions(
    video_id: str, languages: list[str] | None = None
) -> tuple[list[TranscriptSegment], str | None]:
    """Primary transcript source via the shared ytfetch auth pool.

    Returns (segments, language_code), or ([], None) when ytfetch is disabled,
    unavailable, or has no acceptable original-language track — the caller then
    falls back to youtube_transcript_api. ytfetch routes json3 captions through
    the same cookies/po_token/player_client pool as fetch (far more robust
    against YouTube's bot wall than the bare, unauthenticated transcript API) and
    guarantees the track is the video's original language, never a translation.
    """
    if not _ytfetch_enabled():
        return [], None
    langs = languages or ["de", "en"]
    # No --prefer: we don't know the original language yet, so let ytfetch pick
    # the original track (it still refuses machine translations).
    rc, segments, lang = _ytfetch_subs_fetch(video_id, langs, None)
    if rc != 0 or not segments:
        return [], None
    return segments, lang


def _ytfetch_subs(
    video_id: str, langs: list[str], prefer_language: str | None
) -> list[TranscriptSegment] | None:
    """Fetch json3 subtitles via the shared ytfetch CLI (yt-video-engine).

    Returns parsed segments, [] on a decisive notfound (no acceptable
    original-language track — ytfetch refuses to hand back a machine-translated
    one), or None to signal a transient failure so the caller falls back to the
    direct yt-dlp path. Gated behind USE_YTFETCH.
    """
    # prefer_language is the known original here (from the transcript's language
    # code); pass it through as-is — None simply lets ytfetch pick the original.
    rc, segments, _ = _ytfetch_subs_fetch(video_id, langs, prefer_language)
    if rc == 4:  # notfound: no acceptable original-language subtitles
        return []
    if rc != 0 or segments is None:  # auth/ratelimit/unknown -> caller falls back
        return None
    return segments


_YTFETCH_OFF = {"0", "false", "off", "no"}


def _ytfetch_enabled() -> bool:
    """ytfetch (yt-video-engine) is the default fetch path; set USE_YTFETCH=0
    (or false/off/no) to force the legacy direct-yt-dlp path. Any ytfetch failure
    still degrades to direct yt-dlp regardless, so this is just an escape hatch."""
    return os.environ.get("USE_YTFETCH", "1").strip().lower() not in _YTFETCH_OFF


def _ytfetch_meta(video_id: str) -> dict | None:
    """Fetch title/channel/duration via the shared ytfetch CLI (yt-video-engine).

    Returns a dict of VideoMeta kwargs, or None to signal the caller to fall back
    to the direct yt-dlp path: any failure (missing binary, non-zero exit, bad
    JSON) degrades to yt-dlp.
    """
    try:
        proc = subprocess.run(
            ["ytfetch", "meta", video_id, "--fields", "title,channel,duration"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    # ytfetch normalises missing values to JSON null; duration arrives as a number.
    return {
        "title": data.get("title") or video_id,
        "channel": data.get("channel") or "unknown",
        "duration_seconds": int(data.get("duration") or 0),
    }


def fetch_video_meta(video_id: str) -> VideoMeta:
    url = f"https://www.youtube.com/watch?v={video_id}"
    if _ytfetch_enabled():
        meta = _ytfetch_meta(video_id)
        if meta is not None:
            return VideoMeta(video_id=video_id, url=url, **meta)
        # fall through to direct yt-dlp on any ytfetch failure
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
