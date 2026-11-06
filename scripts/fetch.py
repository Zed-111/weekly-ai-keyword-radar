#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Layer 1: fetch RSS/Atom feeds, normalise, filter and de-duplicate.

The output is deliberately boring and machine-readable. Editorial judgement
belongs to the LLM layer, not here. This layer only answers:

  * what was published in the time window?
  * which items are plausibly about AI?
  * which items are the same story repeated by different feeds?

Usage:
    python scripts/fetch.py --days 7
    python scripts/fetch.py --days 7 --config config/sources.yaml --output output/raw.json
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402  (path is set above)

DEFAULT_AI_TERMS = [
    "人工智能",
    "大模型",
    "语言模型",
    "基础模型",
    "多模态",
    "生成式",
    "AIGC",
    "Agent",
    "Agentic",
    "智能体",
    "参数",
    "权重",
    "开放权重",
    "机器人",
    "具身",
    "人形",
    "灵巧手",
    "VLA",
    "世界模型",
    "3D生成",
    "视频生成",
    "图像生成",
    "语音生成",
    "数字人",
    "虚拟人",
    "算力",
    "芯片",
    "GPU",
    "数据中心",
    "开源模型",
    "Hugging Face",
    "GPT",
    "Claude",
    "Gemini",
    "DeepSeek",
    "Kimi",
    "豆包",
    "通义",
    "文心",
    "混元",
    "星火",
    "Llama",
    "Mistral",
    "Sora",
    "自动驾驶",
    "智能驾驶",
    "仿真",
    "AI",
    "AGI",
]

DEFAULT_EXCLUDE_PATTERNS = [
    r"招聘",
    r"诚聘",
    r"活动报名",
    r"直播预告",
    r"课程报名",
    r"培训班",
    r"优惠券",
    r"促销",
    r"抽奖",
]

SIGNAL_PATTERNS = [
    r"\d",
    r"\d+(?:\.\d+)?\s*(?:%|亿|万|倍|美元|元|台|小时|分钟|秒|token|参数)",
    r"首次|开源|发布|突破|收购|融资|IPO|起诉|禁止|警告|裁员|翻车|篡改|终止|拒绝|争议|事故|召回",
]

