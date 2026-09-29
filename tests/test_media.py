"""阶段 2 媒体下载测试：格式选择、错误分类、产物校验、并发/续传/补做。

全部离线：下载器用 :class:`tests.fixtures.FakeDownloader`，ffprobe 用注入的
runner 替身，**不联网、不调用 ffmpeg**。
"""

from __future__ import annotations

import unittest

from bili_sub_archive.bili.parse import PageInfo
from bili_sub_archive.errors import KIND_DENIED, KIND_NETWORK, KIND_PARSE, KIND_RISK
from bili_sub_archive.media import (
    MIN_MEDIA_BYTES,
    MediaFile,
    build_format_selector,
    classify_download_error,
    download_media,
    media_notes,
    probe_media,
    quality_height,
    sniff_container,
)
from tests import drop_workspace, fixtures as fx, make_workspace


def pages(*specs: tuple[int, int, str]) -> list[PageInfo]:
    """``(page, cid, part)`` → PageInfo 列表。"""
    return [PageInfo(cid=cid, page=page, part=part, duration=600) for page, cid, part in specs]


class QualityTest(unittest.TestCase):
    def test_quality_height_aliases(self):
        self.assertEqual(quality_height("1080p"), 1080)
        self.assertEqual(quality_height("720P"), 720)
        self.assertEqual(quality_height("1080"), 1080)
        self.assertEqual(quality_height("480p "), 480)
        for raw in ("", "best", "max", "auto", None):
            self.assertEqual(quality_height(raw), 0, f"{raw!r} 应表示不限制")

    def test_format_selector_needs_ffmpeg_for_dash(self):
        with_ffmpeg = build_format_selector("1080p", has_ffmpeg=True)
        without = build_format_selector("1080p", has_ffmpeg=False)
        self.assertIn("bestvideo", with_ffmpeg)
        self.assertIn("height<=?1080", with_ffmpeg)
        # 没有 ffmpeg 时不能选分离流（选了也合流不了）
        self.assertNotIn("bestvideo", without)
        self.assertIn("ext=mp4", without)

    def test_format_selector_best_has_no_cap(self):
        self.assertNotIn("height<=", build_format_selector("best", has_ffmpeg=True))


class ErrorClassifyTest(unittest.TestCase):
    def test_known_patterns(self):
        self.assertEqual(classify_download_error("HTTP Error 412: Precondition Failed"), KIND_RISK)
        self.assertEqual(classify_download_error("ERROR: unable to download video data: HTTP Error 403"), KIND_DENIED)
        self.assertEqual(classify_download_error("HTTP Error 404: Not Found"), "not_found")
        self.assertEqual(classify_download_error("The read operation timed out"), KIND_NETWORK)
        self.assertEqual(classify_download_error("Unable to extract initial state"), KIND_PARSE)
        self.assertEqual(classify_download_error("something odd"), "http_error")


class SniffTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("media")

    def tearDown(self):
        drop_workspace(self.root)

    def write(self, name: str, blob: bytes):
        path = self.root / name
        path.write_bytes(blob)
        return path

    def test_sniff_containers(self):
        self.assertEqual(sniff_container(self.write("a.mp4", fx.MP4_MIN)), "mp4")
        self.assertEqual(sniff_container(self.write("b.flv", b"FLV\x01" + b"\x00" * 16)), "flv")
        self.assertEqual(sniff_container(self.write("c.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 16)), "matroska")
        self.assertEqual(sniff_container(self.write("d.html", fx.NOT_MEDIA)), "")
        self.assertEqual(sniff_container(self.root / "missing.mp4"), "")

    def test_probe_rejects_missing_empty_and_tiny(self):
        self.assertFalse(probe_media(self.root / "nope.mp4").ok)
        self.assertIn("不存在", probe_media(self.root / "nope.mp4").message)
        empty = self.write("empty.mp4", b"")
        self.assertFalse(probe_media(empty).ok)
        self.assertIn("空文件", probe_media(empty).message)
        tiny = self.write("tiny.mp4", b"\x00\x00\x00\x20ftypisom")
        probe = probe_media(tiny)
        self.assertFalse(probe.ok)
        self.assertIn("最小阈值", probe.message)

    def test_probe_sniff_success_states_its_limit(self):
        """没有 ffprobe 时只声明"结构上像媒体"，绝不声称"已验证可播放"。"""
        probe = probe_media(self.write("ok.mp4", fx.MP4_MIN))
        self.assertTrue(probe.ok)
        self.assertEqual(probe.mode, "sniff")
        self.assertEqual(probe.container, "mp4")
        self.assertIn("未使用 ffprobe", probe.note)
        self.assertIn("未经解码验证", probe.note)

    def test_probe_rejects_non_media_blob(self):
        probe = probe_media(self.write("fake.mp4", fx.NOT_MEDIA))
        self.assertFalse(probe.ok)
        self.assertIn("文件头", probe.message)

    def test_probe_with_ffprobe_reads_streams(self):
        import json

        payload = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"format_name": "mov,mp4,m4a", "duration": "123.45"},
        }

        def runner(argv):
            self.assertIn("-show_streams", argv)
            return 0, json.dumps(payload), ""

        probe = probe_media(self.write("ok.mp4", fx.MP4_MIN), ffprobe="ffprobe", runner=runner)
        self.assertTrue(probe.ok)
        self.assertEqual(probe.mode, "ffprobe")
        self.assertAlmostEqual(probe.duration, 123.45)
        self.assertEqual((probe.width, probe.height), (1920, 1080))
        self.assertTrue(probe.has_audio)
        self.assertIn("ffprobe 校验通过", probe.note)

    def test_probe_ffprobe_no_streams_is_failure(self):
        def runner(argv):
            return 0, '{"streams": [], "format": {}}', ""

        probe = probe_media(self.write("ok.mp4", fx.MP4_MIN), ffprobe="ffprobe", runner=runner)
        self.assertFalse(probe.ok)
        self.assertIn("未找到任何音视频流", probe.message)

    def test_probe_ffprobe_failure_falls_back_and_says_so(self):
        def runner(argv):
            return 1, "", "ffprobe: not found"

        probe = probe_media(self.write("ok.mp4", fx.MP4_MIN), ffprobe="ffprobe", runner=runner)
        self.assertTrue(probe.ok)
        self.assertEqual(probe.mode, "sniff")
        self.assertIn("ffprobe 调用失败", probe.note)

    def test_probe_ffprobe_raising_falls_back(self):
        def runner(argv):
            raise OSError("named pipe not allowed")

        probe = probe_media(self.write("ok.mp4", fx.MP4_MIN), ffprobe="ffprobe", runner=runner)
        self.assertTrue(probe.ok)
        self.assertEqual(probe.mode, "sniff")


class MediaFileJsonTest(unittest.TestCase):
    def test_round_trip(self):
        record = MediaFile(page=2, cid=42, part="P2", name="videos/P02.mp4", status="done",
                           size=123, duration=1.5, width=1920, height=1080, container="mp4",
                           format_id="bv+ba", vcodec="h264", acodec="aac", has_audio=True,
                           verified=True, verify_note="ok", resumed=True, attempts=1,
                           message="下载完成", url="https://example.invalid/v")
        clone = MediaFile.from_json(record.to_json())
        self.assertEqual(clone.to_json(), record.to_json())

    def test_from_json_tolerates_empty(self):
        record = MediaFile.from_json({})
        self.assertEqual(record.page, 0)
        self.assertEqual(record.status, "pending")


class DownloadMediaTest(unittest.TestCase):
    def setUp(self):
        self.root = make_workspace("media_dl")
        self.entry = self.root / "entry"
        self.entry.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        drop_workspace(self.root)

    def run_download(self, downloader, page_list, **kwargs):
        return download_media(
            bvid="BV1test", pages=page_list, entry_dir=self.entry,
            referer="https://www.bilibili.com/video/BV1test",
            downloader=downloader, **kwargs)

    def test_all_pages_download_and_verify(self):
        downloader = fx.FakeDownloader()
        plan = self.run_download(downloader, pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(plan.done, 2)
        self.assertEqual(plan.failed, 0)
        self.assertEqual([r.page for r in plan.records], [1, 2])
        self.assertTrue(all(r.verified for r in plan.records))
        self.assertTrue((self.entry / "videos" / "P01.mp4").is_file())
        self.assertTrue((self.entry / "videos" / "P02.mp4").is_file())
        self.assertGreater(plan.bytes_written, 0)

    def test_partial_failure_records_reason(self):
        downloader = fx.FakeDownloader(fail_pages=(2,))
        plan = self.run_download(downloader, pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(plan.done, 1)
        self.assertEqual(plan.failed, 1)
        failed = [r for r in plan.records if r.status == "failed"][0]
        self.assertEqual(failed.page, 2)
        self.assertEqual(failed.error_kind, KIND_NETWORK)
        notes = media_notes(plan)
        self.assertTrue(any("P02 下载失败" in n for n in notes))

    def test_all_failed(self):
        downloader = fx.FakeDownloader(fail_pages=(1, 2))
        plan = self.run_download(downloader, pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(plan.done, 0)
        self.assertEqual(plan.failed, 2)

    def test_invalid_product_is_rejected_by_verification(self):
        """下载"成功"但产物是错误页 → 校验拦下，步骤不能标 done。"""
        downloader = fx.FakeDownloader(broken_pages=(1,))
        plan = self.run_download(downloader, pages((1, 1001, "P1")))
        self.assertEqual(plan.done, 0)
        self.assertEqual(plan.failed, 1)
        self.assertFalse(plan.records[0].verified)
        self.assertIn("产物校验失败", plan.records[0].message)

    def test_rerun_skips_valid_products(self):
        """幂等重跑：已有有效产物不重复下载（需求 3.3.1 可恢复重试）。"""
        first = fx.FakeDownloader()
        plan1 = self.run_download(first, pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(len(first.calls), 2)

        second = fx.FakeDownloader()
        plan2 = self.run_download(second, pages((1, 1001, "P1"), (2, 1002, "P2")),
                                  previous=plan1.to_json())
        self.assertEqual(second.calls, [], "有效产物不该重复下载")
        self.assertEqual(plan2.done, 2)
        self.assertTrue(all("跳过下载" in r.message for r in plan2.records))

    def test_rerun_only_retries_failed_pages(self):
        """部分失败后重跑：只补失败的分 P。"""
        first = fx.FakeDownloader(fail_pages=(2,))
        plan1 = self.run_download(first, pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(first.calls, [1, 2])

        second = fx.FakeDownloader()
        plan2 = self.run_download(second, pages((1, 1001, "P1"), (2, 1002, "P2")),
                                  previous=plan1.to_json())
        self.assertEqual(second.calls, [2], "只应补做失败的分 P")
        self.assertEqual(plan2.done, 2)

    def test_missing_product_on_disk_is_redownloaded(self):
        first = fx.FakeDownloader()
        plan1 = self.run_download(first, pages((1, 1001, "P1")))
        (self.entry / "videos" / "P01.mp4").unlink()

        second = fx.FakeDownloader()
        plan2 = self.run_download(second, pages((1, 1001, "P1")), previous=plan1.to_json())
        self.assertEqual(second.calls, [1], "产物被删掉后应重新下载")
        self.assertEqual(plan2.done, 1)

    def test_resume_marker_detected(self):
        """上次中断留下的 .part 残片 → 本次下载标记为续传（需求 3.3.1 断点续传）。"""
        videos = self.entry / "videos"
        videos.mkdir(parents=True, exist_ok=True)
        (videos / "P01.mp4.part").write_bytes(b"half")
        downloader = fx.FakeDownloader()
        plan = self.run_download(downloader, pages((1, 1001, "P1")))
        self.assertTrue(plan.records[0].resumed, "存在 .part 残片时应标记为续传")
        self.assertIn("续传", plan.records[0].message)

    def test_no_part_means_not_resumed(self):
        plan = self.run_download(fx.FakeDownloader(), pages((1, 1001, "P1")))
        self.assertFalse(plan.records[0].resumed)

    def test_concurrency_respects_video_workers(self):
        downloader = fx.FakeDownloader(delay=0.05)
        plan = self.run_download(
            downloader,
            pages((1, 1001, "P1"), (2, 1002, "P2"), (3, 1003, "P3"), (4, 1004, "P4")),
            video_workers=2)
        self.assertEqual(plan.done, 4)
        self.assertEqual(downloader.max_concurrent, 2, "并发数应受 video_workers 限制")

    def test_serial_when_workers_is_one(self):
        downloader = fx.FakeDownloader(delay=0.01)
        self.run_download(downloader, pages((1, 1001, "P1"), (2, 1002, "P2")),
                          video_workers=1)
        self.assertEqual(downloader.max_concurrent, 1)

    def test_missing_dependency_marks_every_page_failed(self):
        downloader = fx.FakeDownloader(available=False)
        plan = self.run_download(downloader, pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(plan.failed, 2)
        self.assertTrue(all(r.error_kind == "dependency_missing" for r in plan.records))
        self.assertTrue(any("不可用" in n for n in plan.notes))
        self.assertEqual(downloader.calls, [], "依赖不可用时不该真的去下载")

    def test_no_ffmpeg_note_is_recorded(self):
        plan = self.run_download(fx.FakeDownloader(), pages((1, 1001, "P1")), ffmpeg="")
        self.assertTrue(any("未找到 ffmpeg" in n for n in plan.notes))
        self.assertEqual(plan.verify_mode, "sniff")
        self.assertNotIn("bestvideo", plan.format_selector)

    def test_downloader_exception_does_not_break_batch(self):
        class Exploding:
            name = "exploding"

            def probe(self):
                return True, "ok"

            def download(self, request):
                raise RuntimeError("替身炸了")

        plan = self.run_download(Exploding(), pages((1, 1001, "P1"), (2, 1002, "P2")))
        self.assertEqual(plan.failed, 2)
        self.assertTrue(all("替身炸了" in r.message for r in plan.records))

    def test_verify_min_bytes_constant_is_sane(self):
        self.assertGreaterEqual(MIN_MEDIA_BYTES, 1024)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
