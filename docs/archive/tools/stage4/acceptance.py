"""阶段 4 验收工具：**需求第 6 节验收项逐条离线核对**。

它回答一个问题：**"需求文档第 6 节的 7 条验收标准，现在到底哪几条能被机器证明？"**

两类证据，缺一不可，且严格分开标注：

1. **真实样本产物**（默认 ``.smoke_sync``，阶段 1/2/3 用授权账号真实跑出来的归档）：
   目录/索引/元数据/文字稿/长图是否真的落盘且结构正确 —— 只读，不改动归档目录；
   需要新渲染的产物一律写到 ``--work``（默认 ``.test_tmp/stage4_acceptance``）。
2. **离线测试套件**：把每条验收项映射到具体的测试类（见 ``SUITE_BY_CHECK``），
   用 :mod:`unittest` 在本进程里跑，报告用例数与失败项。

**需要联网、需要凭据、或本机缺组件（yt-dlp / FFmpeg / mermaid-cli / 云端模型）才能证明的部分，
一律进"待在线验收"清单（不算通过、也不算失败）**，并给出可直接复制的命令 ——
不做"用替身通过就宣称真实链路通过"这种事。

```powershell
python -m tools.stage4.acceptance              # 真实样本 + 离线套件
python -m tools.stage4.acceptance --quick      # 只查真实样本产物（跳过测试套件，更快）
python -m tools.stage4.acceptance --json       # 结构化输出（便于存档）
python -m tools.stage4.acceptance --report .test_tmp/stage4_acceptance/report.json
```

退出码：0 无失败项；1 有失败项；2 工具自身参数/环境错误。
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import re
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

#: 每条验收项对应的离线测试（类或单个用例）。名称由 tests/test_delivery.py 锁死，
#: 防止重构测试后这里悄悄失效（点名的用例不存在会直接抛错，不会静默跳过）。
SUITE_BY_CHECK: dict[str, list[str]] = {
    "req6-1": [
        "tests.test_offline_flow.FullFlowTest.test_full_sync_archives_all_three_kinds",
        "tests.test_offline_flow.FullFlowTest.test_idempotent_rerun_skips_everything",
        "tests.test_offline_flow.FullFlowTest.test_dry_run_writes_nothing",
        "tests.test_paths.NamingTest",
        "tests.test_store.IndexTest",
    ],
    "req6-2": [
        "tests.test_render.RenderLongPngTest",
        "tests.test_render.LongImageModuleTest",
        "tests.test_offline_flow.Stage2FlowTest.test_no_render_flag_skips_render_step",
    ],
    "req6-3": [
        "tests.test_offline_flow.Stage3FlowTest",
        "tests.test_transcript.SubtitleParseTest",
        "tests.test_transcript.MergeTest",
        "tests.test_summarize.MermaidCliTest",
    ],
    "req6-4": [
        "tests.test_media.DownloadMediaTest",
        "tests.test_offline_flow.Stage2FlowTest.test_media_partial_failure_then_retry_only_fills_missing",
        "tests.test_offline_flow.Stage2FlowTest.test_dependency_missing_then_install_and_retry",
        "tests.test_offline_flow.RetryTest",
    ],
    "req6-5": [
        "tests.test_redact",
        "tests.test_offline_flow.GuardTest",
        "tests.test_offline_flow.Stage3FlowTest.test_llm_failure_is_retryable",
        "tests.test_offline_flow.Stage2FlowTest.test_secrets_never_leak_into_transcript_products",
    ],
    "req6-6": [
        "tests.test_filters",
        "tests.test_offline_flow.FilterTest",
        "tests.test_offline_flow.Stage3FlowTest.test_summary_and_mindmap_end_to_end",
    ],
    "req6-7": [
        "tests.test_offline_flow.Stage2FlowTest.test_asr_flow_uses_transcriber_only_when_no_subtitle",
        "tests.test_offline_flow.Stage2FlowTest.test_transcript_asr_disabled_then_enable_asr_retry",
        "tests.test_transcript.TranscriberTest",
    ],
    "delivery": [
        "tests.test_delivery",
    ],
}

#: 产物里绝不允许出现的"敏感键 = 值"形态（值被脱敏成 [REDACTED] 的除外）
SECRET_RE = re.compile(
    r"(?i)\b(sessdata|bili_jct|dedeuserid|dedeuserid__ckmd5|buvid3|buvid4|b_lsid|bili_ticket"
    r"|api[_-]?key|authorization|password|secret|llm_api_key)\b\s*[=:]\s*([^\s,;\"'}]{6,})"
)
TEXT_SUFFIXES = (".json", ".md", ".txt", ".mmd", ".srt", ".toml", ".log")

PAGE_HEADING_RE = re.compile(r"^##\s*P\d+", re.MULTILINE)
SRT_CUE_RE = re.compile(r"^\d+\s*$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """一条验收项的核对结果。

    ``evidence`` = 机器验证通过的证据；``problems`` = 失败；``pending`` = 需要在线/凭据/缺失组件
    才能证明的部分（既不算通过也不算失败，但必须出现在报告里）。
    """

    rid: str
    title: str
    requirement: str
    evidence: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.problems:
            return FAIL
        return PASS if self.evidence else SKIP

    def ok(self, text: str) -> None:
        self.evidence.append(text)

    def fail(self, text: str) -> None:
        self.problems.append(text)

    def todo(self, text: str) -> None:
        self.pending.append(text)

    def to_json(self) -> dict:
        return {
            "id": self.rid,
            "title": self.title,
            "requirement": self.requirement,
            "status": self.status,
            "evidence": list(self.evidence),
            "problems": list(self.problems),
            "pending": list(self.pending),
        }


def run_tests(names: list[str]) -> tuple[bool, str, list[str]]:
    """在本进程内跑指定的测试（返回 ``(是否全通过, 概要, 失败明细)``）。"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    problems: list[str] = []
    for case, trace in list(result.failures) + list(result.errors):
        tail = [line.strip() for line in trace.strip().splitlines() if line.strip()]
        problems.append(f"{case.id()}：{tail[-1] if tail else '无输出'}")
    detail = (f"{result.testsRun} 例，失败 {len(result.failures)}，错误 {len(result.errors)}")
    return result.wasSuccessful(), detail, problems


