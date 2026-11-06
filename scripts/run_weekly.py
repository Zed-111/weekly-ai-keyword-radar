#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-command weekly pipeline: fetch -> refine -> validate -> optional archive."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
from validate import validate_document  # noqa: E402


def _run(command: List[str], label: str) -> int:
    print("[weekly] %s" % label, flush=True)
    print("[weekly] $ %s" % " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=str(ROOT))
    return completed.returncode


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="一键运行每周 AI 关键词雷达")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--config", default="config/sources.yaml")
    parser.add_argument("--feedback", default="config/feedback.yaml")
    parser.add_argument("--schema", default="references/output-schema.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-items", type=int, default=160)
    parser.add_argument("--mode", choices=("auto", "map-reduce", "single"), default="auto")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--fallback-on-error", action="store_true")
    parser.add_argument("--no-audit", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-refine", action="store_true")
    parser.add_argument("--archive", action="store_true", help="把本周结果复制到 output/archive/YYYY-Www.json")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    common.load_dotenv()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw.json"
    weekly_path = output_dir / "weekly-keywords.json"
    markdown_path = output_dir / "weekly-keywords.md"
    meta_path = output_dir / "run-meta.json"

    if not args.skip_fetch:
        command = [
            sys.executable,
            str(SCRIPT_DIR / "fetch.py"),
            "--days",
            str(args.days),
            "--config",
            args.config,
            "--output",
            str(raw_path),
        ]
        if args.verbose:
            command.append("--verbose")
        code = _run(command, "第 1 步：抓取 + 去重")
        if code != 0:
            print("[weekly] 抓取失败，退出码 %d" % code, file=sys.stderr)
            return code

    if args.skip_refine:
        print("[weekly] 已跳过精炼。raw.json：%s" % raw_path)
        return 0

    command = [
        sys.executable,
        str(SCRIPT_DIR / "refine.py"),
        "--input",
        str(raw_path),
        "--output",
        str(weekly_path),
        "--feedback",
        args.feedback,
        "--schema",
        args.schema,
        "--markdown",
        str(markdown_path),
        "--meta",
        str(meta_path),
        "--candidates",
        str(output_dir / "candidates.json"),
        "--max-items",
        str(args.max_items),
        "--mode",
        args.mode,
    ]
    if args.no_llm:
        command.append("--no-llm")
    if args.fallback_on_error:
        command.append("--fallback-on-error")
    if args.no_audit:
        command.append("--no-audit")
    if args.verbose:
        command.append("--verbose")
    code = _run(command, "第 2 步：LLM 精炼")
    if code != 0:
        print("[weekly] 精炼失败，退出码 %d" % code, file=sys.stderr)
        return code

    schema = common.read_json(Path(args.schema))
    document = common.read_json(weekly_path)
    errors = validate_document(document, schema)
    if errors:
        print("[weekly] 校验失败：", file=sys.stderr)
        for error in errors:
            print("  - %s" % error, file=sys.stderr)
        return 4
    print("[weekly] 校验通过：%s" % weekly_path)

    if args.archive:
        meta = common.read_json(meta_path) if meta_path.exists() else {}
        generated = common.parse_datetime(meta.get("generated_at")) or common.now_utc()
        local = generated.astimezone(common.CHINA_TZ)
        iso_year, iso_week, _ = local.isocalendar()
        archive_dir = output_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / ("%d-W%02d.json" % (iso_year, iso_week))
        common.write_json(archive_path, document)
        print("[weekly] 已归档：%s" % archive_path)

    print("[weekly] 完成：")
    print("  JSON：%s" % weekly_path)
    print("  Markdown：%s" % markdown_path)
    print("  Meta：%s" % meta_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
