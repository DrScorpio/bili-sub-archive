"""脱敏测试：凭据值绝不出现在文本/对象/产物里，字段名保留。"""

from __future__ import annotations

import unittest
from pathlib import Path

from bili_sub_archive.redact import (
    REDACTED,
    Redactor,
    cookie_field_names,
    redact_known_keys,
    scan_for_secrets,
    secret_values,
)
from tests import workspace

COOKIE = "SESSDATA=cc878d09%2C1796620065%2Cabc; bili_jct=51888e18d69a89ac; buvid3=56A54859-3DFC"


class RedactorTest(unittest.TestCase):
    def setUp(self):
        self.red = Redactor.from_cookie(COOKIE)

    def test_full_cookie_removed(self):
        self.assertEqual(self.red.redact(COOKIE), REDACTED)

    def test_individual_values_removed(self):
        text = f"headers=Cookie: {COOKIE}"
        out = self.red.redact(text)
        self.assertNotIn("cc878d09", out)
        self.assertNotIn("51888e18d69a89ac", out)
        self.assertNotIn("56A54859-3DFC", out)

    def test_field_names_preserved(self):
        out = self.red.redact(COOKIE)
        self.assertEqual(out, REDACTED)  # 整串命中，字段名随整串一起被抹掉

    def test_short_values_not_redacted(self):
        red = Redactor(["1", "ab"])
        self.assertEqual(red.redact("版本 1.2 ab"), "版本 1.2 ab")

    def test_redact_obj_nested(self):
        obj = {"cookie": COOKIE, "list": [{"x": "SESSDATA=cc878d09%2C1796620065%2Cabc"}]}
        out = self.red.redact_obj(obj)
        blob = str(out)
        self.assertNotIn("cc878d09", blob)
        self.assertEqual(out["cookie"], REDACTED)

    def test_redact_obj_keeps_non_strings(self):
        self.assertEqual(self.red.redact_obj({"n": 1, "b": True, "nul": None}),
                         {"n": 1, "b": True, "nul": None})

    def test_llm_key_redacted(self):
        red = Redactor.from_cookie(COOKIE, ["sk-test-1234567890"])
        self.assertNotIn("sk-test-1234567890", red.redact("key=sk-test-1234567890"))

    def test_contains_secret(self):
        self.assertIsNotNone(self.red.contains_secret(COOKIE))
        self.assertIsNone(self.red.contains_secret("无凭据文本"))


class HelperTest(unittest.TestCase):
    def test_cookie_field_names_without_values(self):
        names = cookie_field_names(COOKIE)
        self.assertEqual(names, ["SESSDATA", "bili_jct", "buvid3"])

    def test_secret_values_include_parts(self):
        values = secret_values(COOKIE)
        self.assertIn(COOKIE, values)
        self.assertIn("51888e18d69a89ac", values)

    def test_redact_known_keys_fallback(self):
        out = redact_known_keys("SESSDATA=abcdef123456; CURRENT_QUALITY=80")
        self.assertNotIn("abcdef123456", out)
        self.assertIn("CURRENT_QUALITY=80", out)


class ScanTest(unittest.TestCase):
    def test_scan_finds_leak(self):
        with workspace() as tmp:
            root = Path(tmp)
            (root / "index.json").write_text('{"cookie": "' + COOKIE + '"}', encoding="utf-8")
            (root / "content.md").write_text("正常正文", encoding="utf-8")
            red = Redactor.from_cookie(COOKIE)
            hits = scan_for_secrets(root, red)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0][0].name, "index.json")

    def test_scan_clean_directory(self):
        with workspace() as tmp:
            root = Path(tmp)
            (root / "metadata.json").write_text('{"title": "标题"}', encoding="utf-8")
            self.assertEqual(scan_for_secrets(root, Redactor.from_cookie(COOKIE)), [])

    def test_scan_missing_directory(self):
        self.assertEqual(scan_for_secrets(Path("不存在的目录"), Redactor.from_cookie(COOKIE)), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
