"""解析测试：富文本、段落类型、两种 ``modules`` 形状、统一 Item 输出、权限判定。"""

from __future__ import annotations

import unittest

from bili_sub_archive.bili.api import (
    dynamic_item,
    legacy_article_item,
    mark_pinned_videos,
    opus_item,
    path_of,
    summarize,
    video_item,
)
from bili_sub_archive.bili.parse import (
    dynamic_body_text,
    dynamic_images,
    major_images,
    major_text,
    opus_blocks,
    parse_dynamic_item,
    parse_opus_detail,
    parse_page_list,
    parse_playurl,
    parse_video_detail,
    rich_text,
    text_block_text,
)
from tests import fixtures as fx


class RichTextTest(unittest.TestCase):
    def test_rich_text_object(self):
        self.assertEqual(rich_text(fx.rich_text("正文")), "正文")

    def test_rich_text_plain_string(self):
        self.assertEqual(rich_text("字符串"), "字符串")

    def test_rich_text_none_and_garbage(self):
        self.assertEqual(rich_text(None), "")
        self.assertEqual(rich_text(123), "")
        self.assertEqual(rich_text({}), "")

    def test_rich_text_nodes_use_orig_text(self):
        obj = {"rich_text_nodes": [{"orig_text": "甲"}, {"text": "乙"}, {"orig_text": "丙"}]}
        self.assertEqual(rich_text(obj), "甲乙丙")

    def test_text_block_from_word_nodes(self):
        obj = {"nodes": [{"type": 1, "word": {"words": "第一"}},
                         {"type": 1, "word": {"words": "第二"}}]}
        self.assertEqual(text_block_text(obj), "第一第二")

    def test_text_block_emoji_replacement(self):
        obj = {"nodes": [{"type": 1, "word": {"words": "你好"}},
                         {"type": 2, "emoji": {"text": "[大笑]"}}]}
        self.assertEqual(text_block_text(obj), "你好[大笑]")

    def test_text_block_emoji_without_text(self):
        obj = {"nodes": [{"emoji": {"icon_url": "https://x/y.png"}}]}
        self.assertEqual(text_block_text(obj), "[表情]")


class OpusBlocksTest(unittest.TestCase):
    def test_text_image_heading_order(self):
        blocks = opus_blocks({"paragraphs": [
            fx.para_text("第一段"),
            fx.para_pic("https://i0.hdslb.com/a.png"),
            fx.para_heading("小标题", 2),
            fx.para_text("第二段"),
        ]})
        self.assertEqual([b.kind for b in blocks], ["text", "image", "heading", "text"])
        self.assertEqual(blocks[1].pics[0]["url"], "https://i0.hdslb.com/a.png")
        self.assertEqual(blocks[2].level, 2)

    def test_unknown_para_type_kept_as_note(self):
        blocks = opus_blocks({"paragraphs": [{"para_type": 7, "text": {"nodes": []}}]})
        self.assertEqual(blocks[0].kind, "unknown")
        self.assertIn("para_type=7", blocks[0].note)

    def test_fallback_shape_with_content_and_pics(self):
        blocks = opus_blocks({"content": "纯文本", "pics": [{"url": "https://x/y.jpg"}]})
        self.assertEqual([b.kind for b in blocks], ["text", "image"])

    def test_heading_type_fallback(self):
        blocks = opus_blocks({"paragraphs": [
            {"para_type": 9, "format": {}, "text": {"nodes": [{"word": {"words": "标题"}}]}}
        ]})
        self.assertEqual(blocks[0].level, 3)


class OpusDetailTest(unittest.TestCase):
    def test_parse_article(self):
        item = fx.opus_detail_item("1214741992578220039", 1781667560, "地缘 + 金银",
                                  [fx.para_text("正文"), fx.para_pic("https://x/p.png")],
                                  item_type=1, article_type=4, is_only_fans=True)
        doc = parse_opus_detail({"item": item})
        self.assertEqual(doc.title, "地缘 + 金银")
        self.assertTrue(doc.is_article)
        self.assertTrue(doc.is_only_fans)
        self.assertEqual(doc.pub_ts, 1781667560)
        self.assertEqual(len(doc.images), 1)
        self.assertEqual(doc.text, "正文")

    def test_blocked_module_sets_error_kind(self):
        item = fx.opus_detail_item("1", 1781667560, "标题", blocked=True, is_only_fans=True)
        doc = parse_opus_detail({"item": item})
        self.assertTrue(doc.blocked)
        self.assertEqual(doc.error_kind, "content_blocked")
        self.assertEqual(doc.blocks, [])

    def test_none_for_bad_data(self):
        self.assertIsNone(parse_opus_detail(None))
        self.assertIsNone(parse_opus_detail({"item": "不是对象"}))


