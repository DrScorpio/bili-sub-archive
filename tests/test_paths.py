"""路径与落盘测试：名称清理、作者目录复用、原子写入、单写者锁。"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from bili_sub_archive.errors import OutputError
from bili_sub_archive.paths import (
    DirLock,
    atomic_write_json,
    atomic_write_text,
    author_dir_name,
    entry_dir_name,
    find_existing_author_dir,
    resolve_author_dir,
    sanitize_component,
)
from tests import workspace


class SanitizeTest(unittest.TestCase):
    def test_illegal_windows_chars_removed(self):
        self.assertEqual(sanitize_component('a<b>c:d"e/f\\g|h?i*j'), "abcdefghij")

    def test_newlines_and_collapsed_spaces(self):
        self.assertEqual(sanitize_component("多行\n标题\t  空格"), "多行 标题 空格")

    def test_reserved_names_prefixed(self):
        self.assertEqual(sanitize_component("CON"), "_CON")
        self.assertEqual(sanitize_component("com1"), "_com1")

    def test_trailing_dots_and_spaces_stripped(self):
        self.assertEqual(sanitize_component("标题... "), "标题")

    def test_empty_falls_back(self):
        self.assertEqual(sanitize_component("   ", fallback="无标题"), "无标题")

    def test_truncated_to_budget(self):
        out = sanitize_component("字" * 200, budget=20)
        self.assertEqual(len(out), 20)

    def test_chinese_and_emoji_kept(self):
        self.assertIn("标题", sanitize_component("【标题】测试.png"))


class NamingTest(unittest.TestCase):
    def test_author_dir_name(self):
        self.assertEqual(author_dir_name(1039025435, "战国时代_姜汁汽水"),
                         "1039025435_战国时代_姜汁汽水")

    def test_author_dir_name_sanitizes(self):
        self.assertEqual(author_dir_name(1, 'a/b:c'), "1_abc")

    def test_entry_dir_name_format(self):
        name = entry_dir_name("2026-09-23", "video", "标题", "BV1xx411c7mD")
        self.assertEqual(name, "2026-09-23_视频_标题_BV1xx411c7mD")

    def test_entry_dir_name_kinds(self):
        self.assertTrue(entry_dir_name("2026-01-01", "dynamic", "摘要", "123").endswith("_123"))
        self.assertIn("_动态_", entry_dir_name("2026-01-01", "dynamic", "摘要", "123"))
        self.assertIn("_专栏_", entry_dir_name("2026-01-01", "article", "标题", "cv1"))


class AuthorDirTest(unittest.TestCase):
    def test_resolve_creates_then_reuses_on_name_change(self):
        with workspace() as tmp:
            root = Path(tmp)
            first, _ = resolve_author_dir(root, 42, "旧名字")
            self.assertEqual(first.name, "42_旧名字")
            # 改名后必须复用同一目录（以 UID 定位，计划 3.2）
            second, warnings = resolve_author_dir(root, 42, "新名字")
            self.assertEqual(second, first)
            self.assertEqual(warnings, [])

    def test_multiple_candidates_warns_and_picks_newest(self):
        with workspace() as tmp:
            root = Path(tmp)
            old = root / "42_旧"
            old.mkdir()
            atomic_write_json(old / "index.json", {"uid": 42, "entries": []})
            new = root / "42_新"
            new.mkdir()
            atomic_write_json(new / "index.json", {"uid": 42, "entries": []})
            os.utime(old / "index.json", (time.time() - 100, time.time() - 100))
            found, warnings = find_existing_author_dir(root, 42)
            self.assertEqual(found, new)
            self.assertEqual(len(warnings), 1)
            self.assertIn("多个候选", warnings[0])

    def test_other_uid_dirs_ignored(self):
        with workspace() as tmp:
            root = Path(tmp)
            (root / "7_别人").mkdir()
            found, _ = find_existing_author_dir(root, 8)
            self.assertIsNone(found)


class AtomicWriteTest(unittest.TestCase):
    def test_text_and_json_round_trip(self):
        with workspace() as tmp:
            path = Path(tmp) / "sub" / "a.json"
            atomic_write_json(path, {"中文": 1, "list": [1, 2]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["中文"], 1)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["a.json"])

    def test_text_write_no_temp_left(self):
        with workspace() as tmp:
            path = Path(tmp) / "content.md"
            atomic_write_text(path, "正文")
            self.assertEqual(path.read_text(encoding="utf-8"), "正文")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["content.md"])

    def test_overwrite_keeps_single_file(self):
        with workspace() as tmp:
            path = Path(tmp) / "x.txt"
            atomic_write_text(path, "1")
            atomic_write_text(path, "2")
            self.assertEqual(path.read_text(encoding="utf-8"), "2")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class DirLockTest(unittest.TestCase):
    def test_second_lock_blocked(self):
        with workspace() as tmp:
            first = DirLock(Path(tmp))
            first.acquire()
            try:
                with self.assertRaises(OutputError):
                    DirLock(Path(tmp)).acquire()
            finally:
                first.release()
            # 释放后可再次获取
            second = DirLock(Path(tmp))
            second.acquire()
            second.release()
            self.assertFalse((Path(tmp) / ".lock").exists())

    def test_stale_lock_stolen(self):
        with workspace() as tmp:
            lock_path = Path(tmp) / ".lock"
            lock_path.write_text('{"pid": 1}', encoding="utf-8")
            old = time.time() - 3600 * 24
            os.utime(lock_path, (old, old))
            messages: list[str] = []

            class _Log:
                def warning(self, message):
                    messages.append(message)

            lock = DirLock(Path(tmp), stale_hours=1, logger=_Log())
            lock.acquire()
            self.assertTrue(lock.acquired)
            self.assertTrue(messages and "陈旧写锁" in messages[0])
            lock.release()

    def test_lock_released_on_exception(self):
        with workspace() as tmp:
            with self.assertRaises(RuntimeError):
                with DirLock(Path(tmp)):
                    raise RuntimeError("boom")
            self.assertFalse((Path(tmp) / ".lock").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
