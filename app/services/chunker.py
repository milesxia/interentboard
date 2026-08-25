from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for Chinese/English mixed text.

    It deliberately over-estimates a little so requests stay below Ollama's
    context budget. The exact tokenizer is model-specific, so the final guard
    is Ollama's `truncate=false` rather than silently dropping input.
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    non_cjk = max(0, len(text) - cjk)
    # Chinese is close to 1 char/token; Latin text averages ~4 chars/token.
    base = cjk + (non_cjk + 3) // 4
    return max(1, int(base * 1.12) + 8)


@dataclass(frozen=True)
class TextChunk:
    index: int
    total: int
    text: str
    token_estimate: int
    content_hash: str


def _split_oversized_paragraph(paragraph: str, max_tokens: int) -> list[str]:
    if estimate_tokens(paragraph) <= max_tokens:
        return [paragraph]
    # Sentence-aware split first.
    pieces = [x.strip() for x in re.split(r"(?<=[。！？!?；;\.])\s*", paragraph) if x.strip()]
    if len(pieces) <= 1:
        # Fallback: use a conservative character window for unbroken text.
        chars = max(300, int(max_tokens * 1.8))
        return [paragraph[i : i + chars] for i in range(0, len(paragraph), chars)]

    out: list[str] = []
    buf: list[str] = []
    for piece in pieces:
        candidate = "".join(buf + [piece])
        if buf and estimate_tokens(candidate) > max_tokens:
            out.append("".join(buf).strip())
            buf = [piece]
        else:
            buf.append(piece)
    if buf:
        out.append("".join(buf).strip())
    # A sentence itself can still be huge.
    final: list[str] = []
    for p in out:
        if estimate_tokens(p) <= max_tokens:
            final.append(p)
        else:
            chars = max(300, int(max_tokens * 1.8))
            final.extend(p[i : i + chars] for i in range(0, len(p), chars))
    return [x for x in final if x.strip()]


def _tail_for_overlap(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0 or not text:
        return ""
    paragraphs = [x.strip() for x in text.split("\n\n") if x.strip()]
    keep: list[str] = []
    total = 0
    for p in reversed(paragraphs):
        t = estimate_tokens(p)
        if keep and total + t > overlap_tokens:
            break
        keep.append(p)
        total += t
        if total >= overlap_tokens:
            break
    if not keep:
        # Approximate only for a very large single paragraph.
        chars = max(80, int(overlap_tokens * 1.5))
        return text[-chars:]
    return "\n\n".join(reversed(keep))


def split_text(text: str, max_tokens: int = 2400, overlap_tokens: int = 180) -> list[TextChunk]:
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    if max_tokens < 256:
        raise ValueError("max_tokens must be >= 256")
    overlap_tokens = max(0, min(overlap_tokens, max_tokens // 4))

    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    paragraphs: list[str] = []
    for p in raw_paragraphs:
        paragraphs.extend(_split_oversized_paragraph(p, max_tokens))

    chunks: list[str] = []
    buf: list[str] = []
    for p in paragraphs:
        candidate = "\n\n".join(buf + [p]) if buf else p
        if buf and estimate_tokens(candidate) > max_tokens:
            current = "\n\n".join(buf).strip()
            chunks.append(current)
            overlap = _tail_for_overlap(current, overlap_tokens)
            buf = ([overlap] if overlap else []) + [p]
            # If overlap pushes the next chunk over budget, drop overlap first.
            if estimate_tokens("\n\n".join(buf)) > max_tokens:
                buf = [p]
        else:
            buf.append(p)
    if buf:
        chunks.append("\n\n".join(buf).strip())

    total = len(chunks)
    result: list[TextChunk] = []
    for i, chunk in enumerate(chunks):
        digest = hashlib.sha256(chunk.encode("utf-8", errors="ignore")).hexdigest()
        result.append(TextChunk(i, total, chunk, estimate_tokens(chunk), digest))
    return result


def pack_jsonish(items: list[str], max_tokens: int) -> list[list[str]]:
    """Pack already-serialized compact items into token-budgeted batches."""
    batches: list[list[str]] = []
    current: list[str] = []
    for item in items:
        if not current:
            current = [item]
            continue
        candidate = "\n".join(current + [item])
        if estimate_tokens(candidate) > max_tokens:
            batches.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        batches.append(current)
    return batches
