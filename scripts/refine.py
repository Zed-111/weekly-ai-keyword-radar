#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Layer 2: turn raw items into the fixed weekly keyword JSON.

Default flow:
  1. rank and select a diverse subset of the week's items;
  2. ask the LLM to extract evidence-backed candidates in chunks;
  3. ask the LLM to merge/dedupe candidates into the output schema;
  4. normalise, validate, write JSON + a human-readable Markdown digest.

No API key? Use --no-llm for an explicitly-labelled extractive fallback.

Usage:
    python scripts/refine.py --input output/raw.json --feedback config/feedback.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
from validate import validate_document  # noqa: E402

INTERVIEW_RE = re.compile(r"采访|专访|对话|访谈|座谈|演讲|播客|问答|对谈|内部信|炉边")
CREATOR_RE = re.compile(r"B站|哔哩哔哩|小红书|抖音|UP主|up主|博主|手搓|涨粉|播放量|爆款|短剧|漫剧|二创|整活")
EMBODIED_RE = re.compile(r"具身|人形机器人|机器人|VLA|灵巧手|关节|遥操作|产线|上岗|作业")
OPEN_SOURCE_RE = re.compile(r"开源|开放权重|权重|Hugging Face|GitHub|Star|许可证", re.I)
WORLD_MODEL_RE = re.compile(r"世界模型|空间智能|物理仿真|3D生成|4D|实时交互")
RESEARCH_RE = re.compile(r"论文|研究|实验|benchmark|评测|数据集", re.I)
GENERIC_MARKETING_RE = re.compile(r"重磅|炸裂|震惊|刷屏|王炸|史诗级|遥遥领先")

FALLBACK_VIRAL = [
    "突发",
    "重磅",
    "炸裂",
    "实测",
    "刚刚",
    "开源",
    "首次",
    "反超",
    "临界点",
    "上岗",
    "量产",
    "翻车",
    "篡改",
    "终止",
    "封锁",
    "起诉",
    "内幕",
    "曝光",
    "刷屏",
    "一人公司",
    "ChatGPT时刻",
    "最后几毫米",
    "万亿美元",
]

FALLBACK_CREATOR = [
    "手搓",
    "赛马",
    "抽卡",
    "错题本",
    "一人公司",
    "超级个体",
    "驯服AI",
    "翻车",
    "不塌房的IP",
    "赛博宠物",
    "猫味儿",
    "材料学博士",
    "AI自己刷B站",
]


class LLMError(RuntimeError):
    pass


