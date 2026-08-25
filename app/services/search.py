from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchOutcome:
    query: str
    hits: list[SearchHit]
    provider: str
    error: str = ""
    duration_ms: int = 0
    attempts: list[dict] = field(default_factory=list)


class Searcher:
    def __init__(self, max_results: int = 8, searxng_url: str = ""):
        self.max_results = max_results
        self.searxng_url = searxng_url.rstrip("/")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }
        # In-memory circuit breaker: a broken public search endpoint should not be
        # hammered once for every topic/query in the same run. Persistent metrics live in SQLite.
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    async def _guarded(self, provider: str, fn, query: str) -> SearchOutcome:
        now = time.monotonic()
        until = self._cooldown_until.get(provider, 0.0)
        if until > now:
            remain = int(until - now)
            return SearchOutcome(query, [], provider, f"circuit cooldown {remain}s", 0)
        out = await fn(query)
        if out.hits:
            self._failures[provider] = 0
            self._cooldown_until.pop(provider, None)
        elif out.error:
            n = self._failures.get(provider, 0) + 1
            self._failures[provider] = n
            # Fast recovery for a one-off error, but exponential backoff for repeated blocks/timeouts.
            self._cooldown_until[provider] = time.monotonic() + min(900, 30 * (2 ** min(n - 1, 5)))
        return out

    async def _searxng(self, query: str) -> SearchOutcome:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=self.headers) as client:
                r = await client.get(f"{self.searxng_url}/search", params={"q": query, "format": "json", "language": "zh-CN"})
                r.raise_for_status()
                hits = []
                for item in r.json().get("results", [])[: self.max_results]:
                    url = item.get("url") or ""
                    if urlparse(url).scheme in {"http", "https"}:
                        hits.append(SearchHit(item.get("title", ""), url, item.get("content", "")))
                return SearchOutcome(query, hits, "searxng", duration_ms=int((time.perf_counter()-start)*1000))
        except Exception as e:
            return SearchOutcome(query, [], "searxng", str(e), int((time.perf_counter()-start)*1000))

    async def _bing(self, query: str) -> SearchOutcome:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=self.headers) as client:
                r = await client.get("https://www.bing.com/search", params={"q": query, "count": self.max_results})
                r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            hits = []
            for li in soup.select("li.b_algo"):
                a = li.select_one("h2 a")
                if not a or not a.get("href"):
                    continue
                url = a.get("href")
                if urlparse(url).scheme not in {"http", "https"}:
                    continue
                p = li.select_one(".b_caption p")
                hits.append(SearchHit(a.get_text(" ", strip=True), url, p.get_text(" ", strip=True) if p else ""))
                if len(hits) >= self.max_results:
                    break
            return SearchOutcome(query, hits, "bing", duration_ms=int((time.perf_counter()-start)*1000))
        except Exception as e:
            return SearchOutcome(query, [], "bing", str(e), int((time.perf_counter()-start)*1000))

    async def _duckduckgo_html(self, query: str) -> SearchOutcome:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=self.headers) as client:
                r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
                r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            hits = []
            for result in soup.select(".result"):
                a = result.select_one("a.result__a")
                if not a or not a.get("href"):
                    continue
                url = a.get("href")
                if url.startswith("//duckduckgo.com/l/?"):
                    qs = parse_qs(urlparse("https:" + url).query)
                    url = unquote((qs.get("uddg") or [""])[0])
                if urlparse(url).scheme not in {"http", "https"}:
                    continue
                snippet = result.select_one(".result__snippet")
                hits.append(SearchHit(a.get_text(" ", strip=True), url, snippet.get_text(" ", strip=True) if snippet else ""))
                if len(hits) >= self.max_results:
                    break
            return SearchOutcome(query, hits, "duckduckgo-html", duration_ms=int((time.perf_counter()-start)*1000))
        except Exception as e:
            return SearchOutcome(query, [], "duckduckgo-html", str(e), int((time.perf_counter()-start)*1000))

    @staticmethod
    def _attempt(out: SearchOutcome) -> dict:
        return {
            "provider": out.provider,
            "hit_count": len(out.hits),
            "error": out.error,
            "duration_ms": out.duration_ms,
            "success": bool(out.hits) and not out.error,
        }

    async def search(self, query: str) -> SearchOutcome:
        attempts: list[dict] = []
        errors = []
        if self.searxng_url:
            out = await self._guarded("searxng", self._searxng, query)
            attempts.append(self._attempt(out))
            if out.hits:
                out.attempts = attempts
                return out
            if out.error:
                errors.append(f"searxng: {out.error}")
        out = await self._guarded("bing", self._bing, query)
        attempts.append(self._attempt(out))
        if out.hits:
            out.attempts = attempts
            return out
        if out.error:
            errors.append(f"bing: {out.error}")
        out2 = await self._guarded("duckduckgo-html", self._duckduckgo_html, query)
        attempts.append(self._attempt(out2))
        if out2.hits:
            out2.attempts = attempts
            return out2
        if out2.error:
            errors.append(f"ddg: {out2.error}")
        return SearchOutcome(query, [], "fallback", " | ".join(errors)[:1000], sum(x["duration_ms"] for x in attempts), attempts)