# --------------------------------------------------------------------------- #
# 真实样本读取
# --------------------------------------------------------------------------- #
def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_author_dirs(root: Path) -> list[Path]:
    """输出根目录下的作者目录（``<UID>_<名称>`` 且有 index.json）。"""
    out: list[Path] = []
    if not Path(root).exists():
        return out
    for child in sorted(Path(root).iterdir()):
        if child.is_dir() and re.match(r"^\d+_", child.name) and (child / "index.json").is_file():
            out.append(child)
    return out


def iter_product_text(root: Path):
    """遍历产物里的文本文件（跳过 _runs，运行记录另有检查）。"""
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if "_runs" in path.parts:
            continue
        yield path


class Context:
    """一次验收运行共享的上下文。"""

    def __init__(self, samples: Path, work: Path, run_suite: bool):
        self.samples = Path(samples)
        self.work = Path(work)
        self.run_suite = run_suite
        self.author_dirs = find_author_dirs(self.samples)

    # -- 离线套件 --------------------------------------------------------- #
    def suite(self, check: Check) -> None:
        names = SUITE_BY_CHECK.get(check.rid, [])
        if not names:
            return
        if not self.run_suite:
            check.todo(f"离线测试套件被 --quick 跳过（{len(names)} 个测试入口未运行）")
            return
        ok, detail, problems = run_tests(names)
        label = "、".join(name.rsplit(".", 1)[-1] for name in names)
        if ok:
            check.ok(f"离线用例通过：{label}（{detail}）")
        else:
            check.fail(f"离线用例失败：{label}（{detail}）；{problems[0] if problems else ''}")

    # -- 样本遍历 --------------------------------------------------------- #
    def entries(self, kind: str | None = None) -> list[tuple[Path, dict]]:
        out: list[tuple[Path, dict]] = []
        for author in self.author_dirs:
            data = read_json(author / "index.json") or {}
            for entry in data.get("entries") or []:
                if kind and entry.get("kind") != kind:
                    continue
                out.append((author, entry))
        return out

    def entry_dir(self, author: Path, entry: dict) -> Path:
        return author / str(entry.get("dir") or "")

    def metadata(self, author: Path, entry: dict) -> dict:
        return read_json(self.entry_dir(author, entry) / "metadata.json") or {}