def _read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _schema_categories(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    categories = []
    for key, spec in (schema.get("properties") or {}).items():
        categories.append(
            {
                "key": key,
                "title": spec.get("title") or key,
                "description": spec.get("description") or "",
                "max_items": int(spec.get("maxItems") or 10),
                "max_length": int(((spec.get("items") or {}).get("maxLength")) or 80),
            }
        )
    return categories


def _category_block(categories: List[Dict[str, Any]]) -> str:
    lines = []
    for category in categories:
        lines.append("- `%s`（%s，最多 %d 条）：%s" % (
            category["key"],
            category["title"],
            category["max_items"],
            category["description"],
        ))
    return "\n".join(lines)


def _feedback_block(feedback: Dict[str, Any]) -> str:
    editorial = feedback.get("editorial") or {}
    learned = feedback.get("learned_rules") or []
    payload = {
        "editorial": editorial,
        "learned_rules": learned,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _item_text(item: Dict[str, Any]) -> str:
    return "%s %s" % (item.get("title") or "", item.get("summary") or "")


def _recency_score(item: Dict[str, Any]) -> float:
    published = common.parse_datetime(item.get("published_at"))
    if not published:
        return 0.25
    age_hours = max(0.0, (common.now_utc() - published).total_seconds() / 3600.0)
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.8
    if age_hours <= 24 * 7:
        return 0.6
    return 0.3


def _boosts(item: Dict[str, Any], feedback: Dict[str, Any]) -> float:
    text = _item_text(item)
    boost_terms = feedback.get("boost_terms") or {}
    watchlist = feedback.get("watchlist") or {}

    score = 0.0
    if INTERVIEW_RE.search(text):
        score += 0.90
    if CREATOR_RE.search(text):
        score += 0.55
    if EMBODIED_RE.search(text):
        score += 0.35
    if OPEN_SOURCE_RE.search(text):
        score += 0.30
    if WORLD_MODEL_RE.search(text):
        score += 0.25
    if RESEARCH_RE.search(text):
        score += 0.15
    if GENERIC_MARKETING_RE.search(item.get("title") or ""):
        score -= 0.35

    for key, weight in (("interview", 0.35), ("creator", 0.25), ("embodied", 0.15), ("open_source", 0.15), ("world_model", 0.15)):
        terms = boost_terms.get(key) or []
        if common.match_terms(text, terms):
            score += weight

    people = watchlist.get("people") or []
    companies = watchlist.get("companies") or []
    creators = watchlist.get("creators") or []
    hits = 0
    for term in list(people) + list(companies) + list(creators):
        if term and str(term).lower() in text.lower():
            hits += 1
    score += min(0.60, hits * 0.12)
    return score


def _selection_score(item: Dict[str, Any], feedback: Dict[str, Any]) -> float:
    base = float(item.get("rank_score") or 0.0) * 0.55
    source_weights = feedback.get("source_weights") or {}
    weight = float(source_weights.get(item.get("source_id") or "", item.get("weight") or 1.0))
    score = base + weight * 1.1 + _recency_score(item) + _boosts(item, feedback)
    summary = item.get("summary") or ""
    if len(summary) >= 100:
        score += 0.10
    if len(item.get("title") or "") >= 12:
        score += 0.05
    return round(score, 4)


def select_items(items: List[Dict[str, Any]], feedback: Dict[str, Any], max_items: int) -> List[Dict[str, Any]]:
    """Select a diverse, high-signal subset with reserves for special sections."""

    if max_items <= 0:
        return []
    scored = [( _selection_score(item, feedback), item) for item in items if item.get("title")]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    selection_cfg = feedback.get("selection") or {}
    per_source_cap = int(selection_cfg.get("max_per_source") or 18)
    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    source_counts: Dict[str, int] = defaultdict(int)

    def add(item: Dict[str, Any]) -> bool:
        if len(selected) >= max_items:
            return False
        item_id = item.get("id")
        if item_id in selected_ids:
            return False
        source_key = item.get("source_id") or item.get("feed_name") or "unknown"
        if source_counts[source_key] >= per_source_cap:
            return False
        selected.append(item)
        selected_ids.add(item_id)
        source_counts[source_key] += 1
        return True

    # Reserve slots for the sections that are easy to lose in a pure heat sort.
    reserves = (
        (lambda text: bool(INTERVIEW_RE.search(text)), 12),
        (lambda text: bool(CREATOR_RE.search(text)), 18),
        (lambda text: bool(EMBODIED_RE.search(text)), 20),
        (lambda text: bool(OPEN_SOURCE_RE.search(text)), 12),
        (lambda text: bool(WORLD_MODEL_RE.search(text)), 8),
    )
    for predicate, quota in reserves:
        taken = 0
        for _, item in scored:
            if len(selected) >= max_items or taken >= quota:
                break
            if predicate(_item_text(item)) and add(item):
                taken += 1

    for _, item in scored:
        if len(selected) >= max_items:
            break
        add(item)

    selected.sort(key=lambda item: _selection_score(item, feedback), reverse=True)
    return selected


def _format_items(items: List[Dict[str, Any]], summary_limit: int = 260) -> List[Dict[str, Any]]:
    formatted = []
    for item in items:
        formatted.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "source": item.get("source"),
                "published": (item.get("published_at") or "")[:10],
                "tags": (item.get("tags") or [])[:5],
                "summary": common.truncate(item.get("summary") or "", summary_limit),
            }
        )
    return formatted


