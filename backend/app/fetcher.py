from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import fitz
import httpx
import trafilatura
from bs4 import BeautifulSoup, UnicodeDammit
from dateutil import parser as date_parser

from .config import settings
from .utils import canonicalize_url, is_private_url, normalize_space, safe_filename, sha256_bytes, sha256_text, utcnow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchedDocument:
    url: str
    canonical_url: str
    title: str
    text: str
    source_time: datetime | None
    mime_type: str
    raw_bytes: bytes
    raw_hash: str
    content_hash: str
    metadata: dict


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=utcnow().tzinfo)
        return dt
    except Exception:
        return None


def _extract_pdf(raw: bytes) -> tuple[str, str, dict]:
    doc = fitz.open(stream=raw, filetype="pdf")
    texts: list[str] = []
    for page in doc:
        texts.append(page.get_text("text"))
    meta = doc.metadata or {}
    title = normalize_space(meta.get("title") or "")
    return "\n\n".join(texts).strip(), title, meta


def _extract_html(raw: bytes, url: str) -> tuple[str, str, datetime | None, dict]:
    decoded = UnicodeDammit(raw, is_html=True).unicode_markup or raw.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(
        decoded,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        output_format="txt",
    )
    soup = BeautifulSoup(decoded, "html.parser")
    title = ""
    if soup.title:
        title = normalize_space(soup.title.get_text(" ", strip=True))
    if not extracted:
        for tag in soup(["script", "style", "noscript", "svg", "canvas", "nav", "footer"]):
            tag.decompose()
        extracted = soup.get_text("\n", strip=True)
    extracted = "\n".join(line.strip() for line in extracted.splitlines() if line.strip())

    published = None
    date_candidates = [
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"name": "date"}, "content"),
        ("meta", {"name": "pubdate"}, "content"),
        ("time", {}, "datetime"),
    ]
    for tag_name, attrs, attr in date_candidates:
        node = soup.find(tag_name, attrs=attrs)
        if node and node.get(attr):
            published = _parse_date(str(node.get(attr)))
            if published:
                break
    metadata = {
        "html_title": title,
        "language": soup.html.get("lang") if soup.html else None,
    }
    return extracted.strip(), title, published, metadata


def fetch_document(url: str) -> FetchedDocument:
    canonical = canonicalize_url(url)
    parsed = urlsplit(canonical)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    if not settings.allow_private_urls and is_private_url(canonical):
        raise ValueError("Private/local network URL blocked by ALLOW_PRIVATE_URLS=false")

    headers = {
        "User-Agent": settings.search_user_agent,
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    with httpx.Client(
        timeout=httpx.Timeout(settings.fetch_timeout_seconds, connect=15),
        follow_redirects=False,
        headers=headers,
    ) as client:
        current_url = canonical
        for _ in range(8):
            if not settings.allow_private_urls and is_private_url(current_url):
                raise ValueError("Private/local network redirect blocked by ALLOW_PRIVATE_URLS=false")
            with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        response.raise_for_status()
                    current_url = canonicalize_url(urljoin(current_url, location))
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > settings.max_fetch_bytes:
                    raise ValueError(f"Document exceeds MAX_FETCH_BYTES ({settings.max_fetch_bytes})")
                buffer = io.BytesIO()
                total = 0
                for chunk in response.iter_bytes(1024 * 256):
                    total += len(chunk)
                    if total > settings.max_fetch_bytes:
                        raise ValueError(f"Document exceeds MAX_FETCH_BYTES ({settings.max_fetch_bytes})")
                    buffer.write(chunk)
                raw = buffer.getvalue()
                final_url = canonicalize_url(str(response.url))
                mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
                break
        else:
            raise ValueError("Too many redirects while fetching document")

    if mime == "application/pdf" or raw.startswith(b"%PDF"):
        text, title, metadata = _extract_pdf(raw)
        source_time = _parse_date(metadata.get("creationDate") if isinstance(metadata, dict) else None)
        mime = "application/pdf"
    elif mime.startswith("text/html") or mime == "application/xhtml+xml" or b"<html" in raw[:4096].lower():
        text, title, source_time, metadata = _extract_html(raw, final_url)
        mime = "text/html"
    elif mime.startswith("text/") or mime in {"application/json", "application/xml", "application/rss+xml", "application/atom+xml"}:
        text = (UnicodeDammit(raw).unicode_markup or raw.decode("utf-8", errors="replace")).strip()
        title = Path(urlsplit(final_url).path).name or urlsplit(final_url).netloc
        source_time = None
        metadata = {}
    else:
        raise ValueError(f"Unsupported evidence MIME type: {mime}")

    text = text.strip()
    raw_hash = sha256_bytes(raw)
    if len(text) < 80:
        metadata = dict(metadata or {})
        metadata["extraction_warning"] = "Raw evidence archived, but less than 80 characters of analyzable text were extracted."
    content_hash = sha256_text(text) if text else raw_hash

    return FetchedDocument(
        url=url,
        canonical_url=final_url,
        title=normalize_space(title)[:1000],
        text=text,
        source_time=source_time,
        mime_type=mime,
        raw_bytes=raw,
        raw_hash=raw_hash,
        content_hash=content_hash,
        metadata=metadata,
    )


def archive_document(document: FetchedDocument, topic_id: int) -> str:
    if document.mime_type == "application/pdf":
        suffix = ".pdf"
    elif document.mime_type == "text/html":
        suffix = ".html"
    else:
        suffix = ".txt"
    stem = safe_filename(document.title or urlsplit(document.canonical_url).netloc)
    filename = f"t{topic_id}_{document.raw_hash[:12]}_{stem}{suffix}"
    target = settings.source_dir / filename
    if not target.exists():
        target.write_bytes(document.raw_bytes)
    return str(target)
