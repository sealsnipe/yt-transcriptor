from __future__ import annotations

import re


TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ai/microsoft-agent-framework", (
        "agent framework", "semantic kernel", "autogen", "ki agent", "ai agent",
    )),
    ("ai/general", ("generative", " large language", " llm", "openai", "chatgpt", "copilot")),
    ("hardware/thermal", ("thermal", "noctua", "carbice", "cpu cooler", "paste")),
    ("hardware/pc", ("gpu", "motherboard", "ram", "pc build", "linus tech")),
    ("dev/dotnet", ("c#", "csharp", ".net", "dotnet", "asp.net")),
    ("dev/general", ("programmier", "coding", "software", "developer")),
]


def infer_topic(title: str, text: str, fallback: str = "inbox") -> str:
    haystack = f"{title} {text[:8000]}".lower()
    for topic, keywords in TOPIC_RULES:
        if any(k in haystack for k in keywords):
            return topic
    return fallback
