# -*- coding: utf-8 -*-
import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import fetch  # noqa: E402


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.now = common.parse_datetime("2026-11-06T12:00:00Z")
        self.since = self.now - timedelta(days=7)
        self.until = self.now
        self.rss_source = {
            "id": "fixture-rss",
            "name": "Fixture RSS",
            "type": "rss",
            "url": "https://example.com/feed",
            "weight": 1.0,
            "lang": "zh",
            "ai_only": False,
            "tags": ["test"],
        }

    def _parse(self, filename, source=None, include_undated=False):
        data = (ROOT / "tests" / "fixtures" / filename).read_bytes()
        return fetch.parse_feed(
            data=data,
            source=source or self.rss_source,
            fetched_at=self.now,
            since=self.since,
            until=self.until,
            include_undated=include_undated,
            ai_terms=fetch.DEFAULT_AI_TERMS,
            exclude_patterns=fetch.DEFAULT_EXCLUDE_PATTERNS,
        )

    def test_rss_filters_window_and_non_ai(self):
        items, count = self._parse("rss.xml")
        self.assertEqual(count, 6)
        titles = [item["title"] for item in items]
        self.assertTrue(any("2.8T" in title for title in titles))
        self.assertTrue(any("8 小时" in title for title in titles))
        self.assertFalse(any("秋季新品" in title for title in titles))
        self.assertFalse(any("旧闻" in title for title in titles))
        self.assertFalse(any("没有发布时间" in title for title in titles))

    def test_include_undated(self):
        items, _ = self._parse("rss.xml", include_undated=True)
        self.assertTrue(any("没有发布时间" in item["title"] for item in items))

    def test_ai_only_feed_keeps_general_items(self):
        source = dict(self.rss_source, ai_only=True)
        items, _ = self._parse("rss.xml", source=source)
        self.assertTrue(any("秋季新品" in item["title"] for item in items))

    def test_atom_relative_link(self):
        source = dict(self.rss_source, id="fixture-atom", name="Fixture Atom", url="https://atom.example.com/feed")
        items, _ = self._parse("atom.xml", source=source)
        self.assertEqual(len(items), 2)
        world = [item for item in items if "世界模型" in item["title"]][0]
        self.assertEqual(world["url"], "https://atom.example.com/world")

    def test_google_news_publisher_and_title_suffix(self):
        source = {
            "id": "fixture-google",
            "name": "Google News · 大模型",
            "type": "google_news",
            "query": "大模型",
            "weight": 0.9,
            "lang": "zh",
            "ai_only": True,
        }
        items, _ = self._parse("google_news.xml", source=source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "美团")
        self.assertNotIn(" - 美团", items[0]["title"])
        self.assertEqual(items[0]["date_confidence"], "high")

    def test_dedupe_url_and_similar_title(self):
        base = common.parse_datetime("2026-11-05T10:00:00Z")
        items = [
            {
                "id": "a",
                "title": "某模型 2.8T 参数开放权重",
                "url": "https://example.com/a?utm_source=x",
                "source": "Source A",
                "feed_name": "Source A",
                "summary": "短",
                "published_at": common.iso_z(base),
                "weight": 1.0,
                "tags": ["model"],
            },
            {
                "id": "b",
                "title": "某模型 2.8T 参数开放权重",
                "url": "https://example.com/a",
                "source": "Source B",
                "feed_name": "Source B",
                "summary": "更长的摘要，包含参数和训练代码。",
                "published_at": common.iso_z(base),
                "weight": 0.8,
                "tags": ["open_source"],
            },
            {
                "id": "c",
                "title": "某模型 2.8T 参数开放权重，训练代码同步开源",
                "url": "https://example.com/c",
                "source": "Source C",
                "feed_name": "Source C",
                "summary": "",
                "published_at": common.iso_z(base),
                "weight": 0.9,
                "tags": [],
            },
        ]
        unique = fetch.dedupe_items(items)
        self.assertEqual(len(unique), 1)
        self.assertIn("Source B", unique[0]["also_seen_in"])
        self.assertIn("open_source", unique[0]["tags"])

    def test_score_prefers_concrete_numbers(self):
        base = common.parse_datetime("2026-11-05T10:00:00Z")
        concrete = {
            "title": "某机器人完成 8 小时整班作业，成功率 98%",
            "summary": "",
            "weight": 1.0,
            "published_at": common.iso_z(base),
        }
        vague = {
            "title": "某公司发布重磅产品，行业沸腾",
            "summary": "",
            "weight": 1.0,
            "published_at": common.iso_z(base),
        }
        self.assertGreater(fetch._score_item(concrete), fetch._score_item(vague))


if __name__ == "__main__":
    unittest.main()