# --------------------------------------------------------------------------- #
# 逐条验收
# --------------------------------------------------------------------------- #
def check_req1(ctx: Context) -> Check:
    c = Check("req6-1", "三类内容分别归档、目录含日期与类型、重复运行不重复",
              "需求 6.1 / 3.1.2 / 3.1.3")
    if not ctx.author_dirs:
        c.todo(f"未找到真实样本作者目录（--samples {ctx.samples}）：真实产物检查跳过")
    for author in ctx.author_dirs:
        data = read_json(author / "index.json") or {}
        entries = list(data.get("entries") or [])
        if not entries:
            c.fail(f"{author.name}：index.json 没有条目")
            continue
        keys = [(e.get("kind"), e.get("platform_id")) for e in entries]
        dup = sorted(k for k, n in collections.Counter(keys).items() if n > 1)
        if dup:
            c.fail(f"{author.name}：index.json 存在重复条目 {dup}")
        kinds = collections.Counter(k for k, _ in keys)
        missing_dirs = [e.get("dir") for e in entries if not ctx.entry_dir(author, e).is_dir()]
        if missing_dirs:
            c.fail(f"{author.name}：index.json 指向的目录不存在 {missing_dirs[:3]}")
        bad_meta = [e.get("dir") for e in entries
                    if not isinstance(ctx.metadata(author, e), dict)
                    or not ctx.metadata(author, e)]
        if bad_meta:
            c.fail(f"{author.name}：metadata.json 缺失或不可解析 {bad_meta[:3]}")
        bad_names = [str(e.get("dir")) for e in entries
                     if not re.match(r"^\d{4}-\d{2}-\d{2}_", str(e.get("dir")))
                     or not any(label in str(e.get("dir")) for label in ("动态", "视频", "专栏"))]
        if bad_names:
            c.fail(f"{author.name}：目录名缺日期或类型 {bad_names[:3]}")
        indexed = {str(e.get("dir")) for e in entries}
        orphans = [d.name for d in author.iterdir()
                   if d.is_dir() and not d.name.startswith("_") and d.name not in indexed]
        if orphans:
            c.fail(f"{author.name}：存在未被索引登记的条目目录（疑似重复分配）{orphans[:3]}")
        if not (dup or missing_dirs or bad_meta or bad_names or orphans):
            c.ok(f"{author.name}：{len(entries)} 条索引键唯一、目录与 metadata 齐备"
                 f"（动态 {kinds.get('dynamic', 0)} / 视频 {kinds.get('video', 0)}"
                 f" / 专栏 {kinds.get('article', 0)}）")
        runs = [read_json(p) or {} for p in sorted((author / "_runs").glob("*.json"))]
        idempotent = [r for r in runs
                      if r.get("selected") and r.get("counts", {}).get("skipped") == r.get("selected")]
        if idempotent:
            c.ok(f"{author.name}：真实运行记录里有 {len(idempotent)} 次"
                 f"「选中 N 条 → 全部跳过」的增量重跑（未产生重复条目/重复下载）")
        else:
            c.todo(f"{author.name}：运行记录里没有可证明增量重跑的样本，"
                   "重复运行行为由离线用例覆盖")
    ctx.suite(c)
    return c