INTERVIEW_PATTERN = r"采访|专访|对话|访谈|座谈|演讲|播客|问答|对谈|内部信|炉边"
CREATOR_PATTERN = r"B站|哔哩哔哩|小红书|抖音|UP主|up主|博主|手搓|涨粉|播放量|爆款|短剧|漫剧|二创|整活"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(element: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if _local(child.tag) in wanted:
            text = common.strip_html("".join(child.itertext()))
            if text:
                return text
    return ""


def _first_attr(element: ET.Element, names: Iterable[str], attr: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if _local(child.tag) in wanted:
            value = child.attrib.get(attr, "").strip()
            if value:
                return value
    return ""


def _parse_xml(data: bytes) -> ET.Element:
    raw = data.lstrip()
    head = raw[:512].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise ValueError("返回的是 HTML 而不是 RSS/Atom")
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        text = raw.decode("utf-8", errors="ignore")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        try:
            return ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise ValueError("XML 解析失败：%s" % exc) from exc


def _find_entries(root: ET.Element) -> Tuple[List[ET.Element], str]:
    root_name = _local(root.tag)
    feed_title = _first_text(root, ["title"])
    if root_name == "rss":
        channel = None
        for child in root:
            if _local(child.tag) == "channel":
                channel = child
                break
        if channel is None:
            raise ValueError("RSS 缺少 channel")
        feed_title = _first_text(channel, ["title"]) or feed_title
        entries = [child for child in channel if _local(child.tag) == "item"]
        return entries, feed_title
    if root_name == "feed":
        entries = [child for child in root if _local(child.tag) == "entry"]
        return entries, feed_title
    if root_name == "rdf":
        entries = [child for child in root if _local(child.tag) == "item"]
        return entries, feed_title
    # Some feeds wrap <channel> directly.
    for child in root:
        if _local(child.tag) == "channel":
            entries = [item for item in child if _local(item.tag) == "item"]
            if entries:
                return entries, _first_text(child, ["title"]) or feed_title
    raise ValueError("不支持的 XML 根节点：%s" % root.tag)


def _entry_link(entry: ET.Element, base_url: str) -> str:
    best = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        rel = (child.attrib.get("rel") or "alternate").lower()
        text = common.strip_html("".join(child.itertext()))
        candidate = href or text
        if not candidate:
            continue
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        candidate = urljoin(base_url, candidate)
        if rel in ("alternate", ""):
            return candidate
        if not best:
            best = candidate
    if best:
        return best
    for name in ("guid", "id"):
        value = _first_text(entry, [name])
        if value.startswith("http://") or value.startswith("https://"):
            return value
    return ""


def _entry_date(entry: ET.Element) -> Optional[datetime]:
    for name in ("pubdate", "published", "updated", "date", "dc:date", "created", "issued", "modified"):
        value = _first_text(entry, [name])
        if value:
            parsed = common.parse_datetime(value)
            if parsed:
                return parsed
    return None


def _entry_summary(entry: ET.Element) -> str:
    for name in ("description", "summary", "encoded", "content", "subtitle"):
        value = _first_text(entry, [name])
        if value:
            return common.truncate(value, 1200)
    return ""


def _entry_author(entry: ET.Element) -> str:
    for name in ("author", "creator"):
        value = _first_text(entry, [name])
        if value:
            return common.truncate(value, 80)
    return ""


def _entry_publisher(entry: ET.Element, fallback: str) -> str:
    value = _first_text(entry, ["source"])
    return common.truncate(value, 80) if value else fallback


def _google_news_url(source: Dict[str, Any]) -> str:
    query = str(source.get("query") or "").strip()
    hl = str(source.get("hl") or "zh-CN")
    gl = str(source.get("gl") or "CN")
    ceid = str(source.get("ceid") or ("CN:zh-Hans" if hl.startswith("zh") else "US:en"))
    return "https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s" % (
        quote_plus(query),
        quote_plus(hl),
        quote_plus(gl),
        quote_plus(ceid),
    )


def _source_url(source: Dict[str, Any]) -> str:
    if source.get("type") == "google_news":
        return _google_news_url(source)
    return str(source.get("url") or "").strip()


def _http_get(url: str, timeout: int, retries: int, user_agent: str) -> bytes:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    last_error: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("响应超过 8MB，已跳过")
                if "text/html" in content_type and "xml" not in content_type:
                    # Some servers lie about content type; let the XML parser
                    # make the final call for known-feed URLs.
                    if b"<rss" not in data[:1024].lower() and b"<feed" not in data[:1024].lower():
                        raise ValueError("Content-Type 为 HTML，跳过")
                return data
        except HTTPError as exc:
            last_error = exc
            if exc.code in (401, 403, 404, 410):
                break
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1) + random.random())
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1) + random.random())
    raise RuntimeError(str(last_error) if last_error else "未知网络错误")


