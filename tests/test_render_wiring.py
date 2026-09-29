"""动态长图渲染的**接线**测试：真实阶段 0 响应 → 渲染项 → 长 PNG。

与 ``test_render.py``（渲染器自身）互补：这里验证 ``archive/dynamic.py`` 把
真实 API 响应正确翻译成渲染项（章节顺序、图片序号、占位说明），并真的产出一张
合法 PNG。

数据来自 ``docs/archive/stage0-evidence/raw/dynamic_detail__auth.json``（阶段 0 探针保存的
**真实**响应）。注意：探针把长数组摘要化了（``"pics": [".", ".", "."]``），
因此"图片接线"部分用注入的图片条目补全，其余字段一律用真实值。
"""

from __future__ import annotations

import json
import unittest

from bili_sub_archive.archive.dynamic import _collect_occurrences, _render_image_item, _render_items
from bili_sub_archive.bili.parse import parse_dynamic_item
from tests import ROOT, drop_workspace, fixtures as fx, make_workspace

EVIDENCE = ROOT / "docs" / "archive" / "stage0-evidence" / "raw" / "dynamic_detail__auth.json"

PICS = [
    {"url": "http://i0.hdslb.com/bfs/new_dyn/p1.png", "width": 1200, "height": 800, "size": 1, "type": 1},
    {"url": "http://i0.hdslb.com/bfs/new_dyn/p2.png", "width": 900, "height": 1400, "size": 1, "type": 1},
    {"url": "http://i0.hdslb.com/bfs/new_dyn/p3.png", "width": 1200, "height": 640, "size": 1, "type": 1},
]


class StubPlan:
    """最小 ``path_for`` 实现：模拟 ImagePlan（部分成功、部分失败）。"""

    def __init__(self, mapping: dict[int, str]):
        self.mapping = mapping

    def path_for(self, index: int) -> str:
        return self.mapping.get(index, "")


def load_real_doc(*, with_pics: bool = False):
    """读取阶段 0 的真实动态详情响应并解析；``with_pics`` 时补上图片条目。"""
    if not EVIDENCE.is_file():
        return None
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    item = ((payload.get("data") or {}).get("item")) if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        return None
    if with_pics:
        major = item["modules"]["module_dynamic"]["major"]
        major["opus"]["pics"] = [dict(p) for p in PICS]
    return parse_dynamic_item(item)


@unittest.skipUnless(EVIDENCE.is_file(), "缺少阶段 0 证据文件")
class RealEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("render_wire")
        self.entry = self.root / "entry"
        (self.entry / "images").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        drop_workspace(self.root)

    def test_real_response_uses_detail_dict_shape(self):
        """真实响应是 ``/detail`` 的 dict 形状（阶段 0 契约第 7 条）。"""
        doc = load_real_doc()
        self.assertIsNotNone(doc)
        self.assertEqual(doc.id_str, "1234909896317599783")
        self.assertEqual(doc.dyn_type, "DYNAMIC_TYPE_DRAW")
        self.assertEqual(doc.major_type, "MAJOR_TYPE_OPUS")
        self.assertEqual(doc.author_name, "战国时代_姜汁汽水")
        self.assertEqual(doc.author_mid, 1039025435)
        self.assertGreater(doc.pub_ts, 0, "真实响应应带发布时间")

    def test_render_items_cover_images_with_placeholders(self):
        doc = load_real_doc(with_pics=True)
        layout = _collect_occurrences(doc)
        self.assertEqual(layout.main_card, 3)
        self.assertEqual(layout.total, 3)

        # 只有前两张真的落盘了；第三张缺失 → 必须走占位说明
        for name in ("01.png", "02.png"):
            (self.entry / "images" / name).write_bytes(fx.PNG_1PX)
        plan = StubPlan({1: "images/01.png", 2: "images/02.png"})
        items = _render_items(doc, plan, layout, self.entry, None)
        rendered = [i for i in items if i.kind == "image"]
        self.assertEqual(len(rendered), 3, "3 张配图都要有渲染项（含缺失占位）")
        self.assertTrue(rendered[0].image.endswith("01.png"))
        self.assertTrue(rendered[1].image.endswith("02.png"))
        self.assertEqual(rendered[2].image, "")
        self.assertIn("未取得", rendered[2].caption)

    def test_end_to_end_png_from_real_content(self):
        """真实字段 + 真实中文图片 → 合法长 PNG（图片现场生成，不联网）。"""
        from PIL import Image

        from bili_sub_archive.render import RenderHeader, render_long_png

        doc = load_real_doc(with_pics=True)
        layout = _collect_occurrences(doc)
        for name, color in (("01.png", (200, 60, 60)), ("02.png", (60, 120, 200))):
            Image.new("RGB", (1200, 800), color).save(self.entry / "images" / name)
        plan = StubPlan({1: "images/01.png", 2: "images/02.png"})
        items = _render_items(doc, plan, layout, self.entry, None)

        header = RenderHeader(
            author=doc.author_name, avatar="", published="2026-01-01 00:00:00",
            title="真实响应渲染：中文标题与正文排版检查", url="https://www.bilibili.com/opus/123",
            badge="充电专属", visible="充电专属", stats="点赞 384 / 评论 178 / 转发 0",
        )
        out = self.entry / "render.png"
        result = render_long_png(header, items, out, width=1080)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.images_rendered, 2)
        self.assertEqual(result.images_missing, 1)
        with Image.open(out) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1080, result.height))
        self.assertGreater(result.height, 800, "两张 1200×800 图 + 头部应显著高于 800px")


class ImageItemTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("render_item")
        (self.root / "images").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        drop_workspace(self.root)

    def test_local_file_wins(self):
        (self.root / "images" / "01.jpg").write_bytes(fx.PNG_1PX)
        item = _render_image_item(StubPlan({1: "images/01.jpg"}), 1, self.root)
        self.assertEqual(item.kind, "image")
        self.assertTrue(item.image.endswith("01.jpg"))
        self.assertEqual(item.caption, "图片1")

    def test_missing_file_falls_back_to_caption(self):
        item = _render_image_item(StubPlan({1: "images/gone.jpg"}), 1, self.root)
        self.assertEqual(item.image, "")
        self.assertIn("未取得", item.caption)
        self.assertIn("gone.jpg", item.caption)

    def test_remote_url_is_not_treated_as_local(self):
        item = _render_image_item(StubPlan({1: "https://i0.hdslb.com/x.png"}), 1, self.root)
        self.assertEqual(item.image, "")
        self.assertIn("i0.hdslb.com", item.caption)

    def test_no_plan_is_handled(self):
        item = _render_image_item(None, 1, self.root, "卡片图")
        self.assertEqual(item.image, "")
        self.assertIn("卡片图1", item.caption)

    def test_prefix_is_configurable(self):
        item = _render_image_item(None, 2, self.root, "转发图")
        self.assertIn("转发图2", item.caption)


class ForwardRenderTest(unittest.TestCase):
    """转发动态：转发原文也要进长图（需求 3.2.3）。"""

    def setUp(self):
        self.root = make_workspace("render_fwd")
        self.entry = self.root / "entry"
        (self.entry / "images").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        drop_workspace(self.root)

    def test_forward_section_is_rendered(self):
        orig = fx.dynamic_feed_item("999", 1750000000, "转发原文内容",
                                    major={"type": "MAJOR_TYPE_OPUS",
                                           "opus": {"title": "", "summary": fx.rich_text("转发原文内容"),
                                                    "pics": [{"url": "https://i0.hdslb.com/o.png",
                                                              "width": 100, "height": 50}]}})
        raw = fx.dynamic_feed_item("123", 1750000001, "我的转发评论",
                                   dyn_type="DYNAMIC_TYPE_FORWARD", major_type="", major={},
                                   orig=orig)
        doc = parse_dynamic_item(raw)
        orig_doc = parse_dynamic_item(doc.orig)
        layout = _collect_occurrences(doc, orig_doc)
        self.assertEqual(layout.orig_card, 1)

        items = _render_items(doc, StubPlan({}), layout, self.entry, orig_doc)
        headings = [i.text for i in items if i.kind == "heading"]
        notes = [i.text for i in items if i.kind == "note"]
        self.assertIn("转发原文", headings)
        self.assertTrue(any("原作者" in n for n in notes))
        self.assertTrue(any("转发原文内容" == i.text for i in items if i.kind == "text"))
        # 转发配图缺失 → 占位项，而不是静默消失
        self.assertTrue(any("转发图1" in i.caption for i in items if i.kind == "image"))

    def test_forward_missing_is_noted(self):
        raw = fx.dynamic_feed_item("123", 1750000001, "我的转发评论",
                                   dyn_type="DYNAMIC_TYPE_FORWARD", major_type="", major={},
                                   orig=None)
        doc = parse_dynamic_item(raw)
        layout = _collect_occurrences(doc, None)
        items = _render_items(doc, StubPlan({}), layout, self.entry, None)
        self.assertTrue(any("转发原文缺失" in i.text for i in items if i.kind == "note"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