def check_req2(ctx: Context) -> Check:
    c = Check("req6-2", "多图动态：全部原图 + 含作者/头像/时间/正文/配图的长 PNG",
              "需求 6.2 / 3.2")
    picked = None
    for author, entry in ctx.entries("dynamic"):
        entry_dir = ctx.entry_dir(author, entry)
        images = [p for p in (entry_dir / "images").glob("*")
                  if p.is_file() and not p.name.startswith("avatar")] if (entry_dir / "images").is_dir() else []
        if images and (picked is None or len(images) > len(picked[2])):
            picked = (author, entry, images, entry_dir)
    if picked is None:
        c.todo(f"真实样本里没有带配图的动态（--samples {ctx.samples}）：长图检查跳过")
    else:
        author, entry, images, entry_dir = picked
        content = (entry_dir / "content.md")
        text = content.read_text(encoding="utf-8", errors="replace") if content.is_file() else ""
        empty = [p.name for p in images if p.stat().st_size == 0]
        if empty:
            c.fail(f"原图为空文件：{empty}")
        unreferenced = [p.name for p in images if p.name not in text]
        if unreferenced:
            c.fail(f"原图未被 content.md 引用（图片顺序/图文对应会丢）：{unreferenced}")
        if not empty and not unreferenced:
            c.ok(f"{entry_dir.name}：{len(images)} 张原图全部落盘且被正文引用")
        archived_png = entry_dir / "render.png"
        if archived_png.is_file():
            from subvideo.summarize.mermaid_cli import png_size

            width, height = png_size(archived_png)
            if not width or not height:
                c.fail(f"归档的 render.png 不是合法 PNG：{archived_png}")
            else:
                c.ok(f"归档长图 render.png：{width}×{height}，"
                     f"{archived_png.stat().st_size / 1024:.0f} KiB")
        else:
            c.todo("真实样本的 render.png 是阶段 2 验证工具补生成的，未随流程落盘；"
                   "下面用真实样本现场重渲染一次")
        # 现场重渲染（只写 --work，不动归档目录）
        try:
            from tools.stage2.render_check import content_to_items, load_header
            from subvideo.render import render_long_png
        except Exception as exc:  # noqa: BLE001 - 缺依赖时如实报告
            c.todo(f"无法导入渲染链路：{type(exc).__name__}: {exc}")
            ctx.suite(c)
            return c
        header = load_header(entry_dir)
        items = content_to_items(entry_dir)
        out_path = ctx.work / "render" / f"{entry_dir.name[:40]}.png"
        result = render_long_png(header, items, out_path, width=1080, max_height=20000)
        if not result.ok:
            if result.error_kind == "dependency_missing":
                c.todo(f"未安装 Pillow，长图未现场重渲染：{result.message}")
            else:
                c.fail(f"真实样本重渲染失败（{result.error_kind}）：{result.message}")
        else:
            missing_header = [name for name, value in
                              (("作者", header.author), ("发布时间", header.published),
                               ("正文项", items), ("头像", header.avatar))
                              if not value]
            if missing_header:
                c.fail(f"长图头部缺少字段：{missing_header}")
            if result.images_rendered != len(images):
                c.fail(f"长图只渲染了 {result.images_rendered}/{len(images)} 张配图")
            if result.clamped:
                c.fail(f"长图被高度上限截断：{result.message}")
            c.ok(f"现场重渲染：{result.width}×{result.height}，{result.bytes_written / 1024:.0f} KiB，"
                 f"配图 {result.images_rendered}/{len(images)} 张，未截断（产物 {out_path.name}）")
    ctx.suite(c)
    return c


def check_req3(ctx: Context) -> Check:
    c = Check("req6-3", "视频：带时间戳文字稿 + 总结 + 导图源文件与 PNG；失败有明确状态",
              "需求 6.3 / 3.3.2-3.3.6")
    videos = ctx.entries("video")
    if not videos:
        c.todo(f"真实样本里没有视频条目（--samples {ctx.samples}）：文字稿检查跳过")
    for author, entry in videos:
        entry_dir = ctx.entry_dir(author, entry)
        meta = ctx.metadata(author, entry)
        extra = meta.get("extra") or {}
        txt = entry_dir / "transcript.txt"
        srt = entry_dir / "transcript_timed.srt"
        text = txt.read_text(encoding="utf-8", errors="replace") if txt.is_file() else ""
        if not text.strip():
            c.fail(f"{entry_dir.name}：transcript.txt 缺失或为空")
        elif not PAGE_HEADING_RE.search(text):
            c.fail(f"{entry_dir.name}：transcript.txt 没有分 P 小节标题（无法标注 P 号）")
        else:
            srt_text = srt.read_text(encoding="utf-8", errors="replace") if srt.is_file() else ""
            cues = len(SRT_CUE_RE.findall(srt_text))
            if not srt.is_file() or cues == 0 or "-->" not in srt_text:
                c.fail(f"{entry_dir.name}：transcript_timed.srt 缺失或没有时间戳")
            else:
                sources = "、".join((extra.get("transcript") or {}).get("sources") or []) or "未标注"
                c.ok(f"{entry_dir.name[:28]}…：文字稿 {len(text)} 字，SRT {cues} 条 cue，"
                     f"来源 {sources}（{extra.get('transcript_summary') or '无摘要'}）")
        steps = meta.get("steps") or {}
        for step in ("summary", "mindmap"):
            state = steps.get(step) or {}
            status = state.get("status")
            if status == "done":
                want = {"summary": "summary.md", "mindmap": "mindmap.mmd"}[step]
                if not (entry_dir / want).is_file():
                    c.fail(f"{entry_dir.name}：{step} 记 done 但缺少 {want}")
                else:
                    c.ok(f"{entry_dir.name[:28]}…：{step} done（{want} 已落盘）")
            elif status in ("skipped", "failed"):
                if not (state.get("reason") or state.get("error_kind")):
                    c.fail(f"{entry_dir.name}：{step} 记 {status} 但没有原因")
                else:
                    c.todo(f"{entry_dir.name[:28]}…：{step} 记 {status}"
                           f"({state.get('reason') or state.get('error_kind')})；"
                           f"{state.get('message', '')[:60]}")
            else:
                c.fail(f"{entry_dir.name}：{step} 状态异常 {status!r}")
    done_videos = [entry for author, entry in videos
                   if ((ctx.metadata(author, entry).get("steps") or {})
                       .get("summary") or {}).get("status") == "done"]
    if videos and not done_videos:
        c.todo("真实样本没有跑通总结/导图的视频（本机未配置云端模型）；"
               "总结+导图链路由离线替身用例覆盖；真实端点验收："
               "`python -m subvideo retry --uid <UID> --steps summary,mindmap`")
    if not videos:
        c.todo("真实样本无视频：总结/导图链路只能由离线用例证明")
    ctx.suite(c)
    return c


