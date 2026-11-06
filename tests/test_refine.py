# -*- coding: utf-8 -*-
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
import refine  # noqa: E402


class RefineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = common.read_json(ROOT / "references" / "output-schema.json")
        cls.keys = [key for key in cls.schema["properties"]]

    def _empty_payload(self):
        return {key: [] for key in self.keys}

    def test_normalize_fills_missing_and_dedupes(self):
        payload = self._empty_payload()
        payload["ai_model_and_product"] = [
            "- 某模型 2.8T 参数开放权重",
            "某模型 2.8T 参数开放权重",
            "无",
            "关键词：某机器人完成 8 小时整班作业",
        ]
        result = refine.normalize_result(payload, self.schema)
        self.assertEqual(result["ai_model_and_product"], ["某模型 2.8T 参数开放权重", "某机器人完成 8 小时整班作业"])
        self.assertIn("executive_interviews_and_talks", result)

    def test_normalize_missing_keys_becomes_empty(self):
        result = refine.normalize_result({"ai_model_and_product": ["某模型发布"]}, self.schema)
        self.assertEqual(result["ai_model_and_product"], ["某模型发布"])
        for key in self.keys:
            self.assertIn(key, result)
            self.assertIsInstance(result[key], list)

    def test_extract_json_fenced(self):
        value = refine.extract_json('```json\n{"ok": true}\n```')
        self.assertEqual(value, {"ok": True})

    def test_extract_json_surrounded(self):
        value = refine.extract_json('这是结果：{"ok": true} 以上。')
        self.assertEqual(value, {"ok": True})

    def test_extract_json_invalid(self):
        with self.assertRaises(refine.LLMError):
            refine.extract_json("not json")

    def test_heuristic_refine_interview_and_creator(self):
        items = [
            {
                "id": "1",
                "title": "某 CEO 专访：机器人先过 8 小时整班",
                "summary": "谈具身智能的量产与上岗关。",
                "source": "测试",
                "source_id": "test",
                "published_at": "2026-11-05T00:00:00Z",
                "tags": ["interview"],
                "rank_score": 1.0,
            },
            {
                "id": "2",
                "title": "B站 UP主 手搓 AI 短剧，播放量 5000 万",
                "summary": "用两台电脑做了 534 集。",
                "source": "测试",
                "source_id": "test",
                "published_at": "2026-11-05T00:00:00Z",
                "tags": ["creator"],
                "rank_score": 1.0,
            },
        ]
        result = refine.heuristic_refine(items, self.schema)
        self.assertTrue(any("专访" in value for value in result["executive_interviews_and_talks"]))
        self.assertTrue(any("短剧" in value for value in result["ai_short_drama_and_fail_博主自创"]))

    def test_select_items_reserves_interview(self):
        items = []
        for index in range(20):
            items.append(
                {
                    "id": "g%d" % index,
                    "title": "某模型发布第 %d 代，参数提升 10%%" % index,
                    "summary": "普通模型新闻。" * 10,
                    "source": "general",
                    "source_id": "general",
                    "published_at": "2026-11-05T00:00:00Z",
                    "tags": ["model"],
                    "rank_score": 5.0,
                }
            )
        items.append(
            {
                "id": "interview",
                "title": "某创始人专访：先过上岗关，再谈规模",
                "summary": "访谈内容。",
                "source": "interview",
                "source_id": "interview",
                "published_at": "2026-11-05T00:00:00Z",
                "tags": ["interview"],
                "rank_score": 0.1,
            }
        )
        selected = refine.select_items(items, {}, max_items=5)
        self.assertEqual(len(selected), 5)
        self.assertIn("interview", [item["id"] for item in selected])

    def test_select_items_respects_max(self):
        items = [
            {
                "id": "i%d" % index,
                "title": "AI 新闻 %d" % index,
                "summary": "",
                "source": "s",
                "source_id": "s",
                "published_at": "2026-11-05T00:00:00Z",
                "tags": [],
                "rank_score": 1.0,
            }
            for index in range(100)
        ]
        selected = refine.select_items(items, {}, max_items=10)
        self.assertLessEqual(len(selected), 10)

    def test_parse_candidates_requires_valid_evidence(self):
        items = [{"id": "a"}, {"id": "b"}]
        categories = refine._schema_categories(self.schema)
        payload = {
            "candidates": [
                {
                    "category": "ai_model_and_product",
                    "keyword": "某模型 2.8T 参数开放权重",
                    "evidence": ["a"],
                    "heat": 5,
                },
                {
                    "category": "ai_model_and_product",
                    "keyword": "某模型 2.8T 参数开放权重",
                    "evidence": ["b"],
                    "heat": 4,
                },
                {
                    "category": "not_a_category",
                    "keyword": "某事件",
                    "evidence": ["a"],
                },
                {
                    "category": "ai_model_and_product",
                    "keyword": "没有证据的条目",
                    "evidence": ["missing"],
                },
            ]
        }
        candidates = refine._parse_candidates(payload, items, categories)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(sorted(candidates[0]["evidence"]), ["a", "b"])

    def test_category_routing(self):
        self.assertEqual(refine._heuristic_category("某机器人完成 8 小时整班作业"), "embodied_ai_metrics")
        self.assertEqual(refine._heuristic_category("某世界模型实时生成 3D 场景"), "world_models")
        self.assertEqual(refine._heuristic_category("某模型开源权重与训练代码"), "ai_open_source")
        self.assertEqual(refine._heuristic_category("某 CEO 播客谈 Agent 治理"), "executive_interviews_and_talks")


if __name__ == "__main__":
    unittest.main()
