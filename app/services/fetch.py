from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str
    content_hash: str
    content_type: str
    raw: bytes
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)


class Fetcher:
    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (compatible; InternetBoard/0.4; local research dashboard)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }

    @staticmethod
    def _html_text(html: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            tag.decompose()
        candidates = []
        for selector in ("article", "main", ".article", ".content", ".detail", "#content", "#zoom"):
            for node in soup.select(selector):
                txt = node.get_text("\n", strip=True)
                if len(txt) > 300:
                    candidates.append(txt)
        text = max(candidates, key=len) if candidates else soup.get_text("\n", strip=True)
        return title, text

    async def fetch(self, url: str) -> FetchedPage:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=self.headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            raw = r.content
            ct = (r.headers.get("content-type") or "").lower()
            final_url = str(r.url)
            if "pdf" in ct or final_url.lower().split("?")[0].endswith(".pdf"):
                reader = PdfReader(io.BytesIO(raw))
                text = "\n".join((page.extract_text() or "") for page in reader.pages[:100])
                title = urlparse(final_url).path.rsplit("/", 1)[-1] or "PDF"
            else:
                title, text = self._html_text(r.text)
                title = title or final_url
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if not text:
                raise ValueError("empty page text")
            digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
            keep_headers = {}
            for k in ("content-type", "last-modified", "etag", "date", "cache-control"):
                if r.headers.get(k):
                    keep_headers[k] = r.headers[k]
            return FetchedPage(final_url, title[:500], text, digest, ct, raw, r.status_code, keep_headers)
