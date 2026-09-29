"""阶段 2 文字稿测试：字幕解析、SRT/纯文本序列化、ASR 兜底、跨 P 合并。

全部离线：字幕来自 :class:`tests.fixtures.FakeApi` 的合成响应，ffmpeg 与
faster-whisper 都用注入替身，**不联网、不跑模型、不调用真实 ffmpeg**。
"""

from __future__ import annotations

import json
import unittest

from bili_sub_archive.bili.parse import PageInfo
from bili_sub_archive.config import Config
from bili_sub_archive.errors import KIND_AUTH, KIND_PARSE
from bili_sub_archive.transcript import (
    REASON_DEPENDENCY_MISSING,
    REASON_NO_TRANSCRIPT,
    build_transcript,
    extract_audio,
    fetch_page_subtitle,
    merge_srt,
    merge_text,
    parse_bili_subtitle,
    parse_srt,
    pick_subtitle,
    segments_to_srt,
    segments_to_text,
    subtitle_entries,
)
from bili_sub_archive.transcript.asr import FasterWhisperTranscriber, transcribe_page
from bili_sub_archive.transcript.srt import PageTranscript, Segment, format_timestamp, parse_timestamp
from tests import drop_workspace, fixtures as fx, make_workspace

SUB_URL = "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/fake-zh.json"
ROWS = [(0.24, 0.76, "大家好"), (0.76, 1.92, "今天我们来聊几个话题"), (2.0, 4.5, "第三句")]


def pages(*specs: tuple[int, int, str]) -> list[PageInfo]:
    return [PageInfo(cid=cid, page=page, part=part, duration=600) for page, cid, part in specs]


def api_with_subtitles(cid: int = 1001, rows=None, *, lan="ai-zh", is_lock=False,
                       url: str = SUB_URL) -> fx.FakeApi:
    entries = [fx.subtitle_entry(lan=lan, url=url, is_lock=is_lock)]
    bodies = {url.replace("//", "https://"): fx.subtitle_body(rows or ROWS)}
    return fx.FakeApi(subtitles={str(cid): entries}, subtitle_bodies=bodies)


class TimestampTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_timestamp(0), "00:00:00,000")
        self.assertEqual(format_timestamp(0.24), "00:00:00,240")
        self.assertEqual(format_timestamp(61.5), "00:01:01,500")
        self.assertEqual(format_timestamp(3661.007), "01:01:01,007")
        self.assertEqual(format_timestamp(-5), "00:00:00,000")
        self.assertEqual(format_timestamp("abc"), "00:00:00,000")

    def test_parse_round_trip(self):
        for value in (0.0, 0.24, 61.5, 3661.007, 7200.999):
            self.assertAlmostEqual(parse_timestamp(format_timestamp(value)), value, places=3)

    def test_parse_tolerates_dot_separator_and_junk(self):
        self.assertAlmostEqual(parse_timestamp("00:00:01.500 --> 00:00:03.000"), 1.5)
        self.assertEqual(parse_timestamp("nonsense"), 0.0)


class SubtitleParseTest(unittest.TestCase):
    def test_parse_body(self):
        segments = parse_bili_subtitle(fx.subtitle_body(ROWS), page=2)
        self.assertEqual(len(segments), 3)
        self.assertAlmostEqual(segments[0].start, 0.24)
        self.assertAlmostEqual(segments[0].end, 0.76)
        self.assertEqual(segments[0].text, "大家好")
        self.assertEqual(segments[0].page, 2)
        self.assertEqual(segments[0].source, "subtitle")

    def test_parse_skips_blank_content(self):
        payload = {"body": [{"from": 0, "to": 1, "content": "  "}, {"from": 1, "to": 2, "content": "有字"}]}
        segments = parse_bili_subtitle(payload)
        self.assertEqual([s.text for s in segments], ["有字"])

    def test_parse_rejects_bad_shapes(self):
        for payload in (None, [], {}, {"body": "x"}, {"body": [None, 5]}):
            self.assertEqual(parse_bili_subtitle(payload), [])

    def test_parse_accepts_bare_list(self):
        segments = parse_bili_subtitle([{"from": 0, "to": 1, "content": "裸数组"}])
        self.assertEqual(len(segments), 1)