class LLMClient:
    """Small OpenAI-compatible / Anthropic-compatible chat client."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        provider: str = "openai",
        timeout: int = 180,
        temperature: float = 0.2,
        max_tokens: int = 8000,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider.lower().strip()
        self.timeout = timeout
        self.temperature = temperature
        self.default_max_tokens = max_tokens
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.calls = 0

    @classmethod
    def from_env(cls) -> "LLMClient":
        api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        if not api_key:
            raise LLMError("缺少 LLM_API_KEY。请设置 .env 或环境变量，或使用 --no-llm。")
        provider = os.getenv("LLM_PROVIDER", "openai")
        default_base = "https://api.anthropic.com/v1/messages" if provider == "anthropic" else "https://api.openai.com/v1"
        base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or default_base
        model = os.getenv("LLM_MODEL") or ("claude-sonnet-4-20250514" if provider == "anthropic" else "deepseek-chat")
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider=provider,
            timeout=int(os.getenv("LLM_TIMEOUT", "180")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8000")),
        )

    def _endpoint(self) -> str:
        if self.provider == "anthropic":
            if self.base_url.endswith("/messages"):
                return self.base_url
            if self.base_url.endswith("/v1"):
                return self.base_url + "/messages"
            return self.base_url + "/v1/messages" if not self.base_url.endswith("/anthropic.com/v1") else self.base_url + "/messages"
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def _payload(self, messages: List[Dict[str, str]], max_tokens: int, json_mode: bool) -> Dict[str, Any]:
        if self.provider == "anthropic":
            system_parts = [message["content"] for message in messages if message.get("role") == "system"]
            chat_messages = [message for message in messages if message.get("role") != "system"]
            return {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": self.temperature,
                "system": "\n\n".join(system_parts),
                "messages": chat_messages,
            }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.provider == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = "Bearer " + self.api_key
        last_error: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                request = Request(self._endpoint(), data=body, headers=headers, method="POST")
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                error_body = ""
                try:
                    error_body = exc.read().decode("utf-8", errors="ignore")[:800]
                except Exception:  # noqa: BLE001
                    pass
                last_error = LLMError("HTTP %s: %s" % (exc.code, error_body))
                if exc.code in (401, 403, 404):
                    break
                if exc.code == 400 and "response_format" in error_body:
                    # Some OpenAI-compatible providers do not support JSON mode.
                    payload.pop("response_format", None)
                    continue
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(2.0 * (attempt + 1))
        raise LLMError(str(last_error) if last_error else "LLM 请求失败")

    def chat(self, messages: List[Dict[str, str]], max_tokens: Optional[int] = None, json_mode: bool = True) -> str:
        payload = self._payload(messages, max_tokens or self.default_max_tokens, json_mode)
        data = self._post(payload)
        self.calls += 1
        usage = data.get("usage") or {}
        for key in self.usage:
            self.usage[key] += int(usage.get(key) or 0)
        if self.provider == "anthropic":
            content = data.get("content") or []
            if content and isinstance(content, list):
                text = "".join(part.get("text") or "" for part in content if isinstance(part, dict))
                if text:
                    return text
            raise LLMError("Anthropic 响应缺少 content")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("LLM 响应缺少 choices：%s" % common.truncate(json.dumps(data, ensure_ascii=False), 500))
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text") or "" for part in content if isinstance(part, dict))
        if not content:
            raise LLMError("LLM 返回空内容")
        return content


def extract_json(text: str) -> Any:
    if not text:
        raise LLMError("LLM 返回空内容")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    raise LLMError("LLM 没有返回合法 JSON：%s" % common.truncate(cleaned, 500))


def _system_prompt(guide: str, categories: List[Dict[str, Any]], feedback: Dict[str, Any]) -> str:
    return (
        guide
        + "\n\n# 本周字段清单\n"
        + _category_block(categories)
        + "\n\n# 本周编辑偏好（来自 config/feedback.yaml）\n"
        + _feedback_block(feedback)
        + "\n\n再次强调：只使用输入材料中的事实；输出严格 JSON。"
    )


def _map_prompt(items: List[Dict[str, Any]], categories: List[Dict[str, Any]], feedback: Dict[str, Any]) -> str:
    return (
        "下面是本周的一部分信源条目。请从中提取“候选关键词”，不要直接写最终周报。\n\n"
        "候选格式：\n"
        '{"category":"schema 里的字段名","keyword":"压缩后的事件短语","evidence":["条目 id"],'
        '"entities":["人物/公司/模型"],"numbers":["硬数字"],"tension":"对抗点或反差","heat":1到5的整数}\n\n'
        "要求：\n"
        "1. 每个板块最多 12 条候选；只提取有信息增量的，不要凑数。\n"
        "2. 同一事件被多条材料报道时，合并成一条候选，evidence 放多个 id。\n"
        "3. 不要写新闻标题；不要使用输入中没有的人物、数字或事件。\n"
        "4. 优先具体人物、硬数字、对抗性张力；没有就降低 heat，不要编。\n"
        "5. 没有合格候选的板块直接省略。\n\n"
        "板块定义：\n"
        + _category_block(categories)
        + "\n\n编辑偏好：\n"
        + _feedback_block(feedback)
        + "\n\n本周条目：\n"
        + json.dumps(_format_items(items), ensure_ascii=False)
        + "\n\n只输出 JSON：{\"candidates\":[...]}"
    )


def _final_prompt(
    candidates: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    categories: List[Dict[str, Any]],
    feedback: Dict[str, Any],
) -> str:
    by_category: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_category[candidate["category"]].append(candidate)
    compact_candidates = []
    for category in categories:
        values = sorted(by_category.get(category["key"], []), key=lambda item: (item.get("heat") or 0, len(item.get("evidence") or [])), reverse=True)[:12]
        compact_candidates.extend(values)

    headline_context = [
        {"id": item.get("id"), "title": item.get("title"), "source": item.get("source")}
        for item in items[:160]
    ]
    return (
        "你是终审编辑。请把下面的候选关键词合并、去重，输出最终周报 JSON。\n\n"
        "硬性规则：\n"
        "1. 只能使用候选和标题清单里出现的事实；不得新增人物、数字、公司或事件。\n"
        "2. 输出对象的键必须与下面 schema 字段完全一致；每个值是字符串数组；没有合格条目的板块输出 []。\n"
        "3. 同一事件只保留一条信息量最大的关键词，不要按来源重复。\n"
        "4. 排序优先级：具体人物/公司/模型/产品/场景 > 硬数字 > 对抗性张力 > 抽象趋势。\n"
        "5. 不要写新闻标题，不要用“重磅/炸裂/刷屏/改变一切”等空词。\n"
        "6. `executive_interviews_and_talks` 只收人物和场合明确的采访/座谈/播客/演讲/问答/内部信；没有就返回 []。\n"
        "7. 每条一般 8-30 个汉字，不得超过该字段的 maxLength。\n"
        "8. 只输出纯 JSON，不要 Markdown，不要解释。\n\n"
        "字段与分类定义：\n"
        + _category_block(categories)
        + "\n\n编辑偏好：\n"
        + _feedback_block(feedback)
        + "\n\n候选（已按 heat 排序，最多每板块 12 条）：\n"
        + json.dumps(compact_candidates, ensure_ascii=False)
        + "\n\n标题清单（用于补充上下文，不得据此编造事实）：\n"
        + json.dumps(headline_context, ensure_ascii=False)
        + "\n\n现在输出最终 JSON。"
    )


def _audit_prompt(
    result: Dict[str, List[str]],
    candidates: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    categories: List[Dict[str, Any]],
    feedback: Dict[str, Any],
) -> str:
    evidence = candidates if candidates else _format_items(items, summary_limit=180)
    return (
        "你是终稿的事实核查员，不是重写编辑。下面是已经生成的周报 JSON 和证据材料。\n"
        "你只允许做四件事：\n"
        "1. 删除在证据里完全找不到事实支撑的条目；有证据的条目一律保留，不要为了精简或平衡而删除。\n"
        "2. 把明显放错板块的条目移到更具体的板块：World Labs/Frameboy/世界模型 → world_models；"
        "VLA/具身基座模型 → embodied_ai_models_and_os；不含 AI 的汽车销量/OTA → ai_tools_and_tech_life 或删除。\n"
        "3. 只合并同一板块内完全重复的条目；同一事件在不同板块以不同角度出现时，保留，不要跨板块强行去重。\n"
        "4. 措辞最多改 8 个汉字；不要替换成新事件，不要新增人物、数字、公司或板块。\n\n"
        "不要为了填满板块而保留空话。如果某条只有标题、没有证据，删除它。"
        "最终输出完整 JSON，键与 schema 一致，没有内容的板块返回 []。保持原有条目数量和覆盖面。\n\n"
        "字段定义：\n"
        + _category_block(categories)
        + "\n\n路由偏好：\n"
        + _feedback_block(feedback)
        + "\n\n当前 JSON：\n"
        + json.dumps(result, ensure_ascii=False)
        + "\n\n证据材料：\n"
        + json.dumps(evidence, ensure_ascii=False)
        + "\n\n只输出修正后的完整 JSON。"
    )


def _single_pass_prompt(items: List[Dict[str, Any]], categories: List[Dict[str, Any]], feedback: Dict[str, Any]) -> str:
    return (
        "请从本周信源中直接提炼最终周报 JSON。\n\n"
        "要求：\n"
        "1. 只使用输入材料中的事实；不得新增人物、数字、公司或事件。\n"
        "2. 输出对象的键必须与下面 schema 字段完全一致；每个值是字符串数组；没有合格条目的板块输出 []。\n"
        "3. 同一事件只保留一条；优先具体人物、硬数字、对抗性张力。\n"
        "4. 不要写新闻标题，不要用“重磅/炸裂/刷屏/改变一切”等空词。\n"
        "5. `executive_interviews_and_talks` 只收人物和场合明确的采访/座谈/播客/演讲/问答/内部信；没有就返回 []。\n"
        "6. 每条一般 8-30 个汉字，不得超过 maxLength。\n"
        "7. 只输出纯 JSON。\n\n"
        "字段与分类定义：\n"
        + _category_block(categories)
        + "\n\n编辑偏好：\n"
        + _feedback_block(feedback)
        + "\n\n本周条目：\n"
        + json.dumps(_format_items(items), ensure_ascii=False)
        + "\n\n现在输出最终 JSON。"
    )


def _valid_evidence_ids(items: List[Dict[str, Any]]) -> set:
    return {str(item.get("id")) for item in items if item.get("id")}


def _parse_candidates(payload: Any, items: List[Dict[str, Any]], categories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    allowed = {category["key"] for category in categories}
    valid_ids = _valid_evidence_ids(items)
    if isinstance(payload, dict):
        raw_candidates = payload.get("candidates") or payload.get("keywords") or []
    elif isinstance(payload, list):
        raw_candidates = payload
    else:
        raw_candidates = []
    if isinstance(raw_candidates, dict):
        raw_candidates = [raw_candidates]

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "").strip()
        if category not in allowed:
            continue
        keyword = str(raw.get("keyword") or raw.get("name") or "").strip()
        if len(keyword) < 2:
            continue
        evidence = [str(value) for value in common.ensure_list(raw.get("evidence")) if str(value) in valid_ids]
        if not evidence:
            continue
        heat = raw.get("heat", 3)
        try:
            heat = max(1, min(5, int(heat)))
        except (TypeError, ValueError):
            heat = 3
        key = (category, common.keyword_key(keyword))
        candidate = {
            "category": category,
            "keyword": common.truncate(keyword, 80),
            "evidence": evidence,
            "entities": [str(value) for value in common.ensure_list(raw.get("entities"))][:8],
            "numbers": [str(value) for value in common.ensure_list(raw.get("numbers"))][:8],
            "tension": common.truncate(raw.get("tension") or "", 120),
            "heat": heat,
        }
        if key in merged:
            existing = merged[key]
            existing["evidence"] = sorted(set(existing["evidence"]) | set(candidate["evidence"]))
            existing["entities"] = sorted(set(existing["entities"]) | set(candidate["entities"]))
            existing["numbers"] = sorted(set(existing["numbers"]) | set(candidate["numbers"]))
            if len(candidate["tension"]) > len(existing["tension"]):
                existing["tension"] = candidate["tension"]
            existing["heat"] = max(existing["heat"], candidate["heat"])
        else:
            merged[key] = candidate
    return list(merged.values())


def _canonicalize_keys(payload: Dict[str, Any], categories: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("keywords"), dict):
        payload = payload["keywords"]
    by_title = {category["title"]: category["key"] for category in categories}
    result = dict(payload)
    for title, key in by_title.items():
        if key not in result and title in result:
            result[key] = result[title]
    return result


def _clean_keyword(value: Any) -> str:
    text = common.strip_html(value)
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"^\s*(?:[-*•]+|\d+[.、)])\s*", "", text)
    text = re.sub(r"^\s*(?:关键词|事件|主题)\s*[:：]\s*", "", text)
    text = text.strip().strip("\"'“”‘’")
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip("。；;，,、")
    if text.count("“") > text.count("”"):
        text += "”"
    return text.strip()


def normalize_result(payload: Any, schema: Dict[str, Any]) -> Dict[str, List[str]]:
    categories = _schema_categories(schema)
    data = _canonicalize_keys(payload if isinstance(payload, dict) else {}, categories)
    result: Dict[str, List[str]] = {}
    for category in categories:
        # Missing / null fields become empty arrays. The schema allows empty
        # sections, and this keeps one weak field from discarding the whole run.
        raw_values = data.get(category["key"])
        if raw_values is None:
            raw_values = []
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if not isinstance(raw_values, list):
            raw_values = []
        cleaned_values: List[str] = []
        seen = set()
        for raw in raw_values:
            if isinstance(raw, dict):
                raw = raw.get("keyword") or raw.get("name") or ""
            text = _clean_keyword(raw)
            if common.is_generic_empty(text):
                continue
            max_length = category["max_length"]
            if len(text) > max_length:
                text = text[:max_length].rstrip("，,。；;、 ")
            if len(text) < 2:
                continue
            key = common.keyword_key(text)
            if key in seen:
                continue
            seen.add(key)
            cleaned_values.append(text)
            if len(cleaned_values) >= category["max_items"]:
                break
        result[category["key"]] = cleaned_values
    return result


def _heuristic_category(text: str) -> str:
    if INTERVIEW_RE.search(text):
        return "executive_interviews_and_talks"
    if CREATOR_RE.search(text):
        if re.search(r"宠物|猫|狗|IP|虚拟人|数字人|人格", text):
            return "ai_pet_ip_博主自创"
        if re.search(r"短剧|漫剧|翻车|驯服", text):
            return "ai_short_drama_and_fail_博主自创"
        if re.search(r"代码|编程|Coding|Agent|开源项目|定时任务|Star", text, re.I):
            return "ai_coding_agent_博主自创"
        if re.search(r"3D|游戏|UE|Unity|Blender|CAD", text, re.I):
            return "3d_game_and_agent_博主自创"
        if re.search(r"绘画|音乐|短片|艺术|MV", text):
            return "ai_art_and_music_博主自创"
        return "vibe_video_博主自创"
    if EMBODIED_RE.search(text):
        if re.search(r"融资|估值|轮|IPO|投资", text):
            return "embodied_ai_funding_and_capital"
        if re.search(r"标准|政策|规划|工作组|认证|监管", text):
            return "embodied_ai_standards_and_policy"
        if re.search(r"\d+\s*小时|整班|工时|成功率|节拍|临界点|上岗|稳定作业|PMF|产线级", text):
            return "embodied_ai_metrics"
        if re.search(r"VLA|基座|模型|操作系统|OS|仿真|数据采集|EaaS|Scaling", text, re.I):
            return "embodied_ai_models_and_os"
        return "embodied_ai_hardware_and_deployment"
    if WORLD_MODEL_RE.search(text):
        return "world_models"
    if re.search(r"3D|游戏|UE|Unity|Blender|CAD|引擎", text, re.I):
        return "3d_game_ai"
    if OPEN_SOURCE_RE.search(text):
        return "ai_open_source"
    if re.search(r"监管|立法|安全|伦理|标准|版权|隐私|处罚|诉讼", text):
        return "ai_regulation_and_safety"
    if re.search(r"Agent|智能体|治理|信任|权限|协议", text, re.I):
        return "ai_agent_and_governance"
    if re.search(r"算力|芯片|数据中心|云|融资|收购|IPO|订单|营收|市值|合作", text):
        return "ai_infrastructure_and_business"
    if re.search(r"财报|裁员|组织|战略|起诉|和解|市值|高管", text):
        return "company_event_keywords"
    if re.search(r"视频|短剧|电影|音乐|广告|创作|工作流|生成", text):
        return "ai_creation_and_workflow"
    if re.search(r"曝光|泄露|调查|篡改|争议|召回|事故|内幕", text):
        return "reveal_and_expose"
    if re.search(r"替代|失业|一人公司|重构|岗位|裁员", text):
        return "disruption"
    if re.search(r"首次|突破|反超|极限|最强|破纪录|首次实现", text):
        return "tech_shock"
    if re.search(r"眼镜|耳机|浏览器|办公|搜索|电商|硬件|App", text, re.I):
        return "ai_tools_and_tech_life"
    return "ai_model_and_product"


def heuristic_refine(items: List[Dict[str, Any]], schema: Dict[str, Any]) -> Dict[str, List[str]]:
    """Explicitly-labelled extractive fallback; not a substitute for the LLM."""

    categories = _schema_categories(schema)
    result: Dict[str, List[str]] = {category["key"]: [] for category in categories}
    seen: Dict[str, set] = {category["key"]: set() for category in categories}

    for item in items:
        title = _clean_keyword(item.get("title") or "")
        if not title:
            continue
        title = title.split("，")[0].split("；")[0].strip()
        title = common.truncate(title, 38, suffix="")
        category = _heuristic_category(title + " " + (item.get("summary") or ""))
        if len(result[category]) >= min(6, next(c["max_items"] for c in categories if c["key"] == category)):
            continue
        key = common.keyword_key(title)
        if key in seen[category]:
            continue
        seen[category].add(key)
        result[category].append(title)

    all_text = " ".join((item.get("title") or "") + " " + (item.get("summary") or "") for item in items)
    result["viral_lexicon"] = [word for word in FALLBACK_VIRAL if word in all_text][:20]
    result["creator_viral_lexicon"] = [word for word in FALLBACK_CREATOR if word in all_text][:15]
    return normalize_result(result, schema)


def _write_prompts(prompt_dir: Path, system: str, map_prompts: List[str], final_prompt: Optional[str], single_prompt: Optional[str]) -> None:
    prompt_dir.mkdir(parents=True, exist_ok=True)
    common.write_text(prompt_dir / "system.txt", system)
    for index, prompt in enumerate(map_prompts, 1):
        common.write_text(prompt_dir / ("map-%02d.txt" % index), prompt)
    if final_prompt:
        common.write_text(prompt_dir / "final.txt", final_prompt)
    if single_prompt:
        common.write_text(prompt_dir / "single-pass.txt", single_prompt)
    common.write_text(prompt_dir / "README.txt", "Dry run only. No LLM call was made.\n")


def _summarize_usage(client: Optional[LLMClient]) -> Dict[str, Any]:
    if not client:
        return {}
    return {"calls": client.calls, "usage": dict(client.usage)}


def _render_markdown(result: Dict[str, List[str]], schema: Dict[str, Any], meta: Dict[str, Any]) -> str:
    generated = common.parse_datetime(meta.get("generated_at")) or common.now_utc()
    local = generated.astimezone(common.CHINA_TZ)
    iso_year, iso_week, _ = local.isocalendar()
    lines = [
        "# %d-W%02d AI 与具身智能关键词雷达" % (iso_year, iso_week),
        "",
        "> 窗口：%s ~ %s｜信源条目：%s｜去重后：%s｜模式：%s"
        % (
            (meta.get("window") or {}).get("since", "")[:10],
            (meta.get("window") or {}).get("until", "")[:10],
            (meta.get("raw_stats") or {}).get("items_fetched", "?"),
            (meta.get("raw_stats") or {}).get("items_after_dedupe", "?"),
            meta.get("mode", "?"),
        ),
        "",
    ]
    for key, spec in (schema.get("properties") or {}).items():
        values = result.get(key) or []
        if not values:
            if key == "executive_interviews_and_talks":
                lines.extend(["## %s" % (spec.get("title") or key), "", "_本周没有合格的采访/座谈条目。_", ""])
            continue
        lines.append("## %s" % (spec.get("title") or key))
        lines.append("")
        for value in values:
            lines.append("- %s" % value)
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("由 `fetch.py` 粗筛 + `refine.py` 精炼生成。关键词只基于本周信源，不构成投资建议。")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="把 raw.json 精炼成周报关键词 JSON")
    parser.add_argument("--input", default="output/raw.json")
    parser.add_argument("--output", default="output/weekly-keywords.json")
    parser.add_argument("--feedback", default="config/feedback.yaml")
    parser.add_argument("--schema", default="references/output-schema.json")
    parser.add_argument("--prompt", default="references/prompt.md")
    parser.add_argument("--candidates", default="output/candidates.json")
    parser.add_argument("--meta", default="output/run-meta.json")
    parser.add_argument("--markdown", default="output/weekly-keywords.md")
    parser.add_argument("--max-items", type=int, default=160)
    parser.add_argument("--chunk-size", type=int, default=40)
    parser.add_argument("--mode", choices=("auto", "map-reduce", "single"), default="auto")
    parser.add_argument("--no-llm", action="store_true", help="使用可解释的抽取式兜底，不调用 LLM")
    parser.add_argument("--fallback-on-error", action="store_true", help="LLM 失败时降级到抽取式兜底")
    parser.add_argument("--no-audit", action="store_true", help="跳过终稿事实核查/版面审计")
    parser.add_argument("--dry-run", action="store_true", help="只生成提示词，不调用 LLM")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    common.load_dotenv()
    input_path = Path(args.input)
    if not input_path.exists():
        print("[refine] 找不到输入文件：%s" % input_path, file=sys.stderr)
        return 1
    try:
        raw = common.read_json(input_path)
        schema = common.read_json(Path(args.schema))
        feedback = common.load_yaml(Path(args.feedback))
        guide = _read_text(Path(args.prompt))
    except Exception as exc:  # noqa: BLE001
        print("[refine] 输入/配置读取失败：%s" % exc, file=sys.stderr)
        return 1

    items = raw.get("items") or []
    if not items:
        print("[refine] raw.json 没有条目，先运行 fetch.py", file=sys.stderr)
        return 2

    categories = _schema_categories(schema)
    selected = select_items(items, feedback, args.max_items)
    if not selected:
        print("[refine] 没有选出可精炼的条目", file=sys.stderr)
        return 2
    if args.verbose:
        print("[refine] %d 条输入 → 选中 %d 条（max_items=%d）" % (len(items), len(selected), args.max_items))

    system = _system_prompt(guide, categories, feedback)
    client: Optional[LLMClient] = None
    candidates: List[Dict[str, Any]] = []
    mode = "heuristic"
    audit_status = "skipped"
    error_message = ""

    if args.dry_run:
        use_single = args.mode == "single" or (args.mode == "auto" and len(selected) <= 50)
        if use_single:
            single_prompt = _single_pass_prompt(selected, categories, feedback)
            _write_prompts(Path("output/prompts"), system, [], None, single_prompt)
            print("[refine] dry-run：提示词已写入 output/prompts/（未调用 LLM）")
        else:
            chunks = [selected[index : index + args.chunk_size] for index in range(0, len(selected), args.chunk_size)]
            map_prompts = [_map_prompt(chunk, categories, feedback) for chunk in chunks]
            _write_prompts(Path("output/prompts"), system, map_prompts, None, None)
            print("[refine] dry-run：%d 个 map 提示词已写入 output/prompts/（未调用 LLM；final 提示词需候选生成后才有）" % len(map_prompts))
        return 0

    if args.no_llm:
        result = heuristic_refine(selected, schema)
        mode = "heuristic"
    else:
        try:
            client = LLMClient.from_env()
        except LLMError as exc:
            if not args.fallback_on_error:
                print("[refine] %s" % exc, file=sys.stderr)
                print("[refine] 提示：没有 API key 时可用 --no-llm 生成抽取式兜底。", file=sys.stderr)
                return 3
            error_message = str(exc)
            result = heuristic_refine(selected, schema)
            mode = "heuristic-fallback"

        if client is not None:
            use_single = args.mode == "single" or (args.mode == "auto" and len(selected) <= 50)
            if use_single:
                single_prompt = _single_pass_prompt(selected, categories, feedback)
                try:
                    response = client.chat(
                        [{"role": "system", "content": system}, {"role": "user", "content": single_prompt}],
                        max_tokens=12000,
                        json_mode=True,
                    )
                    result = normalize_result(extract_json(response), schema)
                    mode = "single-pass"
                except Exception as exc:  # noqa: BLE001
                    if not args.fallback_on_error:
                        print("[refine] LLM 精炼失败：%s" % exc, file=sys.stderr)
                        return 3
                    error_message = str(exc)
                    print("[refine] LLM 精炼失败，降级到抽取式兜底：%s" % exc, file=sys.stderr)
                    result = heuristic_refine(selected, schema)
                    mode = "heuristic-fallback"
            else:
                chunks = [selected[index : index + args.chunk_size] for index in range(0, len(selected), args.chunk_size)]
                map_prompts = [_map_prompt(chunk, categories, feedback) for chunk in chunks]
                try:
                    for index, (chunk, prompt) in enumerate(zip(chunks, map_prompts), 1):
                        response = client.chat(
                            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                            max_tokens=4200,
                            json_mode=True,
                        )
                        chunk_candidates = _parse_candidates(extract_json(response), chunk, categories)
                        candidates.extend(chunk_candidates)
                        if args.verbose:
                            print("[refine] map %d/%d：%d 条候选" % (index, len(chunks), len(chunk_candidates)))
                    candidates = _parse_candidates({"candidates": candidates}, selected, categories)
                    if args.candidates:
                        common.write_json(Path(args.candidates), {"generated_at": common.iso_z(common.now_utc()), "candidates": candidates})
                    final_prompt = _final_prompt(candidates, selected, categories, feedback)
                    response = client.chat(
                        [{"role": "system", "content": system}, {"role": "user", "content": final_prompt}],
                        max_tokens=12000,
                        json_mode=True,
                    )
                    result = normalize_result(extract_json(response), schema)
                    mode = "llm-map-reduce"
                except Exception as exc:  # noqa: BLE001
                    if not args.fallback_on_error:
                        print("[refine] LLM 精炼失败：%s" % exc, file=sys.stderr)
                        return 3
                    error_message = str(exc)
                    print("[refine] LLM 精炼失败，降级到抽取式兜底：%s" % exc, file=sys.stderr)
                    result = heuristic_refine(selected, schema)
                    mode = "heuristic-fallback"

    if client is not None and not args.no_audit and mode in ("single-pass", "llm-map-reduce"):
        try:
            before_total = sum(len(values) for values in result.values())
            audit_prompt = _audit_prompt(result, candidates, selected, categories, feedback)
            response = client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": audit_prompt}],
                max_tokens=12000,
                json_mode=True,
            )
            audited = normalize_result(extract_json(response), schema)
            after_total = sum(len(values) for values in audited.values())
            if after_total >= max(1, int(before_total * 0.5)):
                result = audited
                audit_status = "ok"
                if args.verbose:
                    print("[refine] audit：%d → %d 条" % (before_total, after_total))
            else:
                audit_status = "kept-original"
                print("[refine] audit 删除过多（%d → %d），保留原终稿" % (before_total, after_total), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            audit_status = "failed"
            print("[refine] audit 失败，保留原终稿：%s" % exc, file=sys.stderr)

    result = normalize_result(result, schema)
    validation_errors = validate_document(result, schema)
    if validation_errors:
        invalid_path = Path("output/weekly-keywords.invalid.json")
        common.write_json(invalid_path, result)
        print("[refine] 输出未通过校验，已写入 %s" % invalid_path, file=sys.stderr)
        for error in validation_errors:
            print("  - %s" % error, file=sys.stderr)
        return 4

    generated_at = common.now_utc()
    meta = {
        "schema_version": 1,
        "generated_at": common.iso_z(generated_at),
        "mode": mode,
        "model": client.model if client else "",
        "window": raw.get("window") or {},
        "raw_stats": raw.get("stats") or {},
        "items_total": len(items),
        "items_selected": len(selected),
        "chunks": max(1, (len(selected) + args.chunk_size - 1) // args.chunk_size) if mode == "llm-map-reduce" else 1,
        "candidates": len(candidates),
        "audit": audit_status,
        "llm": _summarize_usage(client),
        "error": error_message,
        "output": str(Path(args.output)),
    }
    common.write_json(Path(args.output), result)
    common.write_json(Path(args.meta), meta)
    common.write_text(Path(args.markdown), _render_markdown(result, schema, meta))
    print("[refine] %s：%d 个板块，%d 条关键词 → %s" % (
        mode,
        len(result),
        sum(len(values) for values in result.values()),
        args.output,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