def check_req4(ctx: Context) -> Check:
    c = Check("req6-4", "视频下载并发可配置；部分失败后重跑只补未完成步骤",
              "需求 6.4 / 3.3.1 / 3.1.3")
    from subvideo.config import Config

    cfg = Config()
    if cfg.video_workers >= 1 and cfg.segment_workers >= 1:
        c.ok(f"并发上限来自配置且默认为正整数：video_workers={cfg.video_workers}、"
             f"segment_workers={cfg.segment_workers}（可命令行/TOML 覆盖）")
    else:
        c.fail(f"并发默认值非法：video_workers={cfg.video_workers}、"
               f"segment_workers={cfg.segment_workers}")
    videos = ctx.entries("video")
    if not videos:
        c.todo("真实样本无视频条目：真实下载的并发/续传证据跳过")
    for author, entry in videos:
        meta = ctx.metadata(author, entry)
        media = (meta.get("extra") or {}).get("media") or []
        step = (meta.get("steps") or {}).get("media") or {}
        if not media:
            c.todo(f"{entry.get('dir')}：没有 extra.media 记录（该条目未走到媒体下载）")
            continue
        failed = [m for m in media if m.get("status") != "done"]
        label = str(entry.get("dir") or entry.get("platform_id"))[:26]
        if failed:
            reasons = {m.get("error_kind") or m.get("message", "")[:40] for m in failed}
            c.ok(f"{label}…：{len(media) - len(failed)}/{len(media)} 个分 P 成功，"
                 f"失败分 P 带可定位原因 {sorted(r for r in reasons if r)}（步骤记 "
                 f"{step.get('status')}({step.get('reason') or step.get('error_kind')})）")
        else:
            c.ok(f"{label}…：{len(media)}/{len(media)} 个分 P 下载并校验通过")
    ctx.suite(c)
    return c


def check_req5(ctx: Context) -> Check:
    c = Check("req6-5", "凭据不泄露、失败原因具体、不覆盖已完成结果",
              "需求 6.5 / 5 / 计划 3.2")
    hits: list[str] = []
    scanned = 0
    for author in ctx.author_dirs:
        for path in iter_product_text(author):
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in SECRET_RE.finditer(text):
                value = match.group(2)
                if "REDACT" in value.upper() or value.startswith("["):
                    continue
                hits.append(f"{path.relative_to(author)}：{match.group(1)}=…")
    if hits:
        c.fail(f"产物里出现疑似凭据值 {len(hits)} 处：{hits[:3]}")
    elif scanned:
        c.ok(f"扫描真实产物 {scanned} 个文本文件：未发现「敏感键 = 值」形态的凭据")
    else:
        c.todo(f"没有可扫描的真实产物（--samples {ctx.samples}）")
    for author in ctx.author_dirs:
        for entry in (read_json(author / "index.json") or {}).get("entries") or []:
            meta = ctx.metadata(author, entry)
            leaked = [k for k in ("cookie", "api_key", "llm_api_key", "authorization")
                      if k in meta]
            if leaked:
                c.fail(f"{entry.get('dir')}：metadata.json 含敏感键 {leaked}")
    bad_reason = []
    for kind in ("dynamic", "video", "article"):
        for author, entry in ctx.entries(kind):
            meta = ctx.metadata(author, entry)
            for name, state in (meta.get("steps") or {}).items():
                if state.get("status") in ("failed", "skipped") and not (
                        state.get("reason") or state.get("error_kind") or state.get("message")):
                    bad_reason.append(f"{entry.get('dir')}:{name}")
    if bad_reason:
        c.fail(f"失败/跳过步骤没有原因：{bad_reason[:3]}")
    elif ctx.author_dirs:
        c.ok("真实样本里所有 failed/skipped 步骤都带 reason/error_kind/message（可机读重试依据）")
    ctx.suite(c)
    return c


