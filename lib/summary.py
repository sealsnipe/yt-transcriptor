from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

# LLM bridge (tools/llm-ask.js) into the stockstuff OAuth-preset backend (one
# canonical token store). Heuristic summary remains the offline fallback.
LLM_BRIDGE = str(Path(__file__).resolve().parent.parent / "tools" / "llm-ask.js")
LLM_SYSTEM = (
    "Du fasst YouTube-Transkripte zusammen. Antworte NUR mit einem JSON-Objekt, "
    "ohne Markdown-Zaun: {\"brief\": \"2-3 Saetze Kernaussage\", "
    "\"key_points\": [\"5-10 konkrete Punkte mit Zahlen/Namen/Fakten aus dem Video\"], "
    "\"topics\": [\"2-5 Schlagworte\"]}. Schreibe auf Deutsch, behalte Fachbegriffe "
    "und woertliche Zitate im Original. Erfinde nichts, was nicht im Transkript steht."
)


STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "in", "on", "at", "to", "for", "of",
    "is", "are", "was", "were", "be", "been", "being", "it", "this", "that", "these",
    "those", "with", "as", "by", "from", "so", "we", "you", "they", "their", "our",
    "your", "i", "he", "she", "them", "his", "her", "its", "my", "me", "us", "do",
    "does", "did", "have", "has", "had", "can", "could", "will", "would", "should",
    "just", "like", "about", "into", "than", "then", "there", "here", "when", "what",
    "who", "how", "why", "all", "also", "very", "really", "now", "get", "got",
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "einem", "einen",
    "und", "oder", "aber", "wenn", "dass", "nicht", "sich", "ist", "sind", "war",
    "wird", "werden", "haben", "hat", "kann", "können", "muss", "mal", "halt", "eben",
    "ja", "ne", "also", "noch", "schon", "dann", "denn", "weil", "wie", "was", "wer",
    "wo", "zum", "zur", "vom", "von", "im", "am", "an", "aufs", "ins", "beim",
}

SKIP_HINTS = (
    "sponsor", "ltstore", "book a demo", "meter.com", "through time", "whoa",
    "throwback", "enjoyed this video", "gelächter", "tschüss", "wochenende",
    "pfingstferien", "mvp eventuell", "wow", "livestream zeit", "fun fact",
    "visual studio code", "meetup", "folie", "hammer abend", "apple store",
    "brutado", "pingpong", "geheime insides", "als erstes da", "wow wow",
    "chat ist", "hammer abend", "jubel", "apple store",
)

TECH_BOOST = (
    "agent framework", "semantic kernel", "autogen", "workflow", "systemprompt",
    "tool", "orchestrator", "azure", "csharp", "c#", "dotnet", ".net",
    "microsoft.extensions.ai", "chatcompletion", "middleware", "agent",
    "nachfolger", "release", "migration", "kernel",
    "thermal pad", "carbice", "carbise", "nano tube", "thermal cycles",
    "peel and stick", "burn-in", "anisotropic",
)

TOPIC_CHECKS = [
    ("Microsoft Agent Framework / Semantic Kernel", (
        "microsoft agent framework", "semantic kernel", "agent framework", "autogen",
    )),
    ("Thermische Pads / Noctua", ("thermal pad", "noctua", "carbice", "carbise", "nano tube")),
    ("Performance & Benchmarks", ("thermal cycles", "benchmark", "temperatur", "degrees")),
    ("Live-Stream / Q&A", ("livestream", "frage aus dem chat", "zuschauer")),
]

TOPIC_INTROS: list[tuple[tuple[str, ...], str]] = [
    (
        ("microsoft agent framework", "semantic kernel"),
        "Deep-Dive zum Microsoft Agent Framework (Nachfolger von Semantic Kernel/AutoGen) "
        "für agentische .NET/C#-Lösungen.",
    ),
    (
        ("thermal pad", "noctua", "carbice"),
        "Noctua/Carbice thermische Nanotube-Pads vs. klassische Paste: Installation, "
        "Performance, Burn-in und Grenzen.",
    ),
]