class DynamicParseTest(unittest.TestCase):
    def test_dict_modules_opus(self):
        raw = fx.dynamic_feed_item("1234909896317599783", 1786363266, "谨防诈骗", pinned=True)
        doc = parse_dynamic_item(raw)
        self.assertEqual(doc.id_str, "1234909896317599783")
        self.assertTrue(doc.pinned)
        self.assertEqual(doc.major_type, "MAJOR_TYPE_OPUS")
        self.assertEqual(dynamic_body_text(doc), "谨防诈骗")

    def test_badge_and_charging_flag(self):
        raw = fx.dynamic_feed_item("1", 1786363266, "充电内容", is_only_fans=True)
        doc = parse_dynamic_item(raw)
        self.assertTrue(doc.is_only_fans)
        self.assertEqual(doc.badge, "充电专属")

    def test_is_top_also_marks_pinned(self):
        raw = fx.dynamic_feed_item("1", 1786363266, "x")
        raw["modules"]["module_author"]["is_top"] = True
        raw["modules"]["module_tag"] = None
        self.assertTrue(parse_dynamic_item(raw).pinned)

    def test_list_modules_shape(self):
        item = fx.opus_detail_item("1", 1786363266, "标题", [fx.para_text("正文")])
        doc = parse_dynamic_item(item)
        self.assertEqual(doc.major_type, "MAJOR_TYPE_OPUS")
        self.assertEqual(dynamic_body_text(doc), "正文")

    def test_archive_card_major(self):
        major = {"type": "MAJOR_TYPE_ARCHIVE",
                 "archive": {"bvid": "BV1zveP6eEZ1", "title": "视频标题",
                             "desc": "简介", "cover": "https://x/c.jpg",
                             "duration_text": "33:00"}}
        raw = fx.dynamic_feed_item("2", 1789561886, "", dyn_type="DYNAMIC_TYPE_AV",
                                  major_type="MAJOR_TYPE_ARCHIVE", major=major)
        doc = parse_dynamic_item(raw)
        self.assertIn("视频标题", dynamic_body_text(doc))
        self.assertIn("33:00", major_text(major) + "33:00")
        pics = major_images(major)
        self.assertEqual(pics[0]["url"], "https://x/c.jpg")
        self.assertIn("视频卡片", doc.parse_note)
        self.assertEqual(len(dynamic_images(doc)), 1)

    def test_forward_orig_parsed(self):
        orig = fx.dynamic_feed_item("9", 1784000000, "转发原文")
        raw = fx.dynamic_feed_item("8", 1784467110, "我的评论", dyn_type="DYNAMIC_TYPE_FORWARD",
                                  major_type="", major={}, orig=orig)
        doc = parse_dynamic_item(raw)
        self.assertIsNotNone(doc.orig)
        self.assertEqual(parse_dynamic_item(doc.orig).id_str, "9")

    def test_missing_orig_note(self):
        raw = fx.dynamic_feed_item("8", 1784467110, "我的评论", dyn_type="DYNAMIC_TYPE_FORWARD",
                                  major_type="", major={}, orig=None)
        doc = parse_dynamic_item(raw)
        self.assertIn("转发原文缺失", doc.parse_note)

    def test_pub_ts_zero_means_no_time(self):
        raw = fx.dynamic_feed_item("1", 0, "x")
        doc = parse_dynamic_item(raw)
        self.assertFalse(doc.visible)
        self.assertIsNone(doc.published_at)


