# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402


class CommonTests(unittest.TestCase):
    def test_parse_rfc822(self):
        parsed = common.parse_datetime("Thu, 05 Nov 2026 10:00:00 +0800")
        self.assertIsNotNone(parsed)
        self.assertEqual(common.iso_z(parsed), "2026-11-05T02:00:00Z")

    def test_parse_iso_z(self):
        parsed = common.parse_datetime("2026-11-05T08:00:00Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(common.iso_z(parsed), "2026-11-05T08:00:00Z")

    def test_canonical_url(self):
        value = common.canonical_url("https://www.Example.com/a/?utm_source=x&b=2#frag")
        self.assertEqual(value, "https://example.com/a?b=2")

    def test_normalize_title_removes_publisher_suffix(self):
        self.assertEqual(common.normalize_title("某模型发布 - 36氪"), "某模型发布")

    def test_strip_html(self):
        value = common.strip_html("<p>Hello&nbsp;<strong>AI</strong></p>")
        self.assertEqual(value, "Hello AI")

    def test_match_terms(self):
        self.assertTrue(common.match_terms("OpenAI 发布 GPT-6", ["AI", "OpenAI"]))
        self.assertFalse(common.match_terms("The said train", ["AI"]))
        self.assertTrue(common.match_terms("这是一个大模型", ["大模型"]))

    def test_title_similarity(self):
        left = common.title_shingles(common.normalize_title("某模型 2.8T 参数开放权重"))
        right = common.title_shingles(common.normalize_title("某模型2.8T参数开放权重"))
        self.assertGreaterEqual(common.jaccard(left, right), 0.9)

    def test_load_yaml(self):
        data = common.load_yaml(ROOT / "config" / "sources.yaml")
        self.assertIn("sources", data)
        self.assertGreater(len(data["sources"]), 10)


if __name__ == "__main__":
    unittest.main()