def _clean_sentence(sentence: str) -> str:
    sentence = re.sub(
        r"\[(?:gelächter|gelaechter|musik|applaus|music|laughter|jubel)\]",
        "",
        sentence,
        flags=re.I,
    )
    sentence = re.sub(r"(?i)\b(?:ähm|äh)\b", "", sentence)
    sentence = re.sub(r"\s+", " ", sentence).strip(" ,;")
    return sentence


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    cleaned = [_clean_sentence(p) for p in parts if len(p.strip()) > 30]
    return [p for p in cleaned if len(p) > 30]


def _sample_text(text: str, max_chars: int = 90000) -> str:
    if len(text) <= max_chars:
        return text
    chunk = max_chars // 4
    return "\n".join(
        [
            text[:chunk],
            text[len(text) // 3 : len(text) // 3 + chunk],
            text[(2 * len(text)) // 3 : (2 * len(text)) // 3 + chunk],
            text[-chunk:],
        ]
    )


def _detect_language_hint(text: str) -> str:
    sample = text[:5000].lower()
    de = len(re.findall(r"\b(der|die|das|und|nicht|wir|ich|eine|einen)\b", sample))
    en = len(re.findall(r"\b(the|and|not|we|you|this|that|with)\b", sample))
    return "de" if de > en else "en"


def _score_sentence(
    sentence: str,
    word_weights: Counter[str],
    index: int,
    total: int,
    *,
    skip_intro_ratio: float = 0.0,
) -> float:
    words = re.findall(r"[a-zäöüß0-9']+", sentence.lower())
    content_words = [w for w in words if w not in STOPWORDS and len(w) > 2]
    if not content_words:
        return 0.0

    keyword_score = sum(word_weights[w] for w in set(content_words))
    length = len(sentence)
    if 90 <= length <= 260:
        length_bonus = 2.0
    elif length <= 300:
        length_bonus = 0.5
    else:
        length_bonus = -4.0

    position = index / max(total - 1, 1)
    position_bonus = 1.0 - abs(position - 0.25)
    if skip_intro_ratio and position < skip_intro_ratio:
        position_bonus -= 3.0

    lower = sentence.lower()
    if any(x in lower for x in SKIP_HINTS):
        return 0.0
    penalty = 2.0 if lower.count(",") > 6 else 0.0
    tech_boost = sum(2.0 for term in TECH_BOOST if term in lower)
    return keyword_score + length_bonus + position_bonus + tech_boost - penalty


def _infer_topic_labels(text: str) -> list[str]:
    lower = text.lower()
    return [label for label, keywords in TOPIC_CHECKS if any(k in lower for k in keywords)]


def _compose_brief(text: str, title: str) -> str:
    lower = text.lower()
    for keywords, intro in TOPIC_INTROS:
        if any(k in lower for k in keywords):
            return intro

    early = _split_sentences(text[:12000])
    for sentence in early[:20]:
        if 60 <= len(sentence) <= 260 and _score_sentence(sentence, Counter(), 0, 10) > 0:
            return sentence
    return f"Video: {title}"


def _select_key_points(
    text: str,
    *,
    duration_seconds: int,
    max_points: int = 8,
) -> list[str]:
    sampled = _sample_text(text)
    sentences = _split_sentences(sampled)
    if not sentences:
        return []

    skip_intro = 0.08 if duration_seconds >= 3600 and "livestream" in text[:8000].lower() else 0.0

    words = re.findall(r"[a-zäöüß0-9']+", sampled.lower())
    weights = Counter(w for w in words if w not in STOPWORDS and len(w) > 3)

    scored = [
        (idx, sentence, _score_sentence(sentence, weights, idx, len(sentences), skip_intro_ratio=skip_intro))
        for idx, sentence in enumerate(sentences)
    ]
    scored.sort(key=lambda item: item[2], reverse=True)

    chosen: list[tuple[int, str]] = []
    chosen_idx: set[int] = set()
    for idx, sentence, score in scored:
        if len(chosen) >= max_points or score <= 0.5:
            continue
        if idx in chosen_idx or any(abs(idx - prev) < 3 for prev in chosen_idx):
            continue
        if len(sentence) > 300:
            continue
        trimmed = sentence
        if len(trimmed) > 260:
            cut = trimmed[:260].rfind(" ")
            trimmed = (trimmed[:cut] + "…") if cut > 120 else trimmed[:260] + "…"
        chosen.append((idx, trimmed))
        chosen_idx.add(idx)

    chosen.sort(key=lambda item: item[0])
    return [text for _, text in chosen]


def _llm_summary(text: str, title: str, channel: str) -> dict[str, Any]:
    """Summary via the LLM-preset bridge ('extraction' role). Raises on any problem."""
    user = f"Titel: {title}\nKanal: {channel}\n\nTranskript:\n{_sample_text(text, 40000)}"
    proc = subprocess.run(
        ["node", LLM_BRIDGE, "--role", "extraction", "--system", LLM_SYSTEM],
        input=user, capture_output=True, text=True, timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"llm bridge failed: {proc.stderr.strip()[:200]}")
    match = re.search(r"\{.*\}", proc.stdout, re.S)
    if not match:
        raise ValueError("no JSON in llm output")
    data = json.loads(match.group(0))
    brief = str(data.get("brief", "")).strip()
    key_points = [str(p).strip() for p in data.get("key_points", []) if str(p).strip()]
    topics = [str(t).strip() for t in data.get("topics", []) if str(t).strip()]
    if not brief or not key_points:
        raise ValueError("llm summary incomplete")
    return {"brief": brief, "key_points": key_points[:10], "topics": topics[:5]}


def build_agent_summary(
    text: str,
    title: str,
    channel: str,
    duration_seconds: int = 0,
    use_llm: bool = True,
) -> dict[str, Any]:
    llm: dict[str, Any] | None = None
    if use_llm and not os.environ.get("YT_NO_LLM") and text.strip():
        try:
            llm = _llm_summary(text, title, channel)
        except Exception:
            llm = None  # fail open: the heuristic below always produces something

    if llm:
        brief, key_points, topics = llm["brief"], llm["key_points"], llm["topics"]
    else:
        brief = _compose_brief(text, title)
        key_points = _select_key_points(text, duration_seconds=duration_seconds)
        topics = _infer_topic_labels(text)

    return {
        "title": title,
        "channel": channel,
        "brief": brief,
        "key_points": key_points,
        "topics": topics or ["Allgemein"],
        "language_hint": _detect_language_hint(text),
        "is_long_form": duration_seconds >= 1800,
        "duration_seconds": duration_seconds,
        "summary_source": "llm" if llm else "heuristic",
    }


def render_agent_md(summary: dict[str, Any], meta_paths: dict[str, str] | None = None) -> str:
    lines = [
        f"# {summary['title']}",
        "",
        f"**Kanal:** {summary['channel']}",
        "",
        "## Brief",
        "",
        summary["brief"],
        "",
        "## Key points",
        "",
    ]
    lines.extend(f"- {p}" for p in summary.get("key_points", []))
    lines.extend(["", "## Topics", ""])
    lines.extend(f"- {t}" for t in summary.get("topics", []))
    if meta_paths:
        lines.extend(["", "## Files", ""])
        for label, path in meta_paths.items():
            lines.append(f"- `{label}`: {path}")
    return "\n".join(lines) + "\n"


def build_summary(text: str, title: str, channel: str, max_points: int = 6) -> str:
    summary = build_agent_summary(text, title, channel)
    summary["key_points"] = _select_key_points(text, duration_seconds=0, max_points=max_points)
    return render_agent_md(summary)
