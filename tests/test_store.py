"""状态与索引测试：条目分配、步骤状态转移、索引重建、运行摘要落盘。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from bili_sub_archive.models import (
    KIND_ARTICLE,
    KIND_DYNAMIC,
    KIND_VIDEO,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_SKIPPED,
    Item,
)
from bili_sub_archive.redact import Redactor
from bili_sub_archive.store import METADATA_NAME, Store
from bili_sub_archive.timeutil import BEIJING
from tests import drop_workspace, make_workspace


def item(kind: str = KIND_DYNAMIC, pid: str = "d1", title: str = "标题") -> Item:
    return Item(kind=kind, platform_id=pid, title=title,
                url=f"https://www.bilibili.com/opus/{pid}", author="UP",
                published_at=datetime(2026, 9, 23, 12, 0, tzinfo=BEIJING),
                raw_ref={"api": "/x/fake"}, extras={"pinned": False})


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("store")
        self.author_dir = self.root / "42_测试UP"
        self.store = Store(self.author_dir, uid=42, author_name="测试UP", author_mid=42)
        self.store.load()

    def tearDown(self):
        drop_workspace(self.root)


class AllocateTest(StoreTestBase):
    def test_allocate_creates_dir_and_metadata(self):
        state = self.store.allocate(item())
        self.assertTrue((self.author_dir / state.dir / METADATA_NAME).is_file())
        self.assertEqual(state.dir, "2026-09-23_动态_标题_d1")
        self.assertEqual(sorted(state.steps), ["content", "fetch", "images", "render"])
        self.assertEqual(state.rollup, STEP_PENDING)

    def test_allocate_is_idempotent(self):
        first = self.store.allocate(item())
        second = self.store.allocate(item())
        self.assertEqual(first.dir, second.dir)
        # 只创建了一个条目目录（index.json 尚未保存）
        self.assertEqual([p.name for p in self.author_dir.iterdir()], [first.dir])

    def test_collision_gets_suffix_without_overwrite(self):
        (self.author_dir / "2026-09-23_动态_标题_d1").mkdir(parents=True)
        # 目录存在但索引未登记（上次中断残留）→ 换名，不覆盖
        state = self.store.allocate(item())
        self.assertEqual(state.dir, "2026-09-23_动态_标题_d1_2")
        self.assertTrue(state.dir and (self.author_dir / state.dir).is_dir())

    def test_metadata_excludes_secrets(self):
        store = Store(self.author_dir, uid=42, author_name="测试UP",
                      redactor=Redactor.from_cookie("SESSDATA=abcdef123456; bili_jct=deadbeef99"))
        store.load()
        state = store.allocate(item())
        store.set_step(state, "fetch", STEP_DONE, message="Cookie: SESSDATA=abcdef123456")
        text = (self.author_dir / state.dir / METADATA_NAME).read_text(encoding="utf-8")
        self.assertNotIn("abcdef123456", text)
        self.assertNotIn("deadbeef99", text)
        self.assertIn("[REDACTED]", text)
        # 状态没有泄露：message 也被脱敏
        data = json.loads(text)
        self.assertNotIn("abcdef123456", json.dumps(data, ensure_ascii=False))


class StepStateTest(StoreTestBase):
    def test_rollup_transitions(self):
        state = self.store.allocate(item())
        self.assertEqual(state.rollup, STEP_PENDING)
        self.store.set_step(state, "fetch", STEP_DONE)
        self.store.set_step(state, "content", STEP_DONE)
        self.store.set_step(state, "images", STEP_SKIPPED, reason="no_images")
        self.store.set_step(state, "render", STEP_SKIPPED, reason="stage2_not_implemented")
        self.assertEqual(state.rollup, STEP_DONE)
        self.store.set_step(state, "content", STEP_FAILED, error_kind="risk_control",
                            error_code=-352, message="风控")
        self.assertEqual(state.rollup, STEP_FAILED)

    def test_failed_step_records_error_and_attempts(self):
        state = self.store.allocate(item())
        self.store.set_step(state, "fetch", STEP_FAILED, error_kind="risk_control",
                            error_code=-352, message="风控校验失败")
        step = state.step("fetch")
        self.assertEqual(step.attempts, 1)
        self.assertEqual(state.errors[0]["code"], -352)
        self.assertEqual(state.errors[0]["kind"], "risk_control")

    def test_pending_steps_and_retry_selection(self):
        state = self.store.allocate(item())
        self.store.set_step(state, "fetch", STEP_DONE)
        self.store.set_step(state, "content", STEP_FAILED, error_kind="network_error",
                            message="网络失败")
        pending = self.store.pending_steps(state)
        self.assertEqual(sorted(pending), ["content", "images", "render"])
        self.assertEqual(self.store.pending_steps(state, {"fetch"}), [])
        self.assertEqual(len(self.store.entries_with_pending()), 1)
        # 全部结算后不再需要补做
        self.store.set_step(state, "content", STEP_DONE)
        self.store.set_step(state, "images", STEP_SKIPPED, reason="no_images")
        self.store.set_step(state, "render", STEP_SKIPPED, reason="stage2_not_implemented")
        self.assertEqual(self.store.entries_with_pending(), [])

    def test_settled_property(self):
        state = self.store.allocate(item())
        self.assertFalse(state.step("fetch").settled)
        self.store.set_step(state, "fetch", STEP_DONE)
        self.assertTrue(state.step("fetch").settled)


class IndexTest(StoreTestBase):
    def test_index_round_trip(self):
        state = self.store.allocate(item())
        self.store.set_step(state, "fetch", STEP_DONE)
        self.store.save_index(last_run={"run_id": "abc"})
        data = json.loads((self.author_dir / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(data["uid"], 42)
        self.assertEqual(data["last_run"]["run_id"], "abc")
        entry = data["entries"][0]
        self.assertEqual(entry["dir"], state.dir)
        self.assertEqual(entry["steps"]["fetch"], STEP_DONE)
        self.assertEqual(entry["status"], STEP_PENDING)

        reloaded = Store(self.author_dir, uid=42, author_name="测试UP").load()
        found = reloaded.get(KIND_DYNAMIC, "d1")
        self.assertIsNotNone(found)
        self.assertEqual(found.step("fetch").status, STEP_DONE)

    def test_rebuild_from_metadata_when_index_missing(self):
        state = self.store.allocate(item(KIND_ARTICLE, "cv1", "专栏标题"))
        self.store.set_step(state, "fetch", STEP_DONE)
        self.store.save_index()
        (self.author_dir / "index.json").unlink()
        rebuilt = Store(self.author_dir, uid=42, author_name="测试UP").load()
        found = rebuilt.get(KIND_ARTICLE, "cv1")
        self.assertIsNotNone(found)
        self.assertEqual(found.title, "专栏标题")
        self.assertEqual(found.step("fetch").status, STEP_DONE)

    def test_rebuild_when_index_corrupt(self):
        self.store.allocate(item(KIND_VIDEO, "BV1", "视频标题"))
        self.store.save_index()
        (self.author_dir / "index.json").write_text("{ 坏 JSON", encoding="utf-8")
        rebuilt = Store(self.author_dir, uid=42, author_name="测试UP").load()
        self.assertIsNotNone(rebuilt.get(KIND_VIDEO, "BV1"))
        self.assertTrue(any("重建" in note or "不可用" in note for note in rebuilt.notes))

    def test_write_run_summary(self):
        self.store.allocate(item())
        path = self.store.write_run_summary({"run_id": "r1", "started_at": "2026-09-23T12:00:00+08:00",
                                             "counts": {"done": 1}})
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["counts"]["done"], 1)

    def test_stats(self):
        state = self.store.allocate(item())
        self.store.set_step(state, "fetch", STEP_DONE)
        self.assertEqual(self.store.stats()["entries"], 1)

    def test_items_from_entries_round_trips_iso_time(self):
        """index 里的 published_at 必须是 ISO（retry 靠它还原发布时间）。"""
        from bili_sub_archive.discover import items_from_entries

        state = self.store.allocate(item(KIND_ARTICLE, "cv9", "旧格式专栏"))
        self.assertEqual(state.published_at, "2026-09-23T12:00:00+08:00")
        restored = items_from_entries([state])[0]
        self.assertIsNotNone(restored.published_at)
        self.assertEqual(restored.kind, KIND_ARTICLE)
        self.assertEqual(restored.platform_id, "cv9")
        self.assertEqual(restored.date_stamp, "2026-09-23")
        self.assertEqual(restored.author, "测试UP")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
