from __future__ import annotations

import hashlib
from pathlib import Path


class BaselineStore:
    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest() if self.text else ""
        self.chunks = self._chunk(self.text)

    @staticmethod
    def _chunk(text: str, size: int = 1800, overlap: int = 200) -> list[str]:
        chunks = []
        start = 0
        while start < len(text):
            chunks.append(text[start : start + size])
            start += max(1, size - overlap)
        return chunks

    def relevant(self, keywords: list[str], limit: int = 8) -> str:
        scored = []
        for c in self.chunks:
            score = sum(c.count(k) for k in keywords if k)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return "\n\n---\n\n".join(c for _, c in scored[:limit])