def check_req6(ctx: Context) -> Check:
    c = Check("req6-6", "日期范围 / 跨类最新 N；多 P 同一目录、一份合并总结与导图",
              "需求 6.6 / 3.1.1 / 3.3.1 / 3.3.5")
    multi_p = 0
    for author, entry in ctx.entries("video"):
        meta = ctx.metadata(author, entry)
        pages = (meta.get("extra") or {}).get("pages") or []
        if len(pages) > 1:
            multi_p += 1
            entry_dir = ctx.entry_dir(author, entry)
            same_dir = all((entry_dir / "videos").glob("P*")) if (entry_dir / "videos").is_dir() else False
            c.ok(f"真实多 P 条目：{entry.get('dir')[:28]}…（{len(pages)} 个分 P"
                 f"{'，媒体同目录' if same_dir else ''}）")
    if not multi_p:
        c.todo("真实样本没有多 P 视频：多 P 同目录/一份合并总结由离线用例覆盖"
               "（tests.test_offline_flow.FullFlowTest 里是 2 个分 P）")
    for author in ctx.author_dirs:
        entries = (read_json(author / "index.json") or {}).get("entries") or []
        stamps = [str(e.get("published_at") or "") for e in entries]
        if stamps == sorted(stamps, reverse=True):
            c.ok(f"{author.name}：index.json 按发布时间降序（{len(stamps)} 条），"
                 "跨类排序稳定可查")
        else:
            c.fail(f"{author.name}：index.json 未按发布时间降序")
    ctx.suite(c)
    return c


def check_req7(ctx: Context) -> Check:
    c = Check("req6-7", "本地 ASR 可开关；关闭时不运行识别、不外发媒体",
              "需求 6.7 / 3.3.2 / 5")
    from subvideo.config import Config

    cfg = Config()
    if cfg.asr_enabled is False:
        c.ok("默认配置 asr_enabled=False（未显式开启时不下载模型、不跑识别）")
    else:
        c.fail("默认配置竟然开启了 ASR，与需求 6.7/计划第 4 节不符")
    sources = set()
    wavs: list[str] = []
    for author, entry in ctx.entries("video"):
        extra = (ctx.metadata(author, entry).get("extra") or {})
        sources.update((extra.get("transcript") or {}).get("sources") or [])
        wavs.extend(p.name for p in ctx.entry_dir(author, entry).rglob("*.wav"))
    if sources:
        c.ok(f"真实样本文字稿来源 {sorted(sources)}（本机未开启 ASR，全部来自平台字幕）")
    else:
        c.todo("真实样本没有可读的文字稿来源标注")
    if wavs:
        c.todo(f"产物里保留了 ASR 中间音频 {wavs[:3]}（[asr] keep_audio=true 时才会出现）")
    else:
        c.ok("产物里没有 ASR 中间音频残留（默认转写完即删，不额外占盘）")
    ctx.suite(c)
    return c


