#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the weekly AI keyword radar.

The project intentionally keeps the runtime small: only PyYAML is external.
Everything else (RSS parsing, HTTP, JSON handling) uses the standard library.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
CHINA_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "spm",
    "scm",
    "from",
    "source",
    "ref",
    "refer",
    "share_token",
    "share_source",
    "wt_mc",
    "vero_id",
    "vero_conv",
    "igshid",
    "mkt_tok",
}

GENERIC_EMPTY_VALUES = {
    "无",
    "暂无",
    "没有",
    "n/a",
    "na",
    "none",
    "null",
    "-",
    "—",
    "[]",
    "{}",
    "待补充",
    "略",
}


def project_root() -> Path:
    return ROOT


def load_dotenv(path: Optional[Path] = None) -> None:
    """Load a minimal .env file without adding python-dotenv as a dependency.

    Existing environment variables always win, which makes CI secrets and
    one-off shell overrides behave as expected.
    """

    env_path = Path(path) if path else ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - covered by install docs
        raise RuntimeError(
            "缺少 PyYAML。请先运行：python3 -m pip install -r requirements.txt"
        ) from exc
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是对象：%s" % path)
    return data


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parse RFC 822, ISO 8601 and a few common Chinese date formats."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # RFC 822 / 2822, used by RSS pubDate.
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        pass

    # ISO 8601. Python 3.9 does not accept a trailing Z.
    iso_text = text.replace("Z", "+00:00")
    iso_text = re.sub(r"(\.\d{6})\d+", r"\1", iso_text)
    try:
        parsed = datetime.fromisoformat(iso_text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CHINA_TZ).astimezone(UTC)
        except ValueError:
            continue
    return None


def strip_html(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate(text: Any, limit: int, suffix: str = "…") -> str:
    text = strip_html(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def canonical_url(url: Any) -> str:
    if not url:
        return ""
    raw = html.unescape(str(url).strip())
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("www."):
        host = host[4:]
    if scheme in ("http", "https"):
        host = re.sub(r":(80|443)$", "", host)
    path = re.sub(r"/{2,}", "/", parts.path)
    if path.endswith("/amp"):
        path = path[:-4]
    if path != "/":
        path = path.rstrip("/")
    query_pairs: List[Tuple[str, str]] = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_PARAMS:
            continue
        query_pairs.append((key, val))
    query_pairs.sort()
    return urlunsplit((scheme, host, path, urlencode(query_pairs, doseq=True), ""))


def url_key(url: Any) -> str:
    canonical = canonical_url(url)
    if not canonical:
        return ""
    parts = urlsplit(canonical)
    return (parts.netloc + parts.path + ("?" + parts.query if parts.query else "")).lower()


def normalize_title(title: Any) -> str:
    text = strip_html(title)
    if not text:
        return ""
    # Remove a trailing publisher suffix such as " - 36氪" or " | TechCrunch".
    text = re.sub(r"\s*[-–—|_]\s*[^-–—|_]{1,18}$", "", text).strip()
    # Remove common leading labels.
    text = re.sub(r"^[【\[［][^】\]］]{0,20}[】\]］]\s*", "", text)
    text = unicodedata.normalize("NFKC", text).lower()
    chars: List[str] = []
    for char in text:
        if char.isalnum() or "\u4e00" <= char <= "\u9fff":
            chars.append(char)
        else:
            chars.append(" ")
    text = re.sub(r"\s+", " ", "".join(chars)).strip()
    return text


def title_shingles(normalized_title: str) -> Set[str]:
    compact = normalized_title.replace(" ", "")
    if not compact:
        return set()
    if len(compact) <= 2:
        return {compact}
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def jaccard(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def keyword_key(value: Any) -> str:
    text = normalize_title(value)
    return text.replace(" ", "")


def is_generic_empty(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return (not text) or text in GENERIC_EMPTY_VALUES


def match_terms(text: str, terms: Iterable[str]) -> bool:
    """Match ASCII terms on word boundaries and CJK terms as substrings."""

    haystack = (text or "").lower()
    if not haystack:
        return False
    for term in terms:
        needle = str(term).strip().lower()
        if not needle:
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9.+#_-]*", needle):
            pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
            if re.search(pattern, haystack):
                return True
        elif needle in haystack:
            return True
    return False


def contains_any_pattern(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text or "", flags=re.I) for pattern in patterns)


def safe_source_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value or "").strip("-").lower()
    return cleaned or "source"


def short_hash(value: str, length: int = 12) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]
