from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import unified_diff
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_KEYS = {
    "from", "source", "ref", "referer", "spm", "share", "share_source", "share_token",
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "mkt_tok", "igshid",
}


def canonicalize_url(url: str) -> str:
    """Remove common tracking noise without destroying meaningful query parameters."""
    try:
        p = urlsplit((url or "").strip())
        scheme = (p.scheme or "https").lower()
        host = (p.hostname or "").lower()
        if not host:
            return (url or "").strip()
        port = p.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        path = re.sub(r"/{2,}", "/", p.path or "/")
        items = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            lk = k.lower()
            if lk.startswith("utm_") or lk in _TRACKING_KEYS:
                continue
            items.append((k, v))
        items.sort(key=lambda x: (x[0], x[1]))
        return urlunsplit((scheme, netloc, path, urlencode(items, doseq=True), ""))
    except Exception:
        return (url or "").split("#", 1)[0].strip()


def _norm_text(text: str) -> str:
    text = re.sub(r"\s+", "", text or "").lower()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def simhash64(text: str) -> str:
    """Small dependency-free near-duplicate fingerprint for syndicated articles."""
    norm = _norm_text(text)
    if not norm:
        return "0" * 16
    width = 7 if len(norm) > 400 else 4
    feats = [norm[i : i + width] for i in range(0, max(1, len(norm) - width + 1), max(1, width // 2))]
    acc = [0] * 64
    for feat in feats[:12000]:
        h = int.from_bytes(hashlib.blake2b(feat.encode("utf-8", "ignore"), digest_size=8).digest(), "big")
        for bit in range(64):
            acc[bit] += 1 if (h >> bit) & 1 else -1
    value = 0
    for bit, score in enumerate(acc):
        if score >= 0:
            value |= 1 << bit
    return f"{value:016x}"


def hamming_hex(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return 64


def near_duplicate(a: str, b: str, max_bits: int = 5) -> bool:
    return bool(a and b) and hamming_hex(a, b) <= max_bits


def change_ratio(old_text: str, new_text: str) -> float:
    """Approximate changed fraction using normalized line/shingle sets; bounded 0..1."""
    a, b = _norm_text(old_text), _norm_text(new_text)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    width = 12
    sa = {a[i : i + width] for i in range(0, max(1, len(a) - width + 1), width)}
    sb = {b[i : i + width] for i in range(0, max(1, len(b) - width + 1), width)}
    union = len(sa | sb) or 1
    return round(1.0 - len(sa & sb) / union, 4)


def changed_excerpt(old_text: str, new_text: str, max_chars: int = 7000) -> str:
    """Keep only added/replaced textual lines for minor page updates."""
    old_lines = [x.strip() for x in (old_text or "").splitlines() if x.strip()]
    new_lines = [x.strip() for x in (new_text or "").splitlines() if x.strip()]
    diff = unified_diff(old_lines, new_lines, lineterm="")
    added = []
    for line in diff:
        if line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            value = line[1:].strip()
            if value:
                added.append(value)
        if sum(len(x) for x in added) >= max_chars:
            break
    return "\n".join(added)[:max_chars]


def source_group(canonical_url: str) -> str:
    return "src-" + hashlib.sha1(canonical_url.encode("utf-8", "ignore")).hexdigest()[:16]


@dataclass
class SourceDecision:
    canonical_url: str
    simhash: str
    change_kind: str
    change_ratio: float
    change_excerpt: str
    parent_evidence_id: int | None
    source_group_id: str
    duplicate_evidence_id: int | None = None


def classify_source(db, topic_slug: str, url: str, text: str, content_hash: str) -> SourceDecision:
    canonical = canonicalize_url(url)
    sh = simhash64(text)

    exact = db.get_evidence_by_hash(topic_slug, content_hash)
    if exact:
        return SourceDecision(canonical, sh, "exact-copy", 0.0, "", None, exact.get("source_group_id") or source_group(canonical), int(exact["id"]))

    previous = db.latest_evidence_for_url(topic_slug, canonical)
    if not previous:
        # v0.2/v0.3 rows did not have canonical_url; recognize them lazily without
        # forcing a one-shot migration over a potentially large evidence archive.
        for old in db.recent_evidence(topic_slug, 220):
            if canonicalize_url(old.get("url") or "") == canonical:
                previous = old
                break
    if previous:
        old_text = ""
        try:
            # Caller may replace this with archive text; excerpt is still enough for a conservative ratio fallback.
            old_text = previous.get("excerpt") or ""
        except Exception:
            pass
        ratio = change_ratio(old_text, text)
        delta = changed_excerpt(old_text, text)
        kind = "minor-update" if ratio <= 0.22 else "major-update"
        return SourceDecision(
            canonical, sh, kind, ratio, delta, int(previous["id"]),
            previous.get("source_group_id") or source_group(canonical), None,
        )

    # Cross-domain near-copy detection prevents syndicated copies from masquerading as independent corroboration.
    for old in db.recent_evidence(topic_slug, 220):
        old_sh = old.get("simhash") or simhash64(old.get("excerpt") or "")
        if old_sh and near_duplicate(sh, old_sh):
            return SourceDecision(
                canonical, sh, "syndicated-copy", 0.0, "", None,
                old.get("source_group_id") or source_group(old.get("canonical_url") or old.get("url") or canonical), int(old["id"]),
            )

    return SourceDecision(canonical, sh, "first-seen", 1.0, "", None, source_group(canonical), None)
