# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402
from validate import validate_document  # noqa: E402


class ValidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = common.read_json(ROOT / "references" / "output-schema.json")

    def _valid_document(self):
        return {key: [] for key in self.schema["required"]}

    def test_empty_document_is_valid(self):
        self.assertEqual(validate_document(self._valid_document(), self.schema), [])

    def test_missing_interview_key_fails(self):
        document = self._valid_document()
        document.pop("executive_interviews_and_talks")
        errors = validate_document(document, self.schema)
        self.assertTrue(any("executive_interviews_and_talks" in error for error in errors))

    def test_extra_key_fails(self):
        document = self._valid_document()
        document["extra"] = []
        errors = validate_document(document, self.schema)
        self.assertTrue(any("extra" in error for error in errors))

    def test_duplicate_keyword_fails(self):
        document = self._valid_document()
        document["ai_model_and_product"] = ["某模型发布", "某模型发布"]
        errors = validate_document(document, self.schema)
        self.assertTrue(any("重复" in error for error in errors))

    def test_non_string_fails(self):
        document = self._valid_document()
        document["ai_model_and_product"] = [123]
        errors = validate_document(document, self.schema)
        self.assertTrue(any("字符串" in error for error in errors))

    def test_too_many_items_fails(self):
        document = self._valid_document()
        document["tech_shock"] = ["冲击 %d" % index for index in range(11)]
        errors = validate_document(document, self.schema)
        self.assertTrue(any("超过最大条数" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