def parse_feed(
    data: bytes,
    source: Dict[str, Any],
    fetched_at: datetime,
    since: datetime,
    until: datetime,
    include_undated: bool = False,
    ai_terms: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Parse one feed and return (filtered items, raw entry count)."""

    root = _parse_xml(data)
    entries, feed_title = _find_entries(root)
    base_url = _source_url(source)
    source_name = str(source.get("name") or feed_title or source.get("id") or "Unknown")
    source_id = str(source.get("id") or common.safe_source_id(source_name))
    weight = float(source.get("weight", 1.0))
    lang = str(source.get("lang") or "zh")
    tags = [str(tag) for tag in (source.get("tags") or [])]
    ai_only = bool(source.get("ai_only", False))
    ai_terms = ai_terms or DEFAULT_AI_TERMS
    exclude_patterns = exclude_patterns or DEFAULT_EXCLUDE_PATTERNS

    items: List[Dict[str, Any]] = []
    for entry in entries:
        title = common.strip_html(_first_text(entry, ["title"]))
        link = _entry_link(entry, base_url)
        summary = _entry_summary(entry)
        published = _entry_date(entry)
        publisher = _entry_publisher(entry, source_name)

        # Google News appends " - Publisher" to titles. Remove it when the
        # publisher is available as a separate field.
        if publisher and title.endswith(" - " + publisher):
            title = title[: -(len(publisher) + 3)].strip()
        if not title:
            continue

        text = title + " " + summary
        if common.contains_any_pattern(title, exclude_patterns):
            continue
        if not ai_only and not common.match_terms(text, ai_terms):
            continue
        if published:
            if published < since - timedelta(hours=1) or published > until + timedelta(hours=6):
                continue
        elif not include_undated:
            continue

        canonical = common.canonical_url(link)
        identity = common.url_key(link) or common.normalize_title(title)
        item_id = common.short_hash(identity + "|" + common.normalize_title(title))
        items.append(
            {
                "id": item_id,
                "title": common.truncate(title, 180),
                "url": canonical or link,
                "source": publisher,
                "source_id": source_id,
                "feed_name": source_name,
                "published_at": common.iso_z(published) if published else None,
                "date_confidence": "high" if published else "unknown",
                "summary": summary,
                "author": _entry_author(entry),
                "lang": lang,
                "tags": tags,
                "weight": weight,
                "rank_score": 0.0,
                "also_seen_in": [],
                "fetched_at": common.iso_z(fetched_at),
            }
        )

    # Keep the freshest and most information-dense items per feed.
    items.sort(key=lambda item: (item.get("published_at") or "", len(item.get("summary") or "")), reverse=True)
    max_items = source.get("max_items")
    if max_items:
        items = items[: int(max_items)]
    return items, len(entries)


def _item_quality(item: Dict[str, Any]) -> Tuple[int, int, int]:
    published = common.parse_datetime(item.get("published_at"))
    return (
        1 if published else 0,
        int(float(item.get("weight", 1.0)) * 100),
        len(item.get("summary") or ""),
    )


def _merge_item(winner: Dict[str, Any], other: Dict[str, Any]) -> Dict[str, Any]:
    if other.get("source") and other.get("source") != winner.get("source"):
        seen = set(winner.get("also_seen_in") or [])
        seen.add(other.get("source"))
        seen.add(other.get("feed_name"))
        winner["also_seen_in"] = sorted(value for value in seen if value)
    tags = set(winner.get("tags") or []) | set(other.get("tags") or [])
    winner["tags"] = sorted(tags)
    if len(other.get("summary") or "") > len(winner.get("summary") or ""):
        winner["summary"] = other.get("summary")
    if not winner.get("published_at") and other.get("published_at"):
        winner["published_at"] = other["published_at"]
        winner["date_confidence"] = "high"
    if len(other.get("title") or "") > len(winner.get("title") or "") and len(winner.get("title") or "") < 24:
        winner["title"] = other["title"]
    return winner


def dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Two-pass de-duplication: canonical URL, then near-duplicate title."""

    ranked = sorted(items, key=_item_quality, reverse=True)
    by_url: Dict[str, Dict[str, Any]] = {}
    for item in ranked:
        key = common.url_key(item.get("url") or "") or common.normalize_title(item.get("title") or "")
        if not key:
            continue
        if key in by_url:
            by_url[key] = _merge_item(by_url[key], item)
        else:
            by_url[key] = item

    unique: List[Dict[str, Any]] = []
    for item in sorted(by_url.values(), key=_item_quality, reverse=True):
        title = common.normalize_title(item.get("title") or "")
        shingles = common.title_shingles(title)
        duplicate: Optional[Dict[str, Any]] = None
        for existing in unique:
            existing_title = common.normalize_title(existing.get("title") or "")
            if not title or not existing_title:
                continue
            if title == existing_title:
                duplicate = existing
                break
            if abs(len(title) - len(existing_title)) > max(10, int(0.35 * max(len(title), len(existing_title)))):
                continue
            existing_shingles = common.title_shingles(existing_title)
            similarity = common.jaccard(shingles, existing_shingles)
            intersection = len(shingles & existing_shingles)
            shortest = max(1, min(len(shingles), len(existing_shingles)))
            if similarity >= 0.90 or (shortest >= 8 and intersection / shortest >= 0.92):
                duplicate = existing
                break
        if duplicate:
            _merge_item(duplicate, item)
        else:
            unique.append(item)
    return unique


def _score_item(item: Dict[str, Any]) -> float:
    weight = float(item.get("weight", 1.0))
    published = common.parse_datetime(item.get("published_at"))
    if published:
        age_hours = max(0.0, (common.now_utc() - published).total_seconds() / 3600.0)
        if age_hours <= 24:
            recency = 1.0
        elif age_hours <= 72:
            recency = 0.8
        elif age_hours <= 24 * 7:
            recency = 0.6
        else:
            recency = 0.3
    else:
        recency = 0.3

    title = item.get("title") or ""
    signal = 0.0
    if re.search(SIGNAL_PATTERNS[0], title):
        signal += 0.12
    if re.search(SIGNAL_PATTERNS[1], title, flags=re.I):
        signal += 0.20
    if re.search(SIGNAL_PATTERNS[2], title):
        signal += 0.18
    if re.search(INTERVIEW_PATTERN, title):
        signal += 0.35
    if re.search(CREATOR_PATTERN, title):
        signal += 0.30
    if len(item.get("summary") or "") >= 120:
        signal += 0.08
    return round(weight * 2.0 + recency + signal, 4)


def fetch_source(
    source: Dict[str, Any],
    timeout: int,
    retries: int,
    user_agent: str,
    fetched_at: datetime,
    since: datetime,
    until: datetime,
    include_undated: bool,
    ai_terms: List[str],
    exclude_patterns: List[str],
) -> Dict[str, Any]:
    source_id = str(source.get("id") or "")
    url = _source_url(source)
    result: Dict[str, Any] = {
        "id": source_id,
        "name": source.get("name") or source_id,
        "type": source.get("type") or "rss",
        "status": "failed",
        "fetched": 0,
        "filtered": 0,
        "kept": 0,
        "error": "",
        "items": [],
    }
    if not url:
        result["error"] = "缺少 url/query"
        return result
    try:
        effective_timeout = int(source.get("timeout") or timeout)
        data = _http_get(url, timeout=effective_timeout, retries=retries, user_agent=user_agent)
        items, entry_count = parse_feed(
            data=data,
            source=source,
            fetched_at=fetched_at,
            since=since,
            until=until,
            include_undated=include_undated,
            ai_terms=ai_terms,
            exclude_patterns=exclude_patterns,
        )
        result["fetched"] = entry_count
        result["filtered"] = len(items)
        result["items"] = items
        result["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
        result["error"] = common.truncate(str(exc), 300)
    return result


def _load_config(path: Path) -> Dict[str, Any]:
    config = common.load_yaml(path)
    if not isinstance(config.get("sources"), list):
        raise ValueError("sources.yaml 缺少 sources 列表")
    return config


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="抓取过去 N 天的 AI 信源并去重")
    parser.add_argument("--days", type=int, default=7, help="时间窗口天数，默认 7")
    parser.add_argument("--since", help="起始时间（ISO 8601），覆盖 --days")
    parser.add_argument("--until", help="结束时间（ISO 8601），默认当前时间")
    parser.add_argument("--config", default="config/sources.yaml", help="信源配置")
    parser.add_argument("--output", default="output/raw.json", help="输出文件")
    parser.add_argument("--max-per-source", type=int, help="覆盖每个信源的最大条数")
    parser.add_argument("--include-undated", action="store_true", help="保留没有发布时间的条目")
    parser.add_argument("--timeout", type=int, help="单请求超时秒数")
    parser.add_argument("--workers", type=int, help="并发数")
    parser.add_argument("--source", action="append", help="只抓取指定 source id，可重复")
    parser.add_argument("--list-sources", action="store_true", help="列出启用的信源后退出")
    parser.add_argument("--allow-empty", action="store_true", help="没有条目时仍返回 0")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    common.load_dotenv()
    config_path = Path(args.config)
    if not config_path.exists():
        print("[fetch] 找不到配置文件：%s" % config_path, file=sys.stderr)
        return 1
    try:
        config = _load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        print("[fetch] 配置读取失败：%s" % exc, file=sys.stderr)
        return 1

    fetch_cfg = config.get("fetch") or {}
    sources = [source for source in config.get("sources", []) if source.get("enabled", True)]
    if args.source:
        wanted = set(args.source)
        sources = [source for source in sources if source.get("id") in wanted]
    if not sources:
        print("[fetch] 没有启用的信源", file=sys.stderr)
        return 1
    if args.list_sources:
        for source in sources:
            print("%-28s %-12s %s" % (source.get("id"), source.get("type", "rss"), source.get("name")))
        return 0

    until = common.parse_datetime(args.until) if args.until else common.now_utc()
    since = common.parse_datetime(args.since) if args.since else None
    if until is None:
        print("[fetch] --until 时间格式无效", file=sys.stderr)
        return 1
    if since is None:
        since = until - timedelta(days=max(1, args.days))
    if since >= until:
        print("[fetch] 起始时间必须早于结束时间", file=sys.stderr)
        return 1

    timeout = int(args.timeout or fetch_cfg.get("timeout_seconds") or 20)
    retries = int(fetch_cfg.get("retries") or 2)
    workers = int(args.workers or fetch_cfg.get("max_workers") or 6)
    include_undated = bool(args.include_undated or fetch_cfg.get("include_undated", False))
    ai_terms = list(fetch_cfg.get("ai_filter") or DEFAULT_AI_TERMS)
    exclude_patterns = list(fetch_cfg.get("exclude_patterns") or DEFAULT_EXCLUDE_PATTERNS)
    user_agent = str(fetch_cfg.get("user_agent") or "WeeklyAIKeywordRadar/1.0 (+https://github.com/)")

    if args.max_per_source:
        for source in sources:
            source["max_items"] = args.max_per_source
    elif fetch_cfg.get("max_items_per_source") and not any(source.get("max_items") for source in sources):
        for source in sources:
            source["max_items"] = int(fetch_cfg["max_items_per_source"])

    fetched_at = common.now_utc()
    print(
        "[fetch] %d 个信源，窗口 %s ~ %s"
        % (len(sources), since.astimezone(common.CHINA_TZ).strftime("%Y-%m-%d %H:%M"), until.astimezone(common.CHINA_TZ).strftime("%Y-%m-%d %H:%M"))
    )

    source_results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                fetch_source,
                source,
                timeout,
                retries,
                user_agent,
                fetched_at,
                since,
                until,
                include_undated,
                ai_terms,
                exclude_patterns,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {
                    "id": source.get("id"),
                    "name": source.get("name"),
                    "type": source.get("type", "rss"),
                    "status": "failed",
                    "fetched": 0,
                    "filtered": 0,
                    "kept": 0,
                    "error": common.truncate(str(exc), 300),
                    "items": [],
                }
            source_results.append(result)
            if args.verbose:
                if result["status"] == "ok":
                    print("[ok]   %-28s %3d/%3d 条" % (result["id"], result["filtered"], result["fetched"]))
                else:
                    print("[fail] %-28s %s" % (result["id"], result["error"]))

    all_items: List[Dict[str, Any]] = []
    for result in source_results:
        all_items.extend(result.pop("items", []))

    unique = dedupe_items(all_items)
    for item in unique:
        item["rank_score"] = _score_item(item)
    unique.sort(key=lambda item: (item.get("rank_score") or 0, item.get("published_at") or ""), reverse=True)

    kept_by_source: Dict[str, int] = {}
    for item in unique:
        sid = item.get("source_id") or ""
        kept_by_source[sid] = kept_by_source.get(sid, 0) + 1
    for result in source_results:
        result["kept"] = kept_by_source.get(result["id"], 0)

    ok_count = sum(1 for result in source_results if result["status"] == "ok")
    failed_count = len(source_results) - ok_count
    output = {
        "schema_version": 1,
        "generated_at": common.iso_z(fetched_at),
        "window": {
            "days": args.days,
            "since": common.iso_z(since),
            "until": common.iso_z(until),
            "timezone": "Asia/Shanghai",
        },
        "stats": {
            "sources_configured": len(sources),
            "sources_ok": ok_count,
            "sources_failed": failed_count,
            "items_fetched": len(all_items),
            "items_after_dedupe": len(unique),
            "duplicates_removed": max(0, len(all_items) - len(unique)),
        },
        "sources": sorted(source_results, key=lambda item: item.get("id") or ""),
        "items": unique,
    }

    output_path = Path(args.output)
    common.write_json(output_path, output)
    print(
        "[fetch] %d 条原始 → %d 条去重，%d/%d 信源成功 → %s"
        % (len(all_items), len(unique), ok_count, len(source_results), output_path)
    )
    failures = [result for result in source_results if result["status"] != "ok"]
    if failures and args.verbose:
        for result in failures:
            print("        %s: %s" % (result["id"], result["error"]))
    if not unique and not args.allow_empty:
        print("[fetch] 没有抓到有效条目。请检查网络、时间窗口或信源配置。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
