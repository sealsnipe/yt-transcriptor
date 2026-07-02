from __future__ import annotations

from dataclasses import asdict, dataclass

from lib.cleaner import clean_plain_text
from lib.fetcher import TranscriptSegment, format_timestamp


@dataclass
class TranscriptChunk:
    index: int
    start_seconds: float
    end_seconds: float
    start: str
    end: str
    text: str
    char_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def chunk_segments(
    segments: list[TranscriptSegment],
    *,
    chunk_seconds: int = 900,
    min_chars: int = 400,
) -> list[TranscriptChunk]:
    if not segments:
        return []

    chunks: list[TranscriptChunk] = []
    bucket: list[TranscriptSegment] = []
    bucket_start = segments[0].start

    def flush() -> None:
        nonlocal bucket, bucket_start
        if not bucket:
            return
        raw = " ".join(s.text.strip() for s in bucket if s.text.strip())
        clean, _ = clean_plain_text(raw)
        if len(clean) < min_chars:
            bucket = []
            return
        end_seconds = bucket[-1].start + bucket[-1].duration
        chunks.append(
            TranscriptChunk(
                index=len(chunks) + 1,
                start_seconds=bucket_start,
                end_seconds=end_seconds,
                start=format_timestamp(bucket_start),
                end=format_timestamp(end_seconds),
                text=clean,
                char_count=len(clean),
            )
        )
        bucket = []

    for seg in segments:
        if not bucket:
            bucket_start = seg.start
        bucket.append(seg)
        span = (seg.start + seg.duration) - bucket_start
        if span >= chunk_seconds:
            flush()
            if segments:
                bucket_start = seg.start + seg.duration

    flush()
    return chunks