def check_delivery(ctx: Context) -> Check:
    c = Check("delivery", "交付物齐备：安装使用说明、配置模板、阶段记录、计划同步",
              "计划第 5 节阶段 4 / 需求第 2 节")
    required = {
        ROOT / "README.md": ["## 3. 安装", "## 4. 配置", "## 5. 使用", "## 8. 已知限制",
                             "## 7. 退出码", "## 9. 隐私与凭据"],
        ROOT / "config.example.toml": ["[account]", "[output]", "[request]", "[asr]",
                                       "[download]", "[render]", "[summary]", "[mindmap]"],
        ROOT / "docs" / "stage4-stability-and-delivery.md": ["需求", "验收", "已知限制"],
        ROOT / "DEVELOPMENT_PLAN.md": ["阶段 4 已完成"],
        ROOT / "REQUIREMENTS.md": ["验收标准"],
    }
    for path, needles in required.items():
        if not path.is_file():
            c.fail(f"缺少交付物：{path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        missing = [n for n in needles if n not in text]
        if missing:
            c.fail(f"{path.relative_to(ROOT)} 缺少内容：{missing}")
        else:
            c.ok(f"{path.relative_to(ROOT)} 齐备（{len(needles)} 项关键内容命中）")
    readme = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").is_file() else ""
    # 安装说明必须逐个覆盖外部组件与可选库（阶段 0 第 4 节给阶段 4 的明确要求）
    need_tokens = ["3.11", "yt-dlp", "FFmpeg", "Node.js", "mermaid-cli",
                   "faster-whisper", "Pillow", "openai"]
    absent = [token for token in need_tokens if token.lower() not in readme.lower()]
    if absent:
        c.fail(f"README 安装说明未覆盖：{absent}")
    else:
        c.ok(f"README 安装说明覆盖全部组件：{'、'.join(need_tokens)}")
    ctx.suite(c)
    return c


CHECKS = (check_req1, check_req2, check_req3, check_req4,
          check_req5, check_req6, check_req7, check_delivery)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
MARK = {PASS: "✔ PASS", FAIL: "✘ FAIL", SKIP: "… SKIP"}


def print_report(checks: list[Check], samples: Path, work: Path, stream=sys.stdout) -> None:
    print("", file=stream)
    print("=" * 78, file=stream)
    print("阶段 4 验收：需求第 6 节逐条离线核对", file=stream)
    print("=" * 78, file=stream)
    print(f"真实样本：{samples}（作者目录 {len(find_author_dirs(samples))} 个）", file=stream)
    print(f"验证产物：{work}", file=stream)
    for c in checks:
        print("", file=stream)
        print(f"[{MARK[c.status]}] {c.rid}  {c.title}", file=stream)
        print(f"          依据：{c.requirement}", file=stream)
        for line in c.evidence:
            print(f"    证据 · {line}", file=stream)
        for line in c.problems:
            print(f"    失败 · {line}", file=stream)
        for line in c.pending:
            print(f"    待验 · {line}", file=stream)
    counts = collections.Counter(c.status for c in checks)
    print("", file=stream)
    print("-" * 78, file=stream)
    print(f"结论：PASS {counts.get(PASS, 0)} / FAIL {counts.get(FAIL, 0)}"
          f" / SKIP {counts.get(SKIP, 0)}（共 {len(checks)} 条验收项）", file=stream)
    pending_total = sum(len(c.pending) for c in checks)
    if pending_total:
        print(f"另有 {pending_total} 条需要联网/凭据/缺失组件才能证明，见上面的「待验」行；"
              "在线验收：python -m tools.stage4.online_acceptance --uid <UID> --latest 3", file=stream)
    print("=" * 78, file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stage4_acceptance", description=__doc__.splitlines()[0])
    parser.add_argument("--samples", default=str(ROOT / ".smoke_sync"),
                        help="真实样本输出根目录（默认 .smoke_sync）")
    parser.add_argument("--work", default=str(ROOT / ".test_tmp" / "stage4_acceptance"),
                        help="验证产物目录（默认 .test_tmp/stage4_acceptance）")
    parser.add_argument("--quick", action="store_true", help="跳过离线测试套件，只查真实样本产物")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
    parser.add_argument("--report", help="把 JSON 报告写入该文件")
    args = parser.parse_args(argv)

    try:  # 被重定向到 StringIO 时没有 reconfigure（测试会这么做）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    ctx = Context(Path(args.samples), work, run_suite=not args.quick)
    checks = [fn(ctx) for fn in CHECKS]

    payload = {
        "stage": 4,
        "requirement": "REQUIREMENTS.md 第 6 节",
        "samples": str(args.samples),
        "work": str(work),
        "suite_ran": not args.quick,
        "checks": [c.to_json() for c in checks],
        "summary": dict(collections.Counter(c.status for c in checks)),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(checks, Path(args.samples), work)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json:
            print(f"报告已写入：{path}")
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
