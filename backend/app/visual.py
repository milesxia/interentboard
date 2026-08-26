from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import fitz
import httpx
from bs4 import BeautifulSoup, UnicodeDammit
from PIL import Image, UnidentifiedImageError

from .config import settings
from .fetcher import FetchedDocument
from .utils import canonicalize_url, is_private_url, safe_filename, sha256_bytes

logger = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = 50_000_000


@dataclass(slots=True)
class VisualAsset:
    kind: str
    source_url: str
    title: str
    mime_type: str
    data: bytes
    content_hash: str
    width: int
    height: int
    page_number: int | None = None
    alt_text: str = ""
    score: float = 0.0


_SKIP_HINTS = (
    "logo", "favicon", "avatar", "emoji", "icon-", "/icon", "sprite",
    "qrcode", "qr-code", "weixin", "wechat", "wx_code", "loading",
)
_VALUE_HINTS = (
    "规划", "示意", "地图", "区位", "路线", "线路", "表格", "数据", "图表",
    "进度", "效果图", "公告", "批复", "截图", "附图", "红线", "地块",
    "plan", "map", "diagram", "chart", "table", "route", "project",
)


def _image_score(*, width: int, height: int, url: str, alt: str) -> float:
    area = width * height
    score = min(5.0, area / 500_000.0)
    haystack = f"{url} {alt}".casefold()
    if any(item in haystack for item in _VALUE_HINTS):
        score += 5.0
    if width >= 900 or height >= 900:
        score += 1.5
    return score


def _normalise_image(raw: bytes, *, title: str, source_url: str, alt: str, kind: str, page_number: int | None = None) -> VisualAsset | None:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
            if width < settings.visual_min_width or height < settings.visual_min_height:
                return None
            if width * height < settings.visual_min_pixels:
                return None
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            elif image.mode == "L":
                image = image.convert("RGB")
            image.thumbnail((settings.visual_max_dimension, settings.visual_max_dimension), Image.Resampling.LANCZOS)
            width, height = image.size
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
            data = output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        logger.debug("Skipping unsupported image %s: %s", source_url, exc)
        return None
    return VisualAsset(
        kind=kind,
        source_url=source_url,
        title=title[:1000],
        mime_type="image/jpeg",
        data=data,
        content_hash=sha256_bytes(data),
        width=width,
        height=height,
        page_number=page_number,
        alt_text=alt[:1000],
        score=_image_score(width=width, height=height, url=source_url, alt=alt),
    )


def _fetch_binary(url: str, referer: str) -> bytes | None:
    current = canonicalize_url(url)
    if urlsplit(current).scheme not in {"http", "https"}:
        return None
    headers = {
        "User-Agent": settings.search_user_agent,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": referer,
    }
    with httpx.Client(
        timeout=httpx.Timeout(settings.visual_request_timeout_seconds, connect=12),
        follow_redirects=False,
        headers=headers,
    ) as client:
        for _ in range(6):
            if not settings.allow_private_urls and is_private_url(current):
                return None
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = canonicalize_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > settings.visual_max_image_bytes:
                    return None
                total = 0
                chunks: list[bytes] = []
                for chunk in response.iter_bytes(128 * 1024):
                    total += len(chunk)
                    if total > settings.visual_max_image_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    return None


def _decode_data_uri(value: str) -> bytes | None:
    if not value.startswith("data:image/") or "," not in value:
        return None
    header, payload = value.split(",", 1)
    if ";base64" not in header:
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    return raw if len(raw) <= settings.visual_max_image_bytes else None


def _src_from_img(node) -> str:
    for attr in ("data-original", "data-src", "data-lazy-src", "data-url", "src"):
        value = str(node.get(attr) or "").strip()
        if value:
            return value
    srcset = str(node.get("srcset") or "").strip()
    if srcset:
        parts = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
        if parts:
            return parts[-1]
    return ""


