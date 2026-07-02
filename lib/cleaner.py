from __future__ import annotations

import html
import re

from lib.fetcher import TranscriptSegment, format_timestamp

NOISE_PATTERNS = [
    r"\[music\]",
    r"\[musik\]",
    r"\[applause\]",
    r"\[applaus\]",
    r"\[laughter\]",
    r"\[gelächter\]",
    r"\[gelaechter\]",
    r"\[jubel\]",
    r"\[silence\]",
    r"\[inaudible\]",
    r"\[unverständlich\]",
    r"♪+",
    r"🎵+",
]

REMOVED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "sponsor_intro",
        re.compile(
            r"(?is),\s*and tell you about our sponsor.*?(?:book a demo\.?\s*)"
        ),
    ),
    (
        "sponsor_meter",
        re.compile(
            r"(?is)\s*and you know what else is everywhere\?\s*"
            r"(?:this message from our sponsor\s*>>?\s*)?"
            r".*?book a demo\.?\s*"
        ),
    ),
    (
        "sponsor_meter_fragment",
        re.compile(
            r"(?is)\s*(?:as your business scales|meter builds enterprise).*?book a demo\.?\s*"
        ),
    ),
    (
        "sponsor_outro",
        re.compile(
            r"(?is)\s*(?:man, no is freaking everywhere these days\.\s*)?"
            r"(?:now\.\s*)?that'?s me\s*\.?\s*t?\.?com/ltt.*?book a demo\.?\s*"
        ),
    ),
    (
        "outro_en",
        re.compile(
            r"(?is)\s*if you guys enjoyed this video.*?overdue for another\s*"
        ),
    ),
    (
        "outro_de",
        re.compile(
            r"(?is)\s*(?:ja, es war mir eine ehre.*?"
            r"|ich wünsche euch (?:was|ein schönes wochenende).*?"
            r"(?:danke euch\.?\s*)?(?:tschüss\.?\s*){0,3})$"
        ),
    ),
    (
        "skit",
        re.compile(
            r"(?is)\s*to find out, we need to travel through time\.\s*"
            r"(?:through time[,.]?\s*)+"
            r"whoa, i'?m in the future now\.\s*"
        ),
    ),
]

LTSTORE_REF = re.compile(r"(?i)\s*available on ltstore\.com")
BROKEN_JOIN = re.compile(r"(?i)from Noctua Carbon")
FILLER_TOKENS = re.compile(r"(?i)\b(?:ähm|äh|hm+|also\s+ja|na\s+ja)\b")


def _normalize_spaces(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text


def _remove_noise(text: str) -> str:
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = FILLER_TOKENS.sub(" ", text)
    return _normalize_spaces(text)


def _dedupe_repeated_phrases(text: str) -> str:
    text = re.sub(
        r"\b(\w+(?: \w+){0,2}) \1\b",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    return _normalize_spaces(text)


def _strip_promotional_content(text: str) -> tuple[str, list[str]]:
    removed: list[str] = []
    for _ in range(4):
        changed = False
        for label, pattern in REMOVED_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            removed.append(f"[{label}] {_normalize_spaces(match.group(0))}")
            text = text[: match.start()] + " " + text[match.end() :]
            changed = True
        text = _normalize_spaces(text)
        if not changed:
            break

    text = BROKEN_JOIN.sub("from Noctua.", text)
    text = LTSTORE_REF.sub("", text)
    text = re.sub(r"(?i)\s*and\s*\.\s*", " ", text)
    text = re.sub(r"(?i)\s*this\s*\.\s*", " ", text)
    text = _normalize_spaces(text)
    return text, removed


# Auto-captions carry no sentence punctuation, so a punctuation-only split merges
# the whole video into one line; force a flush so timestamped lines stay usable
# for pointing a video-QA model at a time range.
_MAX_MERGED_CHARS = 400
_MAX_SILENCE_GAP = 4.0


def _merge_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    if not segments:
        return []

    merged: list[TranscriptSegment] = []
    current = TranscriptSegment(
        text=segments[0].text.strip(),
        start=segments[0].start,
        duration=segments[0].duration,
    )

    for seg in segments[1:]:
        piece = seg.text.strip()
        if not piece:
            continue

        prev = current.text.rstrip()
        gap = seg.start - (current.start + current.duration)
        if prev and (prev[-1] in ".!?" or len(prev) >= _MAX_MERGED_CHARS or gap > _MAX_SILENCE_GAP):
            merged.append(current)
            current = TranscriptSegment(text=piece, start=seg.start, duration=seg.duration)
            continue

        # caption cues are word-aligned: always join with a space (gluing produced
        # artifacts like "andmade"); only a trailing hyphen continues a word
        joiner = "" if prev.endswith("-") else " "
        current = TranscriptSegment(
            text=f"{prev}{joiner}{piece}",
            start=current.start,
            duration=(seg.start + seg.duration) - current.start,
        )

    if current.text.strip():
        merged.append(current)
    return merged


def _is_promotional_segment(text: str) -> bool:
    lower = text.lower()
    hints = (
        "sponsor",
        "meter.com",
        "meter handles",
        "meter will",
        "meter builds enterprise",
        "book a demo",
        "throwback",
        "enjoyed this video",
        "travel through time",
        "whoa, i'm in the future",
        "as your business scales",
        "ich wünsche euch ein schönes wochenende",
    )
    return any(h in lower for h in hints)


def _strip_leading_garbage(text: str) -> str:
    match = re.search(r"[A-Za-zÄÖÜäöüß]", text)
    if match and match.start() > 0:
        return text[match.start():]
    return text


def clean_plain_text(text: str) -> tuple[str, list[str]]:
    text = _remove_noise(text)
    text = _strip_leading_garbage(text)
    text = _dedupe_repeated_phrases(text)
    text, removed = _strip_promotional_content(text)
    text = _normalize_spaces(text)
    return text, removed


def cleaned_timestamped_text(segments: list[TranscriptSegment]) -> str:
    merged = _merge_segments(segments)
    lines: list[str] = []
    for seg in merged:
        piece = seg.text.strip()
        if not piece or _is_promotional_segment(piece):
            continue
        piece = _remove_noise(piece)
        piece = LTSTORE_REF.sub("", piece)
        piece = _normalize_spaces(piece)
        if piece:
            lines.append(f"{format_timestamp(seg.start)} {piece}")
    return "\n\n".join(lines)
