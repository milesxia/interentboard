from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import settings
from .utils import canonicalize_url, normalize_space

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    published: str = ""
    provider: str = ""


def _dedupe(items: list[SearchResult], limit: int) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for item in items:
        url = canonicalize_url(item.url)
        if not url or url in seen:
            continue
        seen.add(url)
        item.url = url
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _unwrap_ddg_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.netloc.endswith("duckduckgo.com"):
            uddg = parse_qs(parsed.query).get("uddg", [])
            if uddg:
                return unquote(uddg[0])
    except Exception:
        pass
    return url


def _search_searxng(query: str, limit: int) -> list[SearchResult]:
    if not settings.searxng_url:
        return []
    endpoint = settings.searxng_url.rstrip("/") + "/search"
    with httpx.Client(timeout=settings.search_timeout_seconds, follow_redirects=True) as client:
        response = client.get(endpoint, params={"q": query, "format": "json", "language": "auto"})
        response.raise_for_status()
        data = response.json()
    return [
        SearchResult(
            url=item.get("url", ""),
            title=normalize_space(item.get("title", "")),
            snippet=normalize_space(item.get("content", "")),
            provider="searxng",
        )
        for item in data.get("results", [])[:limit]
        if item.get("url")
    ]


def _search_duckduckgo(query: str, limit: int) -> list[SearchResult]:
    headers = {"User-Agent": settings.search_user_agent, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}
    with httpx.Client(timeout=settings.search_timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get("https://html.duckduckgo.com/html/", params={"q": query})
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[SearchResult] = []
    for node in soup.select(".result"):
        anchor = node.select_one("a.result__a")
        if not anchor or not anchor.get("href"):
            continue
        snippet = node.select_one(".result__snippet")
        results.append(
            SearchResult(
                url=_unwrap_ddg_url(str(anchor.get("href"))),
                title=normalize_space(anchor.get_text(" ", strip=True)),
                snippet=normalize_space(snippet.get_text(" ", strip=True) if snippet else ""),
                provider="duckduckgo",
            )
        )
        if len(results) >= limit:
            break
    return results


def _search_bing_news_rss(query: str, limit: int) -> list[SearchResult]:
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
    headers = {"User-Agent": settings.search_user_agent}
    with httpx.Client(timeout=settings.search_timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
    feed = feedparser.loads(response.text)
    results: list[SearchResult] = []
    for item in feed.entries[:limit]:
        link = item.get("link", "")
        if not link:
            continue
        results.append(
            SearchResult(
                url=link,
                title=normalize_space(html.unescape(item.get("title", ""))),
                snippet=normalize_space(BeautifulSoup(item.get("summary", ""), "html.parser").get_text(" ")),
                published=item.get("published", ""),
                provider="bing-news-rss",
            )
        )
    return results


def search_web(query: str, limit: int | None = None) -> list[SearchResult]:
    limit = limit or settings.max_results_per_query
    aggregated: list[SearchResult] = []
    providers = (_search_searxng, _search_duckduckgo, _search_bing_news_rss)
    for provider in providers:
        try:
            items = provider(query, limit)
            aggregated.extend(items)
            aggregated = _dedupe(aggregated, limit)
            if len(aggregated) >= limit:
                break
        except Exception as exc:
            logger.warning("Search provider %s failed for %r: %s", provider.__name__, query, exc)
    return _dedupe(aggregated, limit)