class SrtSerializeTest(unittest.TestCase):
    def setUp(self):
        self.segments = [
            Segment(start=0.0, end=1.0, text="第一句", page=1),
            Segment(start=1.0, end=2.0, text="第二句", page=1),
        ]

    def test_basic_srt(self):
        text = segments_to_srt(self.segments)
        self.assertIn("1\n00:00:00,000 --> 00:00:01,000\n第一句", text)
        self.assertIn("2\n00:00:01,000 --> 00:00:02,000\n第二句", text)
        self.assertTrue(text.endswith("\n"))

    def test_page_prefix(self):
        text = segments_to_srt(self.segments, page_prefix=True)
        self.assertIn("[P01] 第一句", text)

    def test_marker_and_index_start(self):
        text = segments_to_srt(self.segments, page_prefix=True,
                               marker="【P01】测试 · 时间基准：本 P 起点", index_start=7)
        self.assertIn("7\n00:00:00,000 --> 00:00:00,000\n【P01】测试", text)
        self.assertIn("8\n00:00:00,000 --> 00:00:01,000\n[P01] 第一句", text)

    def test_empty_segments_returns_empty(self):
        self.assertEqual(segments_to_srt([]), "")
        self.assertEqual(segments_to_srt([Segment(start=0, end=1, text="   ")]), "")

    def test_parse_round_trip(self):
        original = [Segment(start=0.0, end=1.5, text="甲", page=3),
                    Segment(start=1.5, end=2.0, text="乙", page=3)]
        parsed = parse_srt(segments_to_srt(original, page_prefix=True))
        self.assertEqual([s.text for s in parsed], ["甲", "乙"])
        self.assertEqual([s.page for s in parsed], [3, 3])
        self.assertAlmostEqual(parsed[0].end, 1.5)

    def test_parse_srt_tolerates_garbage(self):
        self.assertEqual(parse_srt(""), [])
        self.assertEqual(parse_srt("没有时间轴的文本"), [])


class TextParagraphTest(unittest.TestCase):
    def test_splits_on_gap(self):
        segments = [
            Segment(start=0.0, end=1.0, text="A"),
            Segment(start=1.0, end=2.0, text="B"),
            Segment(start=30.0, end=31.0, text="C"),
        ]
        self.assertEqual(segments_to_text(segments), "AB\n\nC")

    def test_splits_on_length(self):
        segments = [Segment(start=float(i), end=float(i) + 0.5, text="字" * 60) for i in range(10)]
        out = segments_to_text(segments, paragraph_chars=100)
        self.assertGreater(len(out.split("\n\n")), 1)

    def test_splits_on_span(self):
        segments = [Segment(start=float(i) * 30, end=float(i) * 30 + 1, text=f"句{i}")
                    for i in range(5)]
        out = segments_to_text(segments, paragraph_chars=10000, max_gap=100, max_span=60)
        self.assertGreater(len(out.split("\n\n")), 1)

    def test_empty(self):
        self.assertEqual(segments_to_text([]), "")


class PickSubtitleTest(unittest.TestCase):
    def test_prefers_configured_order(self):
        entries = [fx.subtitle_entry("ai-en"), fx.subtitle_entry("ai-zh"), fx.subtitle_entry("zh-CN")]
        self.assertEqual(pick_subtitle(entries)["lan"], "zh-CN")

    def test_skips_locked(self):
        entries = [fx.subtitle_entry("zh-CN", is_lock=True), fx.subtitle_entry("ai-zh")]
        self.assertEqual(pick_subtitle(entries)["lan"], "ai-zh")

    def test_all_locked_returns_none(self):
        self.assertIsNone(pick_subtitle([fx.subtitle_entry("ai-zh", is_lock=True)]))

    def test_empty_returns_none(self):
        self.assertIsNone(pick_subtitle([]))

    def test_falls_back_to_any_zh_then_first(self):
        self.assertEqual(pick_subtitle([fx.subtitle_entry("zh-Hant-x")])["lan"], "zh-Hant-x")
        self.assertEqual(pick_subtitle([fx.subtitle_entry("fr")])["lan"], "fr")