def _extract_html_assets(document: FetchedDocument) -> list[VisualAsset]:
    decoded = UnicodeDammit(document.raw_bytes, is_html=True).unicode_markup or document.raw_bytes.decode("utf-8", errors="replace")
    soup = BeautifulSoup(decoded, "html.parser")
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for node in soup.find_all("img"):
        src = _src_from_img(node)
        if not src:
            continue
        alt = " ".join(str(node.get("alt") or node.get("title") or "").split())
        lower = f"{src} {alt}".casefold()
        if any(hint in lower for hint in _SKIP_HINTS):
            continue
        if src.startswith("data:image/"):
            key = sha256_bytes(src.encode("utf-8", errors="ignore"))
            resolved = src
        else:
            resolved = canonicalize_url(urljoin(document.canonical_url, src))
            key = resolved
        if key in seen:
            continue
        seen.add(key)
        candidates.append((resolved, alt, str(node.get("title") or "")))
        if len(candidates) >= settings.visual_max_candidates_per_source:
            break

    assets: list[VisualAsset] = []
    for resolved, alt, title_attr in candidates:
        try:
            raw = _decode_data_uri(resolved) if resolved.startswith("data:image/") else _fetch_binary(resolved, document.canonical_url)
            if not raw:
                continue
            title = alt or title_attr or f"{document.title} visual evidence"
            asset = _normalise_image(raw, title=title, source_url=resolved if not resolved.startswith("data:") else document.canonical_url, alt=alt, kind="html_image")
            if asset:
                assets.append(asset)
        except Exception as exc:
            logger.debug("Image candidate failed %s: %s", resolved[:300], exc)
    assets.sort(key=lambda item: item.score, reverse=True)
    return assets[: settings.visual_max_assets_per_source]


def _extract_pdf_assets(document: FetchedDocument) -> list[VisualAsset]:
    doc = fitz.open(stream=document.raw_bytes, filetype="pdf")
    page_candidates: list[tuple[float, int, str]] = []
    for index, page in enumerate(doc):
        text = page.get_text("text").strip()
        embedded = page.get_images(full=True)
        large_image_count = sum(
            1 for item in embedded
            if len(item) > 3
            and int(item[2] or 0) >= settings.visual_min_width
            and int(item[3] or 0) >= settings.visual_min_height
            and int(item[2] or 0) * int(item[3] or 0) >= settings.visual_min_pixels
        )
        score = 0.0
        if len(text) <= settings.visual_pdf_text_threshold:
            score += 4.0
        if large_image_count:
            score += min(4.0, 1.5 + large_image_count)
        if any(hint in text.casefold() for hint in _VALUE_HINTS):
            score += 2.0
        if score > 0:
            page_candidates.append((score, index, text[:1000]))
    page_candidates.sort(key=lambda item: (-item[0], item[1]))

    assets: list[VisualAsset] = []
    for score, index, text_hint in page_candidates[: settings.visual_pdf_max_pages]:
        page = doc[index]
        pix = page.get_pixmap(matrix=fitz.Matrix(settings.visual_pdf_scale, settings.visual_pdf_scale), alpha=False)
        raw = pix.tobytes("png")
        source_url = f"{document.canonical_url}#page={index + 1}"
        asset = _normalise_image(
            raw,
            title=f"{document.title or 'PDF'} - page {index + 1}",
            source_url=source_url,
            alt=text_hint,
            kind="pdf_page",
            page_number=index + 1,
        )
        if asset:
            asset.score += score
            assets.append(asset)
    assets.sort(key=lambda item: item.score, reverse=True)
    return assets[: settings.visual_max_assets_per_source]


def extract_visual_assets(document: FetchedDocument) -> list[VisualAsset]:
    if not settings.visual_enabled:
        return []
    if document.mime_type == "application/pdf":
        return _extract_pdf_assets(document)
    if document.mime_type == "text/html":
        return _extract_html_assets(document)
    return []


def archive_visual_asset(asset: VisualAsset, topic_id: int) -> str:
    suffix = ".jpg"
    page = f"_p{asset.page_number}" if asset.page_number else ""
    stem = safe_filename(asset.title or asset.kind, fallback="visual")
    target = settings.visual_dir / f"t{topic_id}_{asset.content_hash[:16]}{page}_{stem}{suffix}"
    if not target.exists():
        target.write_bytes(asset.data)
    return str(target)
