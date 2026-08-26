from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .config import settings
from .utils import estimate_tokens, sha256_text


@dataclass(slots=True)
class TextChunk:
    index: int
    content: str
    content_hash: str
    char_count: int
    token_estimate: int
    relevance_score: float = 0.0


def _split_units(text: str) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= settings.chunk_chars:
            units.append(paragraph)
            continue
        sentences = re.split(r"(?<=[。！？.!?])\s*", paragraph)
        buffer = ""
        for sentence in sentences:
            if not sentence:
                continue
            if buffer and len(buffer) + len(sentence) + 1 > settings.chunk_chars:
                units.append(buffer.strip())
                buffer = sentence
            else:
                buffer = f"{buffer}\n{sentence}" if buffer else sentence
        if buffer.strip():
            units.append(buffer.strip())
    return units


def chunk_text(text: str) -> list[TextChunk]:
    units = _split_units(text)
    chunks: list[TextChunk] = []
    current = ""
    idx = 0

    def emit(value: str) -> None:
        nonlocal idx
        value = value.strip()
        if len(value) < settings.min_chunk_chars:
            return
        chunks.append(
            TextChunk(
                index=idx,
                content=value,
                content_hash=sha256_text(value),
                char_count=len(value),
                token_estimate=estimate_tokens(value),
            )
        )
        idx += 1

    for unit in units:
        if not current:
            current = unit
            continue
        if len(current) + len(unit) + 2 <= settings.chunk_chars:
            current += "\n\n" + unit
            continue
        emit(current)
        overlap = current[-settings.chunk_overlap_chars :] if settings.chunk_overlap_chars > 0 else ""
        current = (overlap + "\n\n" + unit).strip()
        while len(current) > settings.chunk_chars * 2:
            emit(current[: settings.chunk_chars])
            current = current[settings.chunk_chars - settings.chunk_overlap_chars :]
    if current:
        emit(current)

    if not chunks and text.strip():
        emit(text[: settings.chunk_chars])
    return chunks


def relevance_score(text: str, query: str, chunk_index: int) -> float:
    qterms = {t.casefold() for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)}
    hay = text.casefold()
    score = 0.0
    for term in qterms:
        count = hay.count(term)
        if count:
            score += 1.0 + math.log1p(count)
    if chunk_index == 0:
        score += 1.5
    elif chunk_index == 1:
        score += 0.5
    return round(score, 4)