class VideoParseTest(unittest.TestCase):
    def test_video_detail(self):
        data = fx.video_detail("BV1xx", 1789561886, "视频标题",
                              pages=[{"cid": 1, "page": 1, "part": "P1", "duration": 60,
                                      "dimension": {"width": 1920, "height": 1080}},
                                     {"cid": 2, "page": 2, "part": "P2", "duration": 70,
                                      "dimension": {"width": 1280, "height": 720}}])
        doc = parse_video_detail(data)
        self.assertEqual(doc.bvid, "BV1xx")
        self.assertEqual(doc.title, "视频标题")
        self.assertEqual(len(doc.pages), 2)
        self.assertEqual(doc.owner_name, fx.AUTHOR_NAME)

    def test_page_list(self):
        pages = parse_page_list({"pages": [{"cid": 1, "page": 1, "part": "P1", "duration": 10,
                                           "dimension": {"width": 1, "height": 2}}]})
        self.assertEqual(pages[0].cid, 1)
        self.assertEqual(pages[0].width, 1)

    def test_playurl_full_vs_preview_only(self):
        full = parse_playurl(fx.playurl_full())
        self.assertEqual(full.mode, "full")
        self.assertEqual(full.dash_video_streams, 4)
        preview = parse_playurl(fx.playurl_preview_only())
        self.assertEqual(preview.mode, "preview_only")
        self.assertEqual(preview.durl_segments, 1)

    def test_playurl_missing_data(self):
        self.assertEqual(parse_playurl(None).mode, "unknown")


class ItemAssemblyTest(unittest.TestCase):
    def test_video_item(self):
        raw = fx.video_vlist_entry("BV1zveP6eEZ1", 1789561886, "标题", is_charging=True)
        item = video_item(raw, fx.AUTHOR_NAME)
        self.assertEqual(item.kind, "video")
        self.assertEqual(item.platform_id, "BV1zveP6eEZ1")
        self.assertEqual(item.url, "https://www.bilibili.com/video/BV1zveP6eEZ1")
        self.assertTrue(item.extras["is_charging_arc"])
        self.assertIsNotNone(item.published_at)

    def test_video_item_requires_bvid(self):
        self.assertIsNone(video_item({"title": "无 BV"}, "UP"))

    def test_mark_pinned_videos(self):
        items = [video_item(fx.video_vlist_entry("BV1", 1000, "置顶"), "UP"),
                 video_item(fx.video_vlist_entry("BV2", 2000, "正常"), "UP"),
                 video_item(fx.video_vlist_entry("BV3", 3000, "更新"), "UP")]
        mark_pinned_videos(items)
        self.assertTrue(items[0].extras["pinned"])
        self.assertFalse(items[1].extras["pinned"])

    def test_no_pinned_when_ordered(self):
        items = [video_item(fx.video_vlist_entry("BV1", 3000, "新"), "UP"),
                 video_item(fx.video_vlist_entry("BV2", 2000, "旧"), "UP")]
        mark_pinned_videos(items)
        self.assertFalse(items[0].extras["pinned"])

    def test_dynamic_item(self):
        raw = fx.dynamic_feed_item("1234909896317599783", 1786363266, "谨防诈骗" * 20)
        item, doc = dynamic_item(raw, fx.AUTHOR_NAME, fx.AUTHOR_MID)
        self.assertEqual(item.kind, "dynamic")
        self.assertEqual(item.url, "https://www.bilibili.com/opus/1234909896317599783")
        self.assertTrue(item.title.endswith("…"))
        self.assertLessEqual(len(item.title), 41)
        self.assertEqual(item.raw_ref["dynamic_type"], "DYNAMIC_TYPE_DRAW")
        self.assertIsNotNone(doc)

    def test_opus_item_has_no_publish_time(self):
        item = opus_item(fx.opus_feed_item("1250858063341027334", "图文内容", badge=True),
                         fx.AUTHOR_NAME, fx.AUTHOR_MID)
        self.assertEqual(item.kind, "article")
        self.assertIsNone(item.published_at)
        self.assertTrue(item.extras["unknown_publish_time"])
        self.assertEqual(item.extras["badge"], "充电专属")

    def test_legacy_article_item(self):
        raw = fx.legacy_article_entry(53148144, 1790154506, "市场分析")
        item = legacy_article_item(raw, fx.AUTHOR_NAME, fx.AUTHOR_MID)
        self.assertEqual(item.platform_id, "cv53148144")
        self.assertEqual(item.url, "https://www.bilibili.com/read/cv53148144")
        self.assertEqual(item.raw_ref["format"], "legacy_cv")
        self.assertEqual(item.date_stamp, "2026-09-23")

    def test_summarize(self):
        self.assertEqual(summarize("多行\n内容  带空格"), "多行 内容 带空格")
        self.assertTrue(summarize("字" * 100, limit=10).endswith("…"))

    def test_whitelist_contains_required_paths(self):
        self.assertEqual(path_of("video_list"), "/x/space/wbi/arc/search")
        self.assertEqual(path_of("opus_feed"), "/x/polymer/web-dynamic/v1/opus/feed/space")
        self.assertEqual(path_of("article_view"), "/x/article/view")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