class SubtitleEntriesTest(unittest.TestCase):
    def test_two_wrapping_shapes(self):
        page = fx.subtitle_list_page([fx.subtitle_entry("ai-zh")])
        self.assertEqual(len(subtitle_entries(fx.ok(page))), 1)
        # 兼容 data 直接就是 subtitle 块
        self.assertEqual(len(subtitle_entries(fx.ok(page["subtitle"]))), 1)

    def test_bad_shapes_return_empty(self):
        for data in (None, [], {}, {"subtitle": {"subtitles": "x"}}):
            self.assertEqual(subtitle_entries(fx.ok(data)), [])


class FetchPageSubtitleTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("subtitle")
        self.entry = self.root / "entry"
        self.entry.mkdir(parents=True, exist_ok=True)
        self.page = pages((1, 1001, "P1"))[0]

    def tearDown(self):
        drop_workspace(self.root)

    def test_success_writes_files(self):
        record = fetch_page_subtitle(api_with_subtitles(), bvid="BV1", page=self.page,
                                     cid=1001, entry_dir=self.entry)
        self.assertTrue(record.has_text)
        self.assertEqual(record.source, "subtitle")
        self.assertEqual(record.lang, "ai-zh")
        self.assertEqual(len(record.segments), 3)
        self.assertEqual(record.chars, len("大家好") + len("今天我们来聊几个话题") + len("第三句"))
        self.assertTrue((self.entry / "transcript" / "P01.subtitle.json").is_file())
        self.assertTrue((self.entry / "transcript" / "P01.srt").is_file())
        self.assertTrue((self.entry / "transcript" / "P01.txt").is_file())
        self.assertEqual(record.raw_name, "transcript/P01.subtitle.json")
        srt = (self.entry / "transcript" / "P01.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:00,240 --> 00:00:00,760", srt)

    def test_no_subtitle_records_reason_not_error(self):
        api = fx.FakeApi()          # 空字幕列表
        record = fetch_page_subtitle(api, bvid="BV1", page=self.page, cid=1001,
                                     entry_dir=self.entry)
        self.assertFalse(record.has_text)
        self.assertEqual(record.skipped_reason, "no_subtitle")
        self.assertEqual(record.error_kind, "", "没有字幕不是错误")
        self.assertIn("没有平台字幕", record.message)

    def test_api_failure_is_reported(self):
        api = fx.FakeApi()
        api.subtitle_list = lambda bvid, cid: fx.fail(-101, "账号未登录")
        record = fetch_page_subtitle(api, bvid="BV1", page=self.page, cid=1001,
                                     entry_dir=self.entry)
        self.assertFalse(record.has_text)
        self.assertEqual(record.error_kind, KIND_AUTH)
        self.assertIn("字幕列表接口失败", record.message)

    def test_download_failure_is_reported(self):
        api = api_with_subtitles(url="//aisubtitle.hdslb.com/broken.json")
        record = fetch_page_subtitle(api, bvid="BV1", page=self.page, cid=1001,
                                     entry_dir=self.entry)
        self.assertFalse(record.has_text)
        self.assertIn("字幕文件下载失败", record.message)

    def test_bad_json_body_is_reported(self):
        url = SUB_URL
        api = fx.FakeApi(
            subtitles={"1001": [fx.subtitle_entry(url=url)]},
            subtitle_bodies={url.replace("//", "https://"): {"unexpected": True}},
        )
        record = fetch_page_subtitle(api, bvid="BV1", page=self.page, cid=1001,
                                     entry_dir=self.entry)
        self.assertFalse(record.has_text)
        self.assertEqual(record.error_kind, KIND_PARSE)
        self.assertIn("结构不符合预期", record.message)

    def test_locked_only_reports_locked(self):
        api = api_with_subtitles(is_lock=True)
        record = fetch_page_subtitle(api, bvid="BV1", page=self.page, cid=1001,
                                     entry_dir=self.entry)
        self.assertEqual(record.skipped_reason, "no_subtitle")
        self.assertIn("锁定", record.message)


class ExtractAudioTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("audio")
        self.media = self.root / "videos" / "P01.mp4"
        self.media.parent.mkdir(parents=True, exist_ok=True)
        self.media.write_bytes(fx.MP4_MIN)
        self.dest = self.root / "transcript" / "P01.audio.wav"

    def tearDown(self):
        drop_workspace(self.root)

    def test_missing_ffmpeg(self):
        ok, note = extract_audio("", self.media, self.dest)
        self.assertFalse(ok)
        self.assertIn("未找到 ffmpeg", note)

    def test_missing_source(self):
        ok, note = extract_audio("ffmpeg", self.root / "nope.mp4", self.dest)
        self.assertFalse(ok)
        self.assertIn("媒体文件不存在", note)

    def test_runner_success(self):
        created = []
        ok, note = extract_audio("ffmpeg", self.media, self.dest,
                                 runner=fx.fake_ffmpeg_runner(created))
        self.assertTrue(ok)
        self.assertTrue(self.dest.is_file())
        argv = created[0]
        self.assertIn("-vn", argv)
        self.assertIn("16000", argv)
        self.assertIn("-nostdin", argv, "必须带 -nostdin，否则可能挂住")

    def test_runner_failure(self):
        ok, note = extract_audio("ffmpeg", self.media, self.dest,
                                 runner=lambda argv: (1, "", "Invalid data found"))
        self.assertFalse(ok)
        self.assertIn("Invalid data found", note)

    def test_runner_raising_is_contained(self):
        def boom(argv):
            raise OSError("sandbox denied")

        ok, note = extract_audio("ffmpeg", self.media, self.dest, runner=boom)
        self.assertFalse(ok)
        self.assertIn("ffmpeg 调用失败", note)


class TranscriberTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("asr")
        self.entry = self.root / "entry"
        (self.entry / "videos").mkdir(parents=True, exist_ok=True)
        (self.entry / "videos" / "P01.mp4").write_bytes(fx.MP4_MIN)
        self.page = pages((1, 1001, "P1"))[0]

    def tearDown(self):
        drop_workspace(self.root)

    def test_missing_media_reports_actionable_message(self):
        (self.entry / "videos" / "P01.mp4").unlink()
        outcome = transcribe_page(fx.FakeTranscriber(), entry_dir=self.entry, page=self.page,
                                  ffmpeg="ffmpeg")
        self.assertFalse(outcome.ok)
        self.assertIn("先补做 media 步骤", outcome.message)

    def test_missing_ffmpeg(self):
        outcome = transcribe_page(fx.FakeTranscriber(), entry_dir=self.entry, page=self.page,
                                  ffmpeg="")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, REASON_DEPENDENCY_MISSING)
        self.assertIn("ffmpeg", outcome.message)

    def test_success_and_audio_cleanup(self):
        outcome = transcribe_page(fx.FakeTranscriber(text="转写"), entry_dir=self.entry,
                                  page=self.page, ffmpeg="ffmpeg",
                                  runner=fx.fake_ffmpeg_runner())
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.segments), 2)
        self.assertTrue(all(s.source == "asr" for s in outcome.segments))
        self.assertFalse((self.entry / "transcript" / "P01.audio.wav").exists(),
                         "默认应清理中间音频")

    def test_keep_audio_flag(self):
        outcome = transcribe_page(fx.FakeTranscriber(), entry_dir=self.entry, page=self.page,
                                  ffmpeg="ffmpeg", keep_audio=True,
                                  runner=fx.fake_ffmpeg_runner())
        self.assertTrue(outcome.ok)
        self.assertTrue((self.entry / "transcript" / "P01.audio.wav").is_file())
        self.assertEqual(outcome.audio_name, "P01.audio.wav")

    def test_unavailable_transcriber_is_dependency_error(self):
        outcome = transcribe_page(fx.FakeTranscriber(available=False), entry_dir=self.entry,
                                  page=self.page, ffmpeg="ffmpeg")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, REASON_DEPENDENCY_MISSING)

    def test_real_transcriber_reports_missing_dependency(self):
        """本机没装 faster-whisper 时：如实报 dependency_missing，不抛异常。"""
        transcriber = FasterWhisperTranscriber()
        ok, note = transcriber.probe()
        if ok:  # pragma: no cover - 装了 faster-whisper 的环境
            self.skipTest("本机已安装 faster-whisper")
        self.assertFalse(ok)
        self.assertIn("faster-whisper", note)
        outcome = transcriber.transcribe(self.entry / "videos" / "P01.mp4", page=1)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, REASON_DEPENDENCY_MISSING)


