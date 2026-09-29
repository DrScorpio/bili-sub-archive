"""离线集成测试：发现 → 筛选 → 归档 → 索引/状态 → 重跑不重复 → retry 补做。

全程使用 :class:`tests.fixtures.FakeApi`（合成响应，结构照抄阶段 0 证据），
不发任何网络请求。覆盖计划第 6.2 节"离线集成测试"的核心断言。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime
from pathlib import Path

from bili_sub_archive.config import Config, load_config
from bili_sub_archive.errors import CredentialError
from bili_sub_archive.models import STEP_DONE, STEP_FAILED, STEP_SKIPPED
from bili_sub_archive.redact import Redactor, scan_for_secrets
from bili_sub_archive.runner import Runner, RunOptions
from bili_sub_archive.store import METADATA_NAME, Store
from bili_sub_archive.timeutil import BEIJING
from tests import drop_workspace, fixtures as fx, make_workspace, strip_config_env


def ts(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=BEIJING).timestamp())


D_NEW = "1250858063341027334"
D_BLOCK = "1250858063341027999"
D_SECOND = "1250858000000000001"
D_CARD = "1248647864556453927"
D_OLD = "1226765968301096995"

BV_NORMAL = "BV1zqMC6LEmp"
BV_CHARGE = "BV1zveP6eEZ1"

OP_ART1 = "1214741992578220039"
OP_ART2 = "1214741992578220038"

CV1 = "cv1001"
CV2 = "cv1002"


class NullLogger:
    """记录日志而不输出（断言用）。"""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def _add(self, level, message):
        self.messages.append((level, str(message)))

    def debug(self, message):
        self._add("debug", message)

    def info(self, message):
        self._add("info", message)

    def warning(self, message):
        self._add("warning", message)

    def error(self, message):
        self._add("error", message)

    def text(self) -> str:
        return "\n".join(m for _l, m in self.messages)


class Scenario:
    """一套完整的合成数据 + FakeApi。"""

    def __init__(self, *, broken_image: bool = False) -> None:
        pic = "https://i0.hdslb.com/bfs/new_dyn/broken.png" if broken_image \
            else "https://i0.hdslb.com/bfs/new_dyn/good.png"
        self.pic = pic
        self.dynamics = [
            fx.dynamic_feed_item(D_NEW, ts(2026, 9, 22, 19, 28), "最新动态正文",
                                 is_only_fans=True),
            fx.dynamic_feed_item(D_BLOCK, ts(2026, 9, 20, 10, 0), "", is_only_fans=True),
            fx.dynamic_feed_item(D_CARD, ts(2026, 9, 10, 12, 0), "",
                                 dyn_type="DYNAMIC_TYPE_AV", major_type="MAJOR_TYPE_ARCHIVE",
                                 major={"type": "MAJOR_TYPE_ARCHIVE",
                                        "archive": {"bvid": BV_NORMAL, "title": "动态里的视频卡",
                                                    "desc": "卡片简介",
                                                    "cover": "https://i0.hdslb.com/cover.jpg",
                                                    "duration_text": "33:00"}}),
            fx.dynamic_feed_item(D_OLD, ts(2026, 8, 1, 9, 0), "我的转发评论",
                                 dyn_type="DYNAMIC_TYPE_FORWARD", major_type="", major={},
                                 orig=fx.dynamic_feed_item(
                                     "999", ts(2026, 7, 1, 8, 0), "转发原文内容",
                                     major={"type": "MAJOR_TYPE_OPUS",
                                            "opus": {"title": "", "summary": fx.rich_text("转发原文内容"),
                                                     "pics": [{"url": "https://i0.hdslb.com/bfs/new_dyn/orig.png",
                                                               "width": 100, "height": 50}]}})),
        ]
        self.dynamic_details = {
            D_NEW: self.dynamics[0],
            D_BLOCK: self.dynamics[1],
            D_CARD: self.dynamics[2],
            D_OLD: self.dynamics[3],
            D_SECOND: fx.dynamic_feed_item(D_SECOND, ts(2026, 9, 18, 8, 0), "次级来源动态"),
        }
        self.opus_details = {
            D_NEW: fx.opus_detail_item(
                D_NEW, ts(2026, 9, 22, 19, 28), "带图动态",
                [fx.para_text("第一段正文"), fx.para_pic(pic), fx.para_text("第二段正文")],
                is_only_fans=True),
            D_BLOCK: fx.opus_detail_item(D_BLOCK, ts(2026, 9, 20, 10, 0), "被门控的动态",
                                         blocked=True, is_only_fans=True),
            D_SECOND: fx.opus_detail_item(D_SECOND, ts(2026, 9, 18, 8, 0), "次级动态",
                                          [fx.para_text("次级来源正文")]),
            OP_ART1: fx.opus_detail_item(
                OP_ART1, ts(2026, 9, 12, 11, 39), "地缘 + 金银 + 石油 + A股",
                [fx.para_text("专栏第一段"), fx.para_heading("小标题一"), fx.para_pic(pic),
                 fx.para_text("专栏第二段")],
                item_type=1, article_type=4, is_only_fans=True),
            OP_ART2: fx.opus_detail_item(
                OP_ART2, ts(2026, 6, 17, 11, 39), "只有文字的专栏",
                [fx.para_text("纯文字专栏正文")], item_type=1, article_type=3),
        }
        # 次级来源：opus 图文列表（type=all）只含列表里没有的那一条
        self.opus_all = [fx.opus_feed_item(D_SECOND, "次级来源动态摘要")]
        self.videos = [
            fx.video_vlist_entry(BV_NORMAL, ts(2026, 9, 16, 20, 31), "公开视频"),
            fx.video_vlist_entry(BV_CHARGE, ts(2026, 9, 15, 20, 31), "充电专属视频",
                                 is_charging=True),
        ]
        self.video_details = {
            BV_NORMAL: fx.video_detail(BV_NORMAL, ts(2026, 9, 16, 20, 31), "公开视频",
                                      pages=[{"cid": 1001, "page": 1, "part": "P1", "duration": 600,
                                              "dimension": {"width": 1920, "height": 1080}},
                                             {"cid": 1002, "page": 2, "part": "P2", "duration": 300,
                                              "dimension": {"width": 1280, "height": 720}}]),
            BV_CHARGE: fx.video_detail(BV_CHARGE, ts(2026, 9, 15, 20, 31), "充电专属视频",
                                      is_upower_exclusive=True),
        }
        self.playurls = {BV_NORMAL: fx.playurl_full(), BV_CHARGE: fx.playurl_preview_only()}
        self.opus_articles = [
            fx.opus_feed_item(OP_ART1, "专栏摘要一", badge=True),
            fx.opus_feed_item(OP_ART2, "专栏摘要二"),
        ]
        self.legacy_articles = [
            fx.legacy_article_entry(1002, ts(2026, 5, 2, 10, 0), "旧格式专栏（HTML）"),
            fx.legacy_article_entry(1001, ts(2026, 5, 1, 10, 0), "旧格式专栏（段落）"),
        ]
        self.legacy_views = {
            CV2: fx.legacy_article_view(
                1002, ts(2026, 5, 2, 10, 0), "旧格式专栏（HTML）",
                html="<h2>小节标题</h2><p>HTML 正文第一段</p><p>第二段 <strong>加粗</strong></p>"
                     "<img src=\"https://i0.hdslb.com/bfs/new_dyn/banner/inline.png\">"
                     "<ul><li>列表项一</li><li>列表项二</li></ul>"),
            CV1: fx.legacy_article_view(
                1001, ts(2026, 5, 1, 10, 0), "旧格式专栏（段落）",
                paragraphs=[fx.para_text("段落正文"), fx.para_pic(self.pic)],
                html="<p>不该被使用的 HTML</p>", opus_id="1251193122342305812"),
        }

    def api(self, **kwargs) -> fx.FakeApi:
        return fx.FakeApi(
            dynamics=self.dynamics, dynamic_details=self.dynamic_details,
            opus_details=self.opus_details, opus_all=self.opus_all,
            opus_articles=self.opus_articles, videos=self.videos,
            video_details=self.video_details, playurls=self.playurls,
            legacy_articles=self.legacy_articles, legacy_views=self.legacy_views,
            **kwargs)


class FlowTestBase(unittest.TestCase):
    cookie = "SESSDATA=abcdef123456; bili_jct=zzzz9999"

    def setUp(self):
        self._saved_env = strip_config_env()
        os.environ["BSA_COOKIE"] = self.cookie
        self.root = make_workspace("flow")
        self.out = self.root / "output"
        self.logger = NullLogger()
        # 假 ffmpeg 可执行文件：只用于让配置解析通过，真正的执行被 runner 替身接管
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.fake_ffmpeg = self.bin_dir / "ffmpeg.exe"
        self.fake_ffmpeg.write_bytes(b"fake")
        self.fake_ffprobe = self.bin_dir / "ffprobe.exe"
        self.fake_ffprobe.write_bytes(b"fake")
        # 假 mmdc：只用于让"mermaid-cli 可用"这条路径走通，真正执行被 runner 替身接管
        self.fake_mmdc = self.bin_dir / "mmdc.cmd"
        self.fake_mmdc.write_bytes(b"fake")

    def tearDown(self):
        drop_workspace(self.root)
        os.environ.update(self._saved_env)

    # -- 工具 -- #
    def make_runner(self, api, *, downloader=None, transcriber=None,
                    ffmpeg_runner=None, chat_client=None, mmdc_runner=None, **overrides) -> Runner:
        overrides.setdefault("uid", 1000)
        overrides.setdefault("output_dir", self.out)
        if ffmpeg_runner is not None:
            overrides.setdefault("ffmpeg_path", str(self.fake_ffmpeg))
            overrides.setdefault("ffprobe_path", str(self.fake_ffprobe))
        loaded = load_config(root=self.root, overrides=overrides)
        # 默认注入替身下载器：离线测试不联网、也不依赖本机是否装了 yt-dlp
        if downloader is None:
            downloader = fx.FakeDownloader()
        return Runner(loaded, logger=self.logger, root=self.root, api=api,
                      downloader=downloader, transcriber=transcriber,
                      ffmpeg_runner=ffmpeg_runner, chat_client=chat_client,
                      mmdc_runner=mmdc_runner)

    def author_dir(self) -> Path:
        dirs = [p for p in self.out.iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1, f"应只有一个作者目录，实际 {dirs}")
        return dirs[0]

    def index(self) -> dict:
        return json.loads((self.author_dir() / "index.json").read_text(encoding="utf-8"))

    def entry_dir(self, kind: str, pid: str) -> Path:
        data = self.index()
        for entry in data["entries"]:
            if entry["kind"] == kind and entry["platform_id"] == pid:
                return self.author_dir() / entry["dir"]
        raise AssertionError(f"index.json 中找不到 {kind}/{pid}")

    def metadata(self, kind: str, pid: str) -> dict:
        return json.loads((self.entry_dir(kind, pid) / METADATA_NAME).read_text(encoding="utf-8"))

    def read(self, kind: str, pid: str, name: str) -> str:
        return (self.entry_dir(kind, pid) / name).read_text(encoding="utf-8")


class FullFlowTest(FlowTestBase):
    def test_full_sync_archives_all_three_kinds(self):
        scenario = Scenario()
        api = scenario.api()
        summary = self.make_runner(api).sync(RunOptions())

        self.assertEqual(summary.discovered, 11)
        self.assertEqual(summary.selected, 11)
        self.assertEqual(len(self.index()["entries"]), 11)

        # --- 动态：正文 + 原图 + 图片相对路径 ---
        dynamic_dir = self.entry_dir("dynamic", D_NEW)
        content = (dynamic_dir / "content.md").read_text(encoding="utf-8")
        self.assertIn("第一段正文", content)
        self.assertIn("第二段正文", content)
        self.assertIn("images/01.png", content)
        self.assertIn("充电专属", content)
        self.assertTrue((dynamic_dir / "images" / "01.png").is_file())
        # 头像按 Content-Type 落盘（替身返回 image/png），扩展名以实际类型为准
        avatars = list((dynamic_dir / "images").glob("avatar.*"))
        self.assertEqual(len(avatars), 1, "头像应下载且只保留一份")
        self.assertIn("images/avatar.", content)
        meta = self.metadata("dynamic", D_NEW)
        self.assertEqual(meta["steps"]["fetch"]["status"], STEP_DONE)
        self.assertEqual(meta["steps"]["content"]["status"], STEP_DONE)
        self.assertEqual(meta["steps"]["images"]["status"], STEP_DONE)
        # 阶段 2：动态单张长图已实现（需求 3.2.2），不再是 skipped
        self.assertEqual(meta["steps"]["render"]["status"], STEP_DONE)
        self.assertTrue((dynamic_dir / "render.png").is_file(), "应生成单张长 PNG")
        self.assertEqual(meta["extra"]["render"]["images_rendered"], 1)
        self.assertGreater(meta["extra"]["render"]["height"], 200)
        self.assertEqual(meta["extra"]["permission"], "ok")
        self.assertFalse(meta["extra"]["blocked"])
        self.assertEqual(meta["source"]["dynamic_type"], "DYNAMIC_TYPE_DRAW")

        # --- 充电动态正文被门控 → denied，不写伪造正文，且不被自动 retry ---
        blocked = self.metadata("dynamic", D_BLOCK)
        self.assertEqual(blocked["steps"]["fetch"]["status"], STEP_DONE)
        self.assertEqual(blocked["steps"]["content"]["status"], STEP_SKIPPED)
        self.assertEqual(blocked["steps"]["content"]["reason"], "content_blocked")
        self.assertEqual(blocked["steps"]["content"]["error_kind"], "content_blocked")
        self.assertTrue(blocked["extra"]["blocked"])
        self.assertFalse((self.entry_dir("dynamic", D_BLOCK) / "content.md").exists())
        self.assertTrue(any(e["step"] == "content" and e["kind"] == "content_blocked"
                            for e in blocked["errors"]))

        # --- 视频卡片动态：保留卡片信息 + 封面图被引用 ---
        card = self.read("dynamic", D_CARD, "content.md")
        self.assertIn("动态里的视频卡", card)
        self.assertIn(BV_NORMAL, card)
        self.assertIn("视频卡片", card)
        self.assertIn("## 配图", card)
        self.assertIn("images/01.png", card)

        # --- 转发动态：保留转发原文，原文配图也被下载并引用 ---
        forward = self.read("dynamic", D_OLD, "content.md")
        self.assertIn("转发原文", forward)
        self.assertIn("转发原文内容", forward)
        self.assertIn("images/01.png", forward)
        forward_meta = self.metadata("dynamic", D_OLD)
        self.assertEqual(forward_meta["extra"]["forward_images"], 1)
        self.assertEqual(forward_meta["extra"]["images"][0]["status"], "done")

        # --- 次级来源动态（opus 图文列表）也被归档 ---
        secondary = self.read("dynamic", D_SECOND, "content.md")
        self.assertIn("次级来源正文", secondary)

        # --- 视频：元数据 + 分 P + 权限探针 + 媒体下载 + 文字稿 ---
        video = self.metadata("video", BV_NORMAL)
        self.assertEqual(video["extra"]["page_source"], "pages(view) + pagelist(对照)")
        self.assertEqual(len(video["extra"]["pages"]), 2)
        self.assertEqual(video["extra"]["access"]["permission"], "full")
        self.assertEqual(video["extra"]["access"]["dash_video_streams"], 4)
        self.assertEqual(video["steps"]["fetch"]["status"], STEP_DONE)
        # 阶段 2：两个分 P 都下载并校验（多 P 同目录、不拼接 —— 需求 3.3.1）
        self.assertEqual(video["steps"]["media"]["status"], STEP_DONE)
        video_dir = self.entry_dir("video", BV_NORMAL)
        self.assertTrue((video_dir / "videos" / "P01.mp4").is_file())
        self.assertTrue((video_dir / "videos" / "P02.mp4").is_file())
        self.assertEqual([r["status"] for r in video["extra"]["media"]], ["done", "done"])
        self.assertTrue(all(r["verified"] for r in video["extra"]["media"]))
        # 未配置字幕、未开 ASR → transcript 记 skipped(asr_disabled)，且不编造文字稿
        self.assertEqual(video["steps"]["transcript"]["status"], STEP_SKIPPED)
        self.assertEqual(video["steps"]["transcript"]["reason"], "asr_disabled")
        self.assertFalse((video_dir / "transcript.txt").exists())
        for step in ("summary", "mindmap"):
            self.assertEqual(video["steps"][step]["status"], STEP_SKIPPED)
            self.assertEqual(video["steps"][step]["reason"], "no_transcript")
            self.assertIn("缺少文字来源", video["steps"][step]["message"])

        # --- 充电视频无权限：denied + permission=preview_only ---
        charging = self.metadata("video", BV_CHARGE)
        self.assertEqual(charging["extra"]["access"]["permission"], "preview_only")
        self.assertTrue(charging["extra"]["is_upower_exclusive"])
        self.assertEqual(charging["steps"]["media"]["reason"], "permission_preview_only")

        # --- opus 专栏：标题 + 小标题 + 图片 ---
        article = self.read("article", OP_ART1, "article.md")
        self.assertIn("地缘 + 金银 + 石油 + A股", article)
        self.assertIn("### 小标题一", article)
        self.assertIn("专栏第一段", article)
        self.assertIn("images/01.png", article)
        self.assertTrue((self.entry_dir("article", OP_ART1) / "images" / "01.png").is_file())
        article_meta = self.metadata("article", OP_ART1)
        self.assertEqual(article_meta["extra"]["format"], "opus")
        self.assertEqual(article_meta["extra"]["article_type"], 4)
        self.assertTrue(article_meta["extra"]["is_only_fans"])

        # --- 旧格式专栏：opus 段落优先，HTML 兜底 ---
        legacy_paras = self.read("article", CV1, "article.md")
        self.assertIn("段落正文", legacy_paras)
        self.assertNotIn("不该被使用的 HTML", legacy_paras)
        self.assertEqual(self.metadata("article", CV1)["extra"]["format"], "legacy_cv")
        legacy_html = self.read("article", CV2, "article.md")
        self.assertIn("## 小节标题", legacy_html)
        self.assertIn("**加粗**", legacy_html)
        self.assertIn("- 列表项一", legacy_html)
        self.assertIn("images/01.png", legacy_html)
        self.assertEqual(self.metadata("article", CV2)["extra"]["html_fallback"], True)

        # --- 运行摘要：结果计数与三类统计 ---
        self.assertEqual(summary.counts.get("denied"), 2)  # 门控动态 + 无权限充电视频
        self.assertEqual(summary.counts.get("done"), 9)
        self.assertEqual(len(summary.scans), 3)
        self.assertTrue(all(scan.requests > 0 for scan in summary.scans))
        self.assertTrue((self.author_dir() / "_runs").is_dir())

        # --- 产物里不得出现凭据 ---
        hits = scan_for_secrets(self.author_dir(), Redactor.from_cookie(self.cookie))
        self.assertEqual(hits, [], f"产物中发现凭据：{hits}")

        # --- 图片不变量：下载成功的图必须被正文引用；引用必须能在盘上找到 ---
        self.assert_image_invariants()

    def assert_image_invariants(self) -> None:
        import re

        pattern = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
        for entry in self.index()["entries"]:
            entry_dir = self.author_dir() / entry["dir"]
            meta = json.loads((entry_dir / METADATA_NAME).read_text(encoding="utf-8"))
            markdown_name = "content.md" if entry["kind"] == "dynamic" else "article.md"
            md_path = entry_dir / markdown_name
            if not md_path.is_file():
                continue
            markdown = md_path.read_text(encoding="utf-8")
            links = pattern.findall(markdown)
            for record in meta.get("extra", {}).get("images", []) or []:
                if record.get("status") != "done":
                    continue
                self.assertIn(record["name"], links,
                              f"{record['name']} 已下载但未被 {markdown_name} 引用")
                self.assertTrue((entry_dir / record["name"]).is_file(),
                                f"{record['name']} 记录为 done 但文件不存在")
            for link in links:
                if link.startswith(("http://", "https://")):
                    continue  # 下载失败时保留的原链接
                self.assertTrue((entry_dir / link).is_file(),
                                f"{markdown_name} 引用了不存在的本地文件 {link}")

    def test_idempotent_rerun_skips_everything(self):
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api).sync(RunOptions())
        downloads_after_first = len(api.downloads)
        dirs_after_first = sorted(p.name for p in self.author_dir().iterdir())

        second = self.make_runner(api).sync(RunOptions())
        self.assertEqual(second.counts.get("skipped"), 11)
        self.assertEqual(second.counts.get("done", 0), 0)
        self.assertEqual(len(api.downloads), downloads_after_first)
        self.assertEqual(sorted(p.name for p in self.author_dir().iterdir()), dirs_after_first)

    def test_dry_run_writes_nothing(self):
        scenario = Scenario()
        api = scenario.api()
        summary = self.make_runner(api).sync(RunOptions(dry_run=True))
        self.assertEqual(summary.counts.get("skipped"), 11)
        entries = [p for p in self.author_dir().iterdir() if p.is_dir() and not p.name.startswith("_")]
        self.assertEqual(entries, [])
        self.assertEqual(len(self.index()["entries"]), 0)


class FilterTest(FlowTestBase):
    def test_date_range_selection(self):
        scenario = Scenario()
        summary = self.make_runner(scenario.api(), date_from=date(2026, 9, 16),
                                   date_to=date(2026, 9, 21)).sync(RunOptions())
        selected = {(r.kind, r.platform_id) for r in summary.results}
        self.assertEqual(selected, {("dynamic", D_BLOCK), ("dynamic", D_SECOND),
                                    ("video", BV_NORMAL)})
        self.assertEqual(summary.selected, 3)

    def test_latest_n_across_kinds(self):
        scenario = Scenario()
        summary = self.make_runner(scenario.api(), date_from=date(2026, 9, 1),
                                   latest=5).sync(RunOptions())
        ordered = [(r.kind, r.platform_id) for r in summary.results]
        self.assertEqual(ordered, [("dynamic", D_NEW), ("dynamic", D_BLOCK),
                                   ("dynamic", D_SECOND), ("video", BV_NORMAL),
                                   ("video", BV_CHARGE)])
        self.assertEqual(summary.latest, 5)

    def test_single_kind_only(self):
        scenario = Scenario()
        summary = self.make_runner(scenario.api(), kinds=["article"]).sync(RunOptions())
        self.assertEqual(summary.selected, 4)  # 2 新格式 + 2 旧格式
        kinds = {r.kind for r in summary.results}
        self.assertEqual(kinds, {"article"})


class RetryTest(FlowTestBase):
    def test_failed_detail_then_retry_succeeds(self):
        scenario = Scenario()
        api = scenario.api()
        api.failures[f"dynamic_detail:{D_OLD}"] = -352
        summary = self.make_runner(api).sync(RunOptions())
        failed = [r for r in summary.results if r.outcome == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].platform_id, D_OLD)
        meta = self.metadata("dynamic", D_OLD)
        self.assertEqual(meta["steps"]["fetch"]["status"], STEP_FAILED)
        self.assertEqual(meta["steps"]["fetch"]["error_code"], -352)
        self.assertEqual(meta["steps"]["content"]["status"], "pending")

        retry = self.make_runner(api).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(retry.counts.get("done"), 1)
        meta = self.metadata("dynamic", D_OLD)
        self.assertEqual(meta["steps"]["fetch"]["status"], STEP_DONE)
        self.assertIn("转发原文内容", self.read("dynamic", D_OLD, "content.md"))

    def test_retry_steps_filter(self):
        scenario = Scenario()
        api = scenario.api()
        api.failures[f"dynamic_detail:{D_OLD}"] = -352
        self.make_runner(api).sync(RunOptions())
        # D_OLD 的 fetch 失败，content 仍为 pending → 指定 render 不能直接跳过 fetch，
        # 因此这里仍会被选中（重跑整条流程 = 该步骤及其下游）
        retry = self.make_runner(api).retry(RunOptions(steps={"render"}))
        self.assertEqual(retry.selected, 1)
        self.assertEqual(retry.counts.get("done"), 1)
        # 阶段 2：render 已实现 → 重跑后为 done（且真的产出了长图）
        self.assertEqual(self.metadata("dynamic", D_OLD)["steps"]["render"]["status"], STEP_DONE)
        self.assertTrue((self.entry_dir("dynamic", D_OLD) / "render.png").is_file())

    def test_retry_skips_permission_denied_entries(self):
        """被门控（content_blocked）的条目不该被自动 retry 反复打接口。"""
        scenario = Scenario()
        self.make_runner(scenario.api()).sync(RunOptions())
        retry = self.make_runner(scenario.api()).retry(RunOptions())
        self.assertEqual(retry.selected, 0)
        self.assertTrue(any("没有需要补做" in note for note in retry.notes))

    def test_partial_image_failure_then_retry(self):
        scenario = Scenario(broken_image=True)
        api = scenario.api()
        summary = self.make_runner(api, kinds=["dynamic"]).sync(RunOptions())
        outcomes = {r.platform_id: r.outcome for r in summary.results}
        self.assertEqual(outcomes[D_NEW], "partial")
        meta = self.metadata("dynamic", D_NEW)
        self.assertEqual(meta["steps"]["images"]["status"], STEP_FAILED)
        image = meta["extra"]["images"][0]
        self.assertEqual(image["status"], "failed")
        content = self.read("dynamic", D_NEW, "content.md")
        self.assertIn("broken.png", content)      # 失败图保留原链接
        self.assertIn("未获取到", content)

        # 修好图片 URL 后 retry：只补这一条
        scenario.opus_details[D_NEW] = fx.opus_detail_item(
            D_NEW, ts(2026, 9, 22, 19, 28), "带图动态",
            [fx.para_text("第一段正文"),
             fx.para_pic("https://i0.hdslb.com/bfs/new_dyn/fixed.png"),
             fx.para_text("第二段正文")], is_only_fans=True)
        api2 = scenario.api()
        retry = self.make_runner(api2, kinds=["dynamic"]).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(retry.counts.get("done"), 1)
        meta = self.metadata("dynamic", D_NEW)
        self.assertEqual(meta["steps"]["images"]["status"], STEP_DONE)
        self.assertIn("images/01.png", self.read("dynamic", D_NEW, "content.md"))

    def test_retry_without_author_dir_raises(self):
        scenario = Scenario()
        with self.assertRaises(Exception):
            self.make_runner(scenario.api()).retry(RunOptions())


class Stage2FlowTest(FlowTestBase):
    """阶段 2 端到端：媒体下载 / 文字稿 / 长图 / 失败补做 / 阶段接管。"""

    def test_subtitle_flow_produces_transcript(self):
        scenario = Scenario()
        rows = [(0.0, 1.0, "字幕第一句"), (1.0, 2.0, "字幕第二句")]
        url = "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/zh.json"
        api = scenario.api(
            subtitles={"1001": [fx.subtitle_entry(url=url)],
                       "1002": [fx.subtitle_entry(url=url)]},
            subtitle_bodies={url.replace("//", "https://"): fx.subtitle_body(rows)},
        )
        summary = self.make_runner(api, kinds=["video"]).sync(RunOptions())
        self.assertEqual(summary.counts.get("done"), 1)
        entry_dir = self.entry_dir("video", BV_NORMAL)
        self.assertTrue((entry_dir / "transcript.txt").is_file())
        self.assertTrue((entry_dir / "transcript_timed.srt").is_file())
        self.assertTrue((entry_dir / "transcript" / "P01.srt").is_file())
        self.assertTrue((entry_dir / "transcript" / "P02.srt").is_file())
        srt = (entry_dir / "transcript_timed.srt").read_text(encoding="utf-8")
        self.assertIn("[P01]", srt)
        self.assertIn("[P02]", srt)
        self.assertIn("时间基准：本 P 起点", srt)
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["transcript"]["status"], STEP_DONE)
        self.assertEqual(meta["extra"]["transcript"]["sources"], ["subtitle"])
        self.assertEqual(meta["extra"]["transcript"]["time_base"], "per_page")
        # 有文字来源 → 阶段 3 接管：未配置 LLM，记 skipped(llm_not_configured)（配置后 retry 可补做）
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_SKIPPED)
        self.assertEqual(meta["steps"]["summary"]["reason"], "llm_not_configured")
        self.assertEqual(meta["steps"]["mindmap"]["reason"], "llm_not_configured")

    def test_asr_flow_uses_transcriber_only_when_no_subtitle(self):
        scenario = Scenario()
        transcriber = fx.FakeTranscriber(text="本地转写")
        runner = self.make_runner(scenario.api(), kinds=["video"], asr_enabled=True,
                                  transcriber=transcriber,
                                  ffmpeg_runner=fx.fake_ffmpeg_runner())
        summary = runner.sync(RunOptions())
        self.assertEqual(summary.counts.get("done"), 1)
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["transcript"]["status"], STEP_DONE)
        self.assertEqual(meta["extra"]["transcript"]["sources"], ["asr"])
        self.assertEqual(transcriber.calls, [1, 2], "两个分 P 都没有字幕 → 都应走本地转写")
        text = self.read("video", BV_NORMAL, "transcript.txt")
        self.assertIn("本地转写", text)
        self.assertIn("本地 ASR", text)
        # 中间音频默认清理掉（不留在产物里）
        self.assertFalse((self.entry_dir("video", BV_NORMAL) / "transcript" / "P01.audio.wav").exists())

    def test_no_media_flag_skips_media_step(self):
        scenario = Scenario()
        summary = self.make_runner(scenario.api(), kinds=["video"],
                                   download_media=False).sync(RunOptions())
        self.assertEqual(summary.counts.get("done"), 1)
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["media"]["status"], STEP_SKIPPED)
        self.assertEqual(meta["steps"]["media"]["reason"], "media_disabled")
        self.assertFalse((self.entry_dir("video", BV_NORMAL) / "videos").exists())

    def test_no_render_flag_skips_render_step(self):
        scenario = Scenario()
        summary = self.make_runner(scenario.api(), kinds=["dynamic"],
                                   render_dynamic_png=False).sync(RunOptions())
        self.assertGreater(summary.counts.get("done"), 0)
        meta = self.metadata("dynamic", D_NEW)
        self.assertEqual(meta["steps"]["render"]["status"], STEP_SKIPPED)
        self.assertEqual(meta["steps"]["render"]["reason"], "render_disabled")
        self.assertFalse((self.entry_dir("dynamic", D_NEW) / "render.png").exists())

    def test_render_disabled_then_enabled_is_redone(self):
        """开关关闭时跳过的步骤，开关打开后 retry 必须能接管（models.stale_skip_map）。"""
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api, kinds=["dynamic"], render_dynamic_png=False).sync(RunOptions())
        self.assertEqual(self.metadata("dynamic", D_NEW)["steps"]["render"]["reason"],
                         "render_disabled")

        retry = self.make_runner(api, kinds=["dynamic"]).retry(RunOptions())
        self.assertGreaterEqual(retry.selected, 1)
        meta = self.metadata("dynamic", D_NEW)
        self.assertEqual(meta["steps"]["render"]["status"], STEP_DONE)
        self.assertTrue((self.entry_dir("dynamic", D_NEW) / "render.png").is_file())

    def test_stage1_stage2_not_implemented_is_taken_over(self):
        """阶段 1 遗留的 skipped: stage2_not_implemented 必须能被阶段 2 接管。"""
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api, kinds=["dynamic"], render_dynamic_png=False).sync(RunOptions())

        # 手工把状态改回阶段 1 的形态，并删掉产物
        meta_path = self.entry_dir("dynamic", D_NEW) / METADATA_NAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["steps"]["render"] = {"status": "skipped", "reason": "stage2_not_implemented",
                                   "attempts": 0, "error_kind": "", "error_code": None,
                                   "message": "", "artifacts": [], "updated_at": ""}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        retry = self.make_runner(api, kinds=["dynamic"]).retry(RunOptions())
        self.assertGreaterEqual(retry.selected, 1)
        self.assertEqual(self.metadata("dynamic", D_NEW)["steps"]["render"]["status"], STEP_DONE)

    def test_permission_preview_only_is_not_retried(self):
        """权限不足是终态：重跑不会让权限变好，不该被反复打接口。"""
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api, kinds=["video"]).sync(RunOptions())
        before = api.calls.get("video_detail:BV1zveP6eEZ1", 0)
        retry = self.make_runner(api, kinds=["video"]).retry(RunOptions())
        self.assertEqual(retry.selected, 0)
        self.assertEqual(api.calls.get("video_detail:BV1zveP6eEZ1", 0), before)

    def test_media_partial_failure_then_retry_only_fills_missing(self):
        scenario = Scenario()
        api = scenario.api()
        first = fx.FakeDownloader(fail_pages=(2,))
        summary = self.make_runner(api, kinds=["video"], downloader=first).sync(RunOptions())
        outcomes = {r.platform_id: r.outcome for r in summary.results}
        self.assertEqual(outcomes[BV_NORMAL], "partial")
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["media"]["status"], STEP_FAILED)
        self.assertEqual([r["status"] for r in meta["extra"]["media"]], ["done", "failed"])

        second = fx.FakeDownloader()
        retry = self.make_runner(api, kinds=["video"], downloader=second).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(retry.counts.get("done"), 1)
        self.assertEqual(second.calls, [2], "只应补做失败的分 P")
        self.assertEqual(self.metadata("video", BV_NORMAL)["steps"]["media"]["status"], STEP_DONE)

    def test_dependency_missing_then_install_and_retry(self):
        """缺 yt-dlp → media 记 failed(dependency_missing)；装好后 retry 补做。"""
        scenario = Scenario()
        api = scenario.api()
        broken = fx.FakeDownloader(available=False)
        summary = self.make_runner(api, kinds=["video"], downloader=broken).sync(RunOptions())
        outcomes = {r.platform_id: r.outcome for r in summary.results}
        self.assertEqual(outcomes[BV_NORMAL], "failed")
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["media"]["error_kind"], "dependency_missing")
        self.assertIn("不可用", meta["steps"]["media"]["message"])

        retry = self.make_runner(api, kinds=["video"]).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(self.metadata("video", BV_NORMAL)["steps"]["media"]["status"], STEP_DONE)

    def test_transcript_asr_disabled_then_enable_asr_retry(self):
        """无字幕且 ASR 关闭 → transcript 记 asr_disabled；开启 ASR 后 retry 必须补上。"""
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api, kinds=["video"]).sync(RunOptions())
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["transcript"]["reason"], "asr_disabled")

        transcriber = fx.FakeTranscriber()
        retry = self.make_runner(api, kinds=["video"], asr_enabled=True,
                                 transcriber=transcriber,
                                 ffmpeg_runner=fx.fake_ffmpeg_runner()).retry(RunOptions())
        # 两个视频的 transcript 都是 asr_disabled → 都应被接管
        self.assertEqual(retry.selected, 2)
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["transcript"]["status"], STEP_DONE)
        self.assertEqual(meta["extra"]["transcript"]["sources"], ["asr"])
        self.assertTrue((self.entry_dir("video", BV_NORMAL) / "transcript.txt").is_file())
        # 无权限的充电视频没有媒体文件 → 记 no_media（终态），不是可无限重试的 failed
        charging = self.metadata("video", BV_CHARGE)
        self.assertEqual(charging["steps"]["transcript"]["status"], STEP_SKIPPED)
        self.assertEqual(charging["steps"]["transcript"]["reason"], "no_media")

    def test_transcript_step_failure_is_retryable(self):
        """ASR 开启但转写失败 → transcript 记 failed，可 retry。"""
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api, kinds=["video"], asr_enabled=True,
                         transcriber=fx.FakeTranscriber(fail=True),
                         ffmpeg_runner=fx.fake_ffmpeg_runner()).sync(RunOptions())
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["transcript"]["status"], STEP_FAILED)

        retry = self.make_runner(api, kinds=["video"], asr_enabled=True,
                                 transcriber=fx.FakeTranscriber(),
                                 ffmpeg_runner=fx.fake_ffmpeg_runner()).retry(RunOptions())
        self.assertGreaterEqual(retry.selected, 1)
        self.assertEqual(self.metadata("video", BV_NORMAL)["steps"]["transcript"]["status"],
                         STEP_DONE)

    def test_media_bytes_are_counted_in_summary(self):
        scenario = Scenario()
        summary = self.make_runner(scenario.api(), kinds=["video"]).sync(RunOptions())
        self.assertGreater(summary.media_bytes, 0)
        self.assertGreater(summary.media_bytes, summary.bytes_downloaded)

    def test_secrets_never_leak_into_transcript_products(self):
        scenario = Scenario()
        url = "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/zh.json?auth_key=0000000000-test-placeholder"
        api = scenario.api(
            subtitles={"1001": [fx.subtitle_entry(url=url)], "1002": [fx.subtitle_entry(url=url)]},
            subtitle_bodies={url.replace("//", "https://"): fx.subtitle_body(
                [(0.0, 1.0, "字幕内容")])},
        )
        self.make_runner(api, kinds=["video"]).sync(RunOptions())
        hits = scan_for_secrets(self.author_dir(), Redactor.from_cookie(self.cookie))
        self.assertEqual(hits, [], f"产物中发现凭据：{hits}")
        # 带 auth_key 的临时签名地址不得出现在 metadata（计划 3.2）
        meta_blob = (self.entry_dir("video", BV_NORMAL) / METADATA_NAME).read_text(encoding="utf-8")
        self.assertNotIn("auth_key", meta_blob)
        self.assertNotIn("aisubtitle.hdslb.com", meta_blob)
        # 但原始字幕内容确实落盘了（可溯源）
        self.assertTrue((self.entry_dir("video", BV_NORMAL)
                         / "transcript" / "P01.subtitle.json").is_file())


class Stage3FlowTest(FlowTestBase):
    """阶段 3 端到端：分块总结 / 受控大纲 / .mmd 与 PNG / 复用 / 独立重试。"""

    SUB_URL = "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/zh.json"

    def setUp(self):
        super().setUp()
        self.llm = {"summary_base_url": "https://api.example.com/v1", "summary_model": "fake-model-1"}
        # 让 probe_mmdc 认为 mermaid-cli 可用（真正的执行由 fake_mmdc_runner 接管）
        self.mmdc = {"mindmap_mmdc_path": str(self.fake_mmdc)}

    def options(self, **extra) -> dict:
        merged = dict(self.llm)
        merged.update(self.mmdc)
        merged.update(extra)
        return merged

    def subtitle_api(self, rows=None, *, long: bool = False):
        """带平台字幕的样本：只有公开视频有字幕（充电视频的 cid 换成 9001 → 无字幕）。"""
        scenario = Scenario()
        scenario.video_details[BV_CHARGE] = fx.video_detail(
            BV_CHARGE, ts(2026, 9, 15, 20, 31), "充电专属视频", is_upower_exclusive=True,
            pages=[{"cid": 9001, "page": 1, "part": "P1", "duration": 600,
                    "dimension": {"width": 1920, "height": 1080}}],
        )
        if long:
            rows = [(i * 1.0, i * 1.0 + 1.0, f"第{i:02d}句" + "内容" * 60) for i in range(6)]
        rows = rows or [(0.0, 1.0, "字幕第一句" + "内容" * 60),
                        (1.0, 2.0, "字幕第二句" + "内容" * 60)]
        url = self.SUB_URL
        api = scenario.api(
            subtitles={"1001": [fx.subtitle_entry(url=url)], "1002": [fx.subtitle_entry(url=url)]},
            subtitle_bodies={url.replace("//", "https://"): fx.subtitle_body(rows)},
        )
        return scenario, api

    def test_summary_and_mindmap_end_to_end(self):
        scenario, api = self.subtitle_api(long=True)
        chat = fx.FakeChatClient()
        runner = self.make_runner(api, kinds=["video"], chat_client=chat,
                                  mmdc_runner=fx.fake_mmdc_runner(),
                                  summary_chunk_chars=500, **self.options())
        summary = runner.sync(RunOptions())
        self.assertEqual(summary.counts.get("done"), 1)
        self.assertEqual(summary.counts.get("partial", 0), 0)

        entry_dir = self.entry_dir("video", BV_NORMAL)
        summary_md = (entry_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("替身模型生成的摘要正文", summary_md)
        self.assertIn("fake-model-1", summary_md)          # 需求 3.3.3：保留模型
        self.assertIn("生成时间：", summary_md)
        self.assertIn("指纹", summary_md)                  # prompt 版本/指纹

        mmd = (entry_dir / "mindmap.mmd").read_text(encoding="utf-8")
        self.assertTrue(mmd.startswith("%%"))              # 注释头 + 确定性序列化
        self.assertIn("mindmap", mmd)
        self.assertIn("root((", mmd)
        self.assertIn("第一部分要点", mmd)
        self.assertTrue((entry_dir / "mindmap.png").is_file())

        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_DONE)
        self.assertEqual(meta["steps"]["mindmap"]["status"], STEP_DONE)
        self.assertEqual(meta["extra"]["summary"]["model"], "fake-model-1")
        self.assertEqual(meta["extra"]["summary"]["outline"]["source"], "llm")
        self.assertEqual(meta["extra"]["summary"]["mindmap"]["width"], 1600)
        self.assertGreaterEqual(meta["extra"]["summary"]["calls"], 2)   # 多块要点 + 合并
        self.assertIn("summary.md", meta["steps"]["summary"]["artifacts"])
        self.assertIn("mindmap.png", meta["steps"]["mindmap"]["artifacts"])

    def test_long_transcript_tail_reaches_final_reduce(self):
        """需求 3.3.3：长稿分段处理再合并，**不丢尾段**（端到端断言最后一句进了合并输入）。"""
        rows = [(i * 1.0, i * 1.0 + 1.0, f"第{i:03d}句正文内容" + "补" * 60) for i in range(40)]
        rows.append((41.0, 42.0, "ZZZ最后一句唯一标记ZZZ"))
        _scenario, api = self.subtitle_api(rows)
        chat = fx.FakeChatClient()
        runner = self.make_runner(api, kinds=["video"], chat_client=chat,
                                  mmdc_runner=fx.fake_mmdc_runner(),
                                  summary_chunk_chars=500, **self.options())
        summary = runner.sync(RunOptions())
        self.assertEqual(summary.counts.get("done"), 1)

        meta = self.metadata("video", BV_NORMAL)
        self.assertGreater(meta["extra"]["summary"]["chunks"], 2, "应切成多块")
        self.assertGreater(len(chat.calls), meta["extra"]["summary"]["chunks"],
                           "每块一次要点调用，外加合并调用")
        self.assertIn("ZZZ最后一句唯一标记ZZZ", chat.prompts()[-1],
                      "最后一块的尾部必须出现在合并输入里（不丢尾段）")

    def test_llm_not_configured_then_configured_retry(self):
        """未配置 LLM → skipped(llm_not_configured)；配置好后 retry 必须补做。"""
        scenario, api = self.subtitle_api()
        self.make_runner(api, kinds=["video"], mmdc_runner=fx.fake_mmdc_runner(),
                         **self.mmdc).sync(RunOptions())
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_SKIPPED)
        self.assertEqual(meta["steps"]["summary"]["reason"], "llm_not_configured")
        self.assertFalse((self.entry_dir("video", BV_NORMAL) / "summary.md").exists())

        chat = fx.FakeChatClient()
        retry = self.make_runner(api, kinds=["video"], chat_client=chat,
                                 mmdc_runner=fx.fake_mmdc_runner(),
                                 **self.options()).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(retry.counts.get("done"), 1)
        self.assertEqual(self.metadata("video", BV_NORMAL)["steps"]["summary"]["status"], STEP_DONE)
        self.assertTrue((self.entry_dir("video", BV_NORMAL) / "mindmap.png").is_file())

    def test_no_transcript_never_calls_model(self):
        """需求 3.3.6：没有文字来源时不生成总结，且一次模型都不调用。"""
        scenario = Scenario()
        chat = fx.FakeChatClient()
        summary = self.make_runner(scenario.api(), kinds=["video"], chat_client=chat,
                                   mmdc_runner=fx.fake_mmdc_runner(),
                                   **self.options()).sync(RunOptions())
        self.assertEqual(summary.counts.get("done"), 1)     # 没文字不算失败
        self.assertEqual(chat.calls, [])
        meta = self.metadata("video", BV_NORMAL)
        for step in ("summary", "mindmap"):
            self.assertEqual(meta["steps"][step]["status"], STEP_SKIPPED)
            self.assertEqual(meta["steps"][step]["reason"], "no_transcript")
            self.assertIn("缺少文字来源", meta["steps"][step]["message"])

    def test_insufficient_text_is_skipped(self):
        """文字太少（< min_chars）不编造总结，记 skipped(insufficient_text)。"""
        _scenario, api = self.subtitle_api([(0.0, 1.0, "短")])
        chat = fx.FakeChatClient()
        self.make_runner(api, kinds=["video"], chat_client=chat,
                         mmdc_runner=fx.fake_mmdc_runner(),
                         **self.options()).sync(RunOptions())
        self.assertEqual(chat.calls, [])
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["reason"], "insufficient_text")
        self.assertEqual(meta["steps"]["mindmap"]["reason"], "insufficient_text")

    def test_llm_failure_is_retryable(self):
        """模型失败：summary 记 failed，mindmap 记 failed(summary_failed)；重试只补这两步。"""
        scenario, api = self.subtitle_api()
        broken = fx.FakeChatClient(fail=True)
        summary = self.make_runner(api, kinds=["video"], chat_client=broken,
                                   mmdc_runner=fx.fake_mmdc_runner(),
                                   **self.options()).sync(RunOptions())
        outcomes = {r.platform_id: r.outcome for r in summary.results}
        self.assertEqual(outcomes[BV_NORMAL], "partial")
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_FAILED)
        self.assertEqual(meta["steps"]["summary"]["error_kind"], "llm_http_error")
        self.assertEqual(meta["steps"]["mindmap"]["status"], STEP_FAILED)
        self.assertEqual(meta["steps"]["mindmap"]["reason"], "llm_failed")
        self.assertFalse((self.entry_dir("video", BV_NORMAL) / "summary.md").exists())

        chat = fx.FakeChatClient()
        retry = self.make_runner(api, kinds=["video"], chat_client=chat,
                                 mmdc_runner=fx.fake_mmdc_runner(),
                                 **self.options()).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_DONE)
        self.assertEqual(meta["steps"]["mindmap"]["status"], STEP_DONE)

    def test_one_chunk_failure_stops_without_partial_summary(self):
        """某一块要点失败 → 整条总结失败，**不生成缺失内容的总结**。"""
        _scenario, api = self.subtitle_api(long=True)
        chat = fx.FakeChatClient(fail_on_call=2)
        self.make_runner(api, kinds=["video"], chat_client=chat,
                         mmdc_runner=fx.fake_mmdc_runner(),
                         summary_chunk_chars=500, **self.options()).sync(RunOptions())
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_FAILED)
        self.assertFalse((self.entry_dir("video", BV_NORMAL) / "summary.md").exists())
        self.assertTrue(any("已停止" in note for note in meta["extra"]["summary"]["notes"]))

    def test_missing_mmdc_keeps_mmd_and_can_be_retried(self):
        """没有 mermaid-cli：summary 成功、mindmap.mmd 保留、PNG 记 failed(dependency_missing)。"""
        from unittest import mock

        scenario, api = self.subtitle_api()
        with mock.patch("bili_sub_archive.summarize.probe_mmdc", return_value=""):
            summary = self.make_runner(api, kinds=["video"], chat_client=fx.FakeChatClient(),
                                       **self.llm).sync(RunOptions())
        self.assertEqual(summary.counts.get("partial"), 1)
        entry_dir = self.entry_dir("video", BV_NORMAL)
        self.assertTrue((entry_dir / "summary.md").is_file())
        self.assertTrue((entry_dir / "mindmap.mmd").is_file())
        self.assertFalse((entry_dir / "mindmap.png").exists())
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_DONE)
        self.assertEqual(meta["steps"]["mindmap"]["status"], STEP_FAILED)
        self.assertEqual(meta["steps"]["mindmap"]["reason"], "dependency_missing")
        self.assertIn("npm install", meta["steps"]["mindmap"]["message"])

        # 装好 mermaid-cli（这里用替身）后 retry：只补渲染，不重新调用模型
        chat = fx.FakeChatClient()
        retry = self.make_runner(api, kinds=["video"], chat_client=chat,
                                 mmdc_runner=fx.fake_mmdc_runner(),
                                 **self.options()).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(chat.calls, [], "总结指纹未变 → 不该重新调用模型")
        self.assertTrue((entry_dir / "mindmap.png").is_file())
        done = self.metadata("video", BV_NORMAL)
        self.assertEqual(done["steps"]["mindmap"]["status"], STEP_DONE)
        self.assertEqual(done["steps"]["mindmap"]["reason"], "")
        # 步骤状态与详情块必须一致：status=done 不该还挂着上次失败的 reason
        detail = done["extra"]["summary"]["mindmap"]
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["reason"], "")
        self.assertEqual(detail["error_kind"], "")

    def test_reuse_skips_model_calls_when_fingerprint_matches(self):
        """媒体开关变化触发重跑整条流程，但总结指纹未变 → 不重复调用模型。"""
        scenario, api = self.subtitle_api()
        chat = fx.FakeChatClient()
        self.make_runner(api, kinds=["video"], chat_client=chat, download_media=False,
                         mmdc_runner=fx.fake_mmdc_runner(), **self.options()).sync(RunOptions())
        calls_after_first = len(chat.calls)
        self.assertGreater(calls_after_first, 0)

        # 模拟旧产物里的矛盾字段（补做成功却没清 reason）：复用分支必须清掉它
        meta_path = self.entry_dir("video", BV_NORMAL) / METADATA_NAME
        stale = json.loads(meta_path.read_text(encoding="utf-8"))
        stale["extra"]["summary"]["mindmap"]["reason"] = "render_failed"
        stale["extra"]["summary"]["mindmap"]["error_kind"] = "render_failed"
        meta_path.write_text(json.dumps(stale, ensure_ascii=False, indent=2), encoding="utf-8")

        retry = self.make_runner(api, kinds=["video"], chat_client=chat,
                                 mmdc_runner=fx.fake_mmdc_runner(),
                                 **self.options()).retry(RunOptions())
        self.assertGreaterEqual(retry.selected, 1)          # media 被开关接管，整条流程重跑
        self.assertEqual(len(chat.calls), calls_after_first, "指纹一致不该重复调用模型")
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_DONE)
        self.assertTrue(meta["extra"]["summary"]["reused"])
        self.assertTrue(meta["extra"]["summary"]["mindmap"]["reused"])
        self.assertEqual(meta["extra"]["summary"]["mindmap"]["reason"], "")
        self.assertEqual(meta["extra"]["summary"]["mindmap"]["error_kind"], "")
        # 复用分支不重新渲染，但样式仍要如实落进 metadata（PNG 就是这套样式出的）
        self.assertEqual(meta["extra"]["summary"]["mindmap"]["style"], "paper")
        self.assertEqual(meta["steps"]["media"]["status"], STEP_DONE)

    def test_prompt_change_triggers_redo(self):
        """换 prompt 文件（指纹变化）→ 只重做总结/导图，其余步骤保持 done。"""
        scenario, api = self.subtitle_api()
        chat = fx.FakeChatClient()
        self.make_runner(api, kinds=["video"], chat_client=chat,
                         mmdc_runner=fx.fake_mmdc_runner(),
                         **self.options()).sync(RunOptions())
        before = len(chat.calls)

        prompt_file = self.root / "custom_prompt.txt"
        prompt_file.write_text(
            "[full]\n以下是文字稿：\n{transcript}\n请输出摘要与大纲。\n"
            "[reduce]\n要点：{points}\n请输出摘要与大纲。\n",
            encoding="utf-8",
        )
        retry = self.make_runner(api, kinds=["video"], chat_client=chat,
                                 mmdc_runner=fx.fake_mmdc_runner(),
                                 summary_prompt_file=str(prompt_file),
                                 **self.options()).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertGreater(len(chat.calls), before, "prompt 变了必须重做总结")
        meta = self.metadata("video", BV_NORMAL)
        self.assertFalse(meta["extra"]["summary"]["reused"])
        self.assertEqual(meta["steps"]["summary"]["status"], STEP_DONE)

    def test_summary_switch_off_then_on(self):
        """--no-summary 关闭 → skipped(summary_disabled)；重新开启后 retry 补做。"""
        scenario, api = self.subtitle_api()
        chat = fx.FakeChatClient()
        self.make_runner(api, kinds=["video"], chat_client=chat, summary_enabled=False,
                         mmdc_runner=fx.fake_mmdc_runner(), **self.options()).sync(RunOptions())
        self.assertEqual(chat.calls, [])
        meta = self.metadata("video", BV_NORMAL)
        self.assertEqual(meta["steps"]["summary"]["reason"], "summary_disabled")

        retry = self.make_runner(api, kinds=["video"], chat_client=chat,
                                 mmdc_runner=fx.fake_mmdc_runner(),
                                 **self.options()).retry(RunOptions())
        self.assertEqual(retry.selected, 1)
        self.assertEqual(self.metadata("video", BV_NORMAL)["steps"]["summary"]["status"], STEP_DONE)

    def test_summary_products_never_leak_llm_key(self):
        """API 密钥不得出现在产物里（需求 5 / 计划 4）。"""
        secret = "sk-test-1234567890abcdef"
        os.environ["BSA_LLM_API_KEY"] = secret
        try:
            _scenario, api = self.subtitle_api()
            self.make_runner(api, kinds=["video"], chat_client=fx.FakeChatClient(),
                             mmdc_runner=fx.fake_mmdc_runner(),
                             **self.options()).sync(RunOptions())
        finally:
            os.environ.pop("BSA_LLM_API_KEY", None)
        hits = scan_for_secrets(self.author_dir(), Redactor.from_cookie(self.cookie, [secret]))
        self.assertEqual(hits, [], f"产物中发现密钥：{hits}")
        blob = (self.entry_dir("video", BV_NORMAL) / METADATA_NAME).read_text(encoding="utf-8")
        self.assertNotIn(secret, blob)
        self.assertIn("endpoint_host", blob)               # 只记主机名，不记完整地址


class StaleSkipMapTest(unittest.TestCase):
    """``models.stale_skip_map``：哪些 skipped 应该被重做，哪些是终态。"""

    def test_implemented_stage_is_taken_over(self):
        from bili_sub_archive.models import stale_skip_map

        mapping = stale_skip_map(Config(uid=1))
        for step in ("render", "media", "transcript"):
            self.assertIn("stage2_not_implemented", mapping.get(step, set()))
        # 阶段 3 已落地 → 历史遗留的 stage3_not_implemented 也必须被接管
        for step in ("summary", "mindmap"):
            self.assertIn("stage3_not_implemented", mapping.get(step, set()))

    def test_llm_configured_makes_skip_redoable(self):
        """当时没配 LLM 而跳过的总结/导图，配置好后 retry 必须补做。"""
        from bili_sub_archive.models import stale_skip_map

        off = stale_skip_map(Config(uid=1))
        for step in ("summary", "mindmap"):
            self.assertNotIn("llm_not_configured", off.get(step, set()))

        on = Config(uid=1, summary_base_url="https://api.example.com/v1", summary_model="m")
        mapping = stale_skip_map(on)
        for step in ("summary", "mindmap"):
            self.assertIn("llm_not_configured", mapping.get(step, set()))

    def test_no_transcript_is_terminal_for_summary(self):
        """确实没有文字时，总结/导图不该被 retry 反复选中（避免重打详情接口）。"""
        from bili_sub_archive.models import stale_skip_map

        mapping = stale_skip_map(Config(uid=1))
        for step in ("summary", "mindmap"):
            self.assertNotIn("no_transcript", mapping.get(step, set()))

    def test_config_switches_make_skips_redoable(self):
        from bili_sub_archive.models import stale_skip_map

        off = Config(uid=1)
        off.download_media = False
        off.render_dynamic_png = False
        off.asr_enabled = False
        off.prefer_subtitle = False
        off.summary_enabled = False
        off.mindmap_enabled = False
        mapping = stale_skip_map(off)
        self.assertNotIn("media_disabled", mapping.get("media", set()))
        self.assertNotIn("render_disabled", mapping.get("render", set()))
        self.assertNotIn("asr_disabled", mapping.get("transcript", set()))
        self.assertNotIn("summary_disabled", mapping.get("summary", set()))
        self.assertNotIn("mindmap_disabled", mapping.get("mindmap", set()))

        on = Config(uid=1)
        on.download_media = True
        on.render_dynamic_png = True
        on.asr_enabled = True
        on.prefer_subtitle = True
        on.summary_enabled = True
        on.mindmap_enabled = True
        mapping = stale_skip_map(on)
        self.assertIn("media_disabled", mapping["media"])
        self.assertIn("render_disabled", mapping["render"])
        self.assertIn("asr_disabled", mapping["transcript"])
        self.assertIn("subtitle_disabled", mapping["transcript"])
        self.assertIn("summary_disabled", mapping["summary"])
        self.assertIn("mindmap_disabled", mapping["mindmap"])

    def test_terminal_skips_are_never_stale(self):
        from bili_sub_archive.models import stale_skip_map

        mapping = stale_skip_map(Config(uid=1))
        flat = {reason for reasons in mapping.values() for reason in reasons}
        self.assertNotIn("permission_preview_only", flat)
        self.assertNotIn("content_blocked", flat)
        self.assertNotIn("no_transcript", flat)
        self.assertNotIn("no_images", flat)


class GuardTest(FlowTestBase):
    def test_missing_cookie_raises(self):
        os.environ.pop("BSA_COOKIE", None)
        scenario = Scenario()
        with self.assertRaises(CredentialError):
            self.make_runner(scenario.api()).sync(RunOptions())

    def test_unknown_uid_raises_config_error(self):
        from bili_sub_archive.errors import ConfigError

        scenario = Scenario()
        api = scenario.api()
        api.author_info = lambda mid: (None, fx.fail(-404, "啥都木有"))
        with self.assertRaises(ConfigError) as ctx:
            self.make_runner(api).sync(RunOptions())
        self.assertIn("请确认 UID", str(ctx.exception))

    def test_author_card_auth_failure_raises_credential_error(self):
        scenario = Scenario()
        api = scenario.api()
        api.author_info = lambda mid: (None, fx.fail(-101, "账号未登录"))
        with self.assertRaises(CredentialError):
            self.make_runner(api).sync(RunOptions())

    def test_concurrent_lock_blocks_second_run(self):
        from bili_sub_archive.paths import DirLock

        scenario = Scenario()
        runner = self.make_runner(scenario.api())
        author_dir, _ = __import__("bili_sub_archive.paths", fromlist=["x"]).resolve_author_dir(
            self.out, 1000, fx.AUTHOR_NAME)
        with DirLock(author_dir):
            with self.assertRaises(Exception):
                runner.sync(RunOptions())

    def test_force_reruns_completed_entries(self):
        scenario = Scenario()
        api = scenario.api()
        self.make_runner(api).sync(RunOptions())
        downloads_first = len(api.downloads)
        summary = self.make_runner(api).sync(RunOptions(force=True))
        self.assertEqual(summary.counts.get("skipped", 0), 0)
        self.assertGreaterEqual(len(api.downloads), downloads_first)

    def test_store_reload_matches_index(self):
        scenario = Scenario()
        self.make_runner(scenario.api()).sync(RunOptions())
        store = Store(self.author_dir(), uid=1000, author_name=fx.AUTHOR_NAME).load()
        self.assertEqual(len(store.entries), 11)
        self.assertEqual(store.get("video", BV_NORMAL).step("fetch").status, STEP_DONE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

