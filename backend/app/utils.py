from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_entity_name(value: str) -> str:
    return normalize_space(value).casefold()[:300]


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    query = [(k, v) for k, v in query if k.lower() not in {"fbclid", "gclid", "spm", "from"}]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def is_private_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return True
        if host in {"localhost", "localhost.localdomain"}:
            return True
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)]
        return any(
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            for ip in addresses
        )
    except Exception:
        return True


def safe_filename(value: str, fallback: str = "document") -> str:
    value = normalize_space(value)
    value = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", value, flags=re.UNICODE)
    value = value.strip("._")
    return (value or fallback)[:120]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def estimate_tokens(text: str) -> int:
    # Conservative estimate for mixed Chinese/English text.
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = max(0, len(text) - cjk)
    return cjk + max(1, non_cjk // 4)
