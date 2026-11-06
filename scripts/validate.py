#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the weekly keyword JSON against references/output-schema.json.

Only a small, deliberately readable subset of JSON Schema is implemented:
required keys, additionalProperties, array/string types, maxItems, minLength,
maxLength and duplicate detection. This keeps CI dependency-free.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402


def validate_document(document: Any, schema: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(document, dict):
        return ["顶层必须是 JSON 对象"]

    properties = schema.get("properties") or {}
    required = schema.get("required") or list(properties.keys())
    for key in required:
        if key not in document:
            errors.append("缺少必需字段：%s" % key)
    if schema.get("additionalProperties") is False:
        for key in document:
            if key not in properties:
                errors.append("出现 schema 之外的字段：%s" % key)

    for key, spec in properties.items():
        if key not in document:
            continue
        value = document[key]
        label = spec.get("title") or key
        if spec.get("type") == "array":
            if not isinstance(value, list):
                errors.append("%s 必须是数组" % label)
                continue
            max_items = spec.get("maxItems")
            if isinstance(max_items, int) and len(value) > max_items:
                errors.append("%s 超过最大条数 %d（当前 %d）" % (label, max_items, len(value)))
            item_spec = spec.get("items") or {}
            seen = set()
            for index, entry in enumerate(value):
                prefix = "%s[%d]" % (label, index)
                if not isinstance(entry, str):
                    errors.append("%s 必须是字符串" % prefix)
                    continue
                text = entry.strip()
                if not text:
                    errors.append("%s 不能为空" % prefix)
                    continue
                if text != entry:
                    errors.append("%s 首尾不能有空白" % prefix)
                min_length = item_spec.get("minLength")
                max_length = item_spec.get("maxLength")
                if isinstance(min_length, int) and len(text) < min_length:
                    errors.append("%s 长度小于 %d：%s" % (prefix, min_length, text))
                if isinstance(max_length, int) and len(text) > max_length:
                    errors.append("%s 长度大于 %d：%s" % (prefix, max_length, text))
                if common.is_generic_empty(text):
                    errors.append("%s 是空占位值：%s" % (prefix, text))
                normalized = common.keyword_key(text)
                if normalized in seen:
                    errors.append("%s 重复：%s" % (prefix, text))
                seen.add(normalized)
        elif spec.get("type") == "string":
            if not isinstance(value, str):
                errors.append("%s 必须是字符串" % label)

    if "executive_interviews_and_talks" not in document:
        errors.append("缺少“大佬采访座谈分享”字段：executive_interviews_and_talks")
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校验周报关键词 JSON")
    parser.add_argument("--input", default="output/weekly-keywords.json")
    parser.add_argument("--schema", default="references/output-schema.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    schema_path = Path(args.schema)
    if not input_path.exists():
        print("[validate] 找不到输入文件：%s" % input_path, file=sys.stderr)
        return 1
    if not schema_path.exists():
        print("[validate] 找不到 schema：%s" % schema_path, file=sys.stderr)
        return 1

    document = common.read_json(input_path)
    schema = common.read_json(schema_path)
    errors = validate_document(document, schema)
    if errors:
        print("[validate] 校验失败，共 %d 个问题：" % len(errors), file=sys.stderr)
        for error in errors:
            print("  - %s" % error, file=sys.stderr)
        return 1

    if not args.quiet:
        total = sum(len(value) for value in document.values() if isinstance(value, list))
        print("[validate] 通过：%d 个板块，%d 条关键词" % (len(document), total))
        for key, spec in (schema.get("properties") or {}).items():
            values = document.get(key) or []
            print("  %-38s %2d" % (spec.get("title") or key, len(values)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