class MergeTest(unittest.TestCase):
    def make_pages(self) -> list[PageTranscript]:
        p1 = PageTranscript(page=1, cid=1001, part="P1", duration=600, source="subtitle",
                            lang="ai-zh", lang_label="中文（AI 生成）",
                            segments=[Segment(0.0, 1.0, "P1 第一句", page=1, source="subtitle"),
                                      Segment(1.0, 2.0, "P1 第二句", page=1, source="subtitle")])
        p2 = PageTranscript(page=2, cid=1002, part="P2", duration=300, source="asr",
                            lang="zh", lang_label="本地 ASR（small）",
                            segments=[Segment(0.0, 1.0, "P2 第一句", page=2, source="asr")])
        return [p1, p2]

    def test_merge_text_marks_source_and_pages(self):
        text = merge_text(self.make_pages())
        self.assertIn("## P01 P1", text)
        self.assertIn("## P02 P2", text)
        self.assertIn("平台字幕", text)
        self.assertIn("本地 ASR", text)
        self.assertIn("P1 第一句", text)
        self.assertIn("P2 第一句", text)
        self.assertIn("各 P 内部计时", text)

    def test_merge_srt_marks_page_and_time_base(self):
        srt = merge_srt(self.make_pages())
        self.assertIn("[P01] P1 第一句", srt)
        self.assertIn("[P02] P2 第一句", srt)
        self.assertIn("时间基准：本 P 起点", srt)
        self.assertIn("各 P 未拼接", srt)
        # 序号连续（标记 cue + 正文 cue 依次递增）
        indexes = [line for line in srt.splitlines() if line.strip().isdigit()]
        self.assertEqual(indexes, ["1", "2", "3", "4", "5"])

    def test_merge_skips_pages_without_text(self):
        pages = self.make_pages() + [PageTranscript(page=3, cid=1003, part="P3", source="")]
        srt = merge_srt(pages)
        self.assertNotIn("[P03]", srt)
        self.assertNotIn("## P03", merge_text(pages))


class BuildTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("build_transcript")
        self.entry = self.root / "entry"
        (self.entry / "videos").mkdir(parents=True, exist_ok=True)
        (self.entry / "videos" / "P01.mp4").write_bytes(fx.MP4_MIN)
        (self.entry / "videos" / "P02.mp4").write_bytes(fx.MP4_MIN)
        self.page_list = pages((1, 1001, "P1"), (2, 1002, "P2"))

    def tearDown(self):
        drop_workspace(self.root)

    def build(self, api, **cfg):
        config = Config(uid=1000)
        config.asr_enabled = cfg.pop("asr_enabled", False)
        for key, value in cfg.items():
            setattr(config, key, value)
        return build_transcript(
            api=api, bvid="BV1", pages=self.page_list, entry_dir=self.entry,
            config=config, transcriber=cfg.pop("_transcriber", None),
            ffmpeg=cfg.pop("_ffmpeg", ""), runner=cfg.pop("_runner", None),
        )

    def test_subtitle_path_is_primary(self):
        api = fx.FakeApi(
            subtitles={"1001": [fx.subtitle_entry(url=SUB_URL)],
                       "1002": [fx.subtitle_entry(url=SUB_URL)]},
            subtitle_bodies={SUB_URL.replace("//", "https://"): fx.subtitle_body(ROWS)},
        )
        plan = self.build(api)
        self.assertTrue(plan.has_text)
        self.assertEqual(plan.sources, ["subtitle"])
        self.assertEqual(plan.pages_with_text, 2)
        self.assertTrue((self.entry / "transcript.txt").is_file())
        self.assertTrue((self.entry / "transcript_timed.srt").is_file())
        self.assertIn("[P01]", (self.entry / "transcript_timed.srt").read_text(encoding="utf-8"))
        self.assertIn("[P02]", (self.entry / "transcript_timed.srt").read_text(encoding="utf-8"))

    def test_no_subtitle_and_asr_off_is_skipped_but_redoable(self):
        plan = self.build(fx.FakeApi(), asr_enabled=False)
        self.assertFalse(plan.has_text)
        self.assertEqual(plan.status, "skipped")
        self.assertEqual(plan.reason, "asr_disabled",
                         "应记 asr_disabled（可重做），而不是终态 no_transcript")
        self.assertIn("不生成虚构文字稿", plan.message)
        self.assertFalse((self.entry / "transcript.txt").exists())
        self.assertTrue(any("--asr" in n for n in plan.notes))

    def test_asr_fallback_produces_transcript(self):
        plan = self.build(fx.FakeApi(), asr_enabled=True, _transcriber=fx.FakeTranscriber(),
                          _ffmpeg="ffmpeg", _runner=fx.fake_ffmpeg_runner())
        self.assertTrue(plan.has_text)
        self.assertEqual(plan.sources, ["asr"])
        self.assertEqual(plan.pages_with_text, 2)
        text = (self.entry / "transcript.txt").read_text(encoding="utf-8")
        self.assertIn("本地 ASR", text)
        self.assertIn("替身转写文本", text)

    def test_asr_missing_dependency_fails_with_actionable_message(self):
        plan = self.build(fx.FakeApi(), asr_enabled=True,
                          _transcriber=fx.FakeTranscriber(available=False))
        self.assertFalse(plan.has_text)
        # 缺依赖是**硬失败**（装好后 retry 就能补做），不是良性跳过
        self.assertEqual(plan.status, "failed")
        self.assertEqual(plan.error_kind, REASON_DEPENDENCY_MISSING)
        self.assertEqual(plan.reason, REASON_DEPENDENCY_MISSING)
        self.assertIn("faster-whisper", plan.message)

    def test_subtitle_wins_over_asr(self):
        api = fx.FakeApi(
            subtitles={"1001": [fx.subtitle_entry(url=SUB_URL)]},
            subtitle_bodies={SUB_URL.replace("//", "https://"): fx.subtitle_body(ROWS)},
        )
        transcriber = fx.FakeTranscriber()
        plan = self.build(api, asr_enabled=True, _transcriber=transcriber,
                          _ffmpeg="ffmpeg", _runner=fx.fake_ffmpeg_runner())
        self.assertEqual(plan.sources, ["subtitle", "asr"])
        # P1 有字幕 → 不该跑 ASR；只有 P2 需要兜底
        self.assertEqual(transcriber.calls, [2])

    def test_asr_runs_but_finds_nothing(self):
        plan = self.build(fx.FakeApi(), asr_enabled=True,
                          _transcriber=fx.FakeTranscriber(empty=True),
                          _ffmpeg="ffmpeg", _runner=fx.fake_ffmpeg_runner())
        self.assertFalse(plan.has_text)
        self.assertEqual(plan.reason, REASON_NO_TRANSCRIPT)

    def test_prefer_subtitle_disabled(self):
        plan = self.build(fx.FakeApi(), prefer_subtitle=False, asr_enabled=False)
        self.assertFalse(plan.has_text)
        self.assertEqual(plan.reason, "subtitle_disabled")

    def test_to_json_has_time_base_note(self):
        plan = self.build(fx.FakeApi(), asr_enabled=False)
        payload = plan.to_json()
        self.assertEqual(payload["time_base"], "per_page")
        self.assertIn("未拼接", payload["note"])
        json.dumps(payload, ensure_ascii=False)      # 必须可序列化

    def test_signed_subtitle_url_is_never_persisted(self):
        """带 auth_key 的临时签名地址不得落进 metadata（计划 3.2）。"""
        url = "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/x.json?auth_key=0000000000-test-placeholder"
        api = fx.FakeApi(
            subtitles={"1001": [fx.subtitle_entry(url=url)], "1002": [fx.subtitle_entry(url=url)]},
            subtitle_bodies={url.replace("//", "https://"): fx.subtitle_body(ROWS)},
        )
        plan = self.build(api)
        self.assertTrue(plan.has_text)
        self.assertTrue(plan.pages[0].subtitle_url, "内存里仍保留（日志/诊断用）")
        blob = json.dumps(plan.to_json(), ensure_ascii=False)
        self.assertNotIn("auth_key", blob)
        self.assertNotIn("aisubtitle.hdslb.com", blob)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

