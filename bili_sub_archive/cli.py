"""命令行入口：``check`` / ``sync`` / ``retry``（命令 ``bsa``，全名 ``bili-sub-archive``）。

```powershell
bsa check
bsa sync --uid 123456 --from 2026-01-01 --to 2026-09-23
bsa sync --uid 123456 --latest 20 --asr --video-workers 3
bsa retry --uid 123456 --steps summary,mindmap
```

功能面：视频分 P 并发下载/续传与校验、平台字幕提取、本地 ASR 兜底
（``--asr``，默认关闭）、跨 P 文字稿合并、动态单张长 PNG；
OpenAI 兼容接口的分块总结（``--summary-*``）、自定义 prompt、
Mermaid ``mindmap.mmd`` 与 ``mindmap.png``（``--no-summary`` / ``--no-mindmap`` 可关）。
全局开关 ``--config``/``--json``/``-v``/``--quiet`` 写在子命令**前面或后面**都生效。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .bili.api import BilibiliApi
from .bili.client import HttpClient
from .config import LoadedConfig, load_config
from .errors import (
    EXIT_UNEXPECTED,
    ConfigError,
    BiliSubArchiveError,
    exit_code_for_kind,
)
from .log import LogAdapter, configure_stdio, setup_logger
from .models import ALL_KINDS, STEPS_BY_KIND, RunSummary
from .runner import Runner, RunOptions
from .timeutil import parse_date
from .ui import OUTCOME_TONES, Ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bsa",
        description="B 站 UP 主内容归档工具 bili-sub-archive（动态/视频/专栏 → 本地归档、文字稿、总结与思维导图）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "配置优先级：默认值 < config.toml < config.local.toml < 环境变量/命令行。\n"
            "Cookie 从 BSA_COOKIE 环境变量（旧名 SUBVIDEO_COOKIE 仍兼容）、config.local.toml "
            "或 require.txt 读取，绝不写入产物。\n"
            "本地 ASR 默认关闭；关闭时不下载模型、不运行识别，也不把媒体发送给任何云端识别服务。\n"
            "总结只把文字稿发给用户配置的 OpenAI 兼容接口（BSA_LLM_API_KEY / [summary]），"
            "不发送 Cookie、媒体文件或原视频地址。"
        ),
    )
    parser.add_argument("--version", action="version",
                        version=f"bili-sub-archive {__version__}（命令 bsa）")
    parser.add_argument("--config", help="指定配置文件（默认读 config.toml 与 config.local.toml）")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="输出调试日志")
    parser.add_argument("--quiet", action="store_true", help="只输出警告与错误")
    parser.add_argument("--json", action="store_true", help="运行摘要以 JSON 输出到 stdout")
    add_ui_flags(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="检查依赖、配置与登录态（不发起批量下载）")
    add_common(check)
    check.add_argument("--scan-output", action="store_true",
                       help="扫描输出目录产物，确认没有凭据泄漏")

    sync = sub.add_parser("sync", help="发现并归档（首次采集与增量重跑同一命令）")
    add_common(sync)
    sync.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD（北京时间，含当日）")
    sync.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD（北京时间，含当日）")
    sync.add_argument("--latest", type=int, help="跨三类按发布时间合计取最新 N 条")
    sync.add_argument("--types", help=f"内容类型，逗号分隔：{','.join(ALL_KINDS)}（默认全部）")
    add_override_flags(sync)
    sync.add_argument("--force", action="store_true", help="忽略既有状态，强制重跑所有步骤")
    sync.add_argument("--dry-run", action="store_true", help="只做发现与筛选，不落盘")

    retry = sub.add_parser("retry", help="只补做失败或被配置变更影响的步骤")
    add_common(retry)
    add_override_flags(retry)
    retry.add_argument("--steps", help="只补做这些步骤（逗号分隔），默认全部未结算步骤")
    retry.add_argument("--force", action="store_true", help="忽略状态，强制重跑选中条目")
    return parser


def add_ui_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """终端呈现开关（全局：主解析器与子命令都挂，前后写都生效）。

    ``--color``：``auto``（默认，看是否交互终端）/ ``always`` / ``never``；
    不写值等价于 ``always``。另识别业界通行的 ``NO_COLOR`` 与 ``CLICOLOR_FORCE``
    环境变量（``--color=always`` 优先于 ``NO_COLOR``，见 ``bili_sub_archive.ui.resolve_color``）。
    ``--no-progress``：关掉实时进度（``--quiet`` 也视为关闭）。
    """
    default = argparse.SUPPRESS if suppress_defaults else "auto"
    progress_default = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument("--color", nargs="?", const="always", default=default,
                        choices=["auto", "always", "never"],
                        help="终端配色：auto（默认）/ always / never（不写值 = always）")
    parser.add_argument("--no-progress", action="store_true", default=progress_default,
                        help="不显示实时进度（重定向或非交互终端下本来就不显示）")


def add_override_flags(parser: argparse.ArgumentParser) -> None:
    """配置覆盖开关：``sync`` 与 ``retry`` 共用。

    这一组开关从 ``sync`` 抽出来给两者共用：``retry`` 的语义就是"补做被配置变更影响的步骤"，
    因此必须能在命令行上表达这些变更 —— 例如装好 mermaid-cli 后
    ``retry --steps mindmap --mmdc <路径>``、开启本地转写后
    ``retry --steps transcript --asr``。此前这些开关只挂在 ``sync`` 上，
    文档里给出的 ``retry --mmdc`` / ``retry --asr`` 实际会报 unrecognized arguments。
    """
    parser.add_argument("--no-images", dest="download_images", action="store_false", default=None,
                        help="不下载动态/专栏配图原图")
    parser.add_argument("--no-media", dest="download_media", action="store_false", default=None,
                        help="不下载视频媒体文件（只归档元数据与文字来源）")
    parser.add_argument("--no-render", dest="render_dynamic_png", action="store_false", default=None,
                        help="不生成动态单张长 PNG")
    parser.add_argument("--asr", dest="asr_enabled", action="store_true", default=None,
                        help="开启本地语音转写（仅对无平台字幕的分 P 执行；不调用云端识别）")
    parser.add_argument("--no-subtitle", dest="prefer_subtitle", action="store_false", default=None,
                        help="不使用平台现成字幕（只走本地 ASR，需同时开启 --asr）")
    parser.add_argument("--no-summary", dest="summary_enabled", action="store_false", default=None,
                        help="不生成视频总结（需要配置 OpenAI 兼容接口才会生成）")
    parser.add_argument("--no-mindmap", dest="mindmap_enabled", action="store_false", default=None,
                        help="不生成 Mermaid 思维导图（.mmd 与 PNG）")
    parser.add_argument("--summary-model", dest="summary_model",
                        help="总结用的模型名（默认取 [summary].model / BSA_LLM_MODEL）")
    parser.add_argument("--summary-base-url", dest="summary_base_url",
                        help="OpenAI 兼容接口地址（默认取 [summary].base_url / BSA_LLM_BASE_URL）")
    parser.add_argument("--summary-prompt", dest="summary_prompt_file",
                        help="自定义 prompt 文件（分节 [system]/[map]/[reduce]/[full]）")
    parser.add_argument("--mmdc", dest="mindmap_mmdc_path",
                        help="mermaid-cli（mmdc）路径：留空则从 PATH 查找")
    parser.add_argument("--mindmap-style", dest="mindmap_style",
                        help="导图渲染样式：paper（默认，浅色卡片）/ pastel / dark / classic")
    parser.add_argument("--video-workers", type=int, help="同时下载的视频数（默认 3）")
    parser.add_argument("--segment-workers", type=int, help="单视频内分片并发数（默认 4）")
    parser.add_argument("--quality", dest="video_quality",
                        help="清晰度上限：360p/480p/720p/1080p/1440p/4k/best（默认 1080p）")
    parser.add_argument("--asr-model", dest="asr_model", help="本地 ASR 模型名（默认 small）")
    parser.add_argument("--asr-language", dest="asr_language", help="ASR 识别语言（默认 zh，空串=自动）")
    parser.add_argument("--interval", dest="interval_seconds", type=float,
                        help="请求间隔秒数（风控敏感，默认 1.2）")
    parser.add_argument("--max-pages", type=int, help="每类列表最多翻页数（默认 40）")


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--uid", type=int, help="目标 UP 主 UID（默认取配置/require.txt）")
    parser.add_argument("--out", dest="output_dir", help="输出目录")
    add_global_aliases(parser)


def add_global_aliases(parser: argparse.ArgumentParser) -> None:
    """把全局开关（``--config`` / ``--json`` / ``-v`` / ``--quiet``）也挂到子命令上。

    argparse 的子解析器会把自己的默认值**复制回**主命名空间（``_SubParsersAction``
    解析到独立 namespace 后整体 ``setattr``），所以这里统一用
    ``default=argparse.SUPPRESS``：只有用户真的写了这个开关才覆盖主解析器的值。
    这样 ``bsa --json sync`` 与 ``bsa sync --json`` 都能用，
    而不是"写在子命令后面就报 unrecognized arguments"。
    """
    parser.add_argument("--config", default=argparse.SUPPRESS,
                        help="指定配置文件（等价于写在子命令之前）")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="运行摘要以 JSON 输出到 stdout")
    parser.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                        help="输出调试日志")
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="只输出警告与错误")
    add_ui_flags(parser, suppress_defaults=True)


def _overrides(args) -> dict:
    return {
        "uid": getattr(args, "uid", None),
        "output_dir": getattr(args, "output_dir", None),
        "date_from": parse_date(args.date_from) if getattr(args, "date_from", None) else None,
        "date_to": parse_date(args.date_to) if getattr(args, "date_to", None) else None,
        "latest": getattr(args, "latest", None),
        "kinds": [k.strip() for k in args.types.split(",")] if getattr(args, "types", None) else None,
        "download_images": getattr(args, "download_images", None),
        "download_media": getattr(args, "download_media", None),
        "render_dynamic_png": getattr(args, "render_dynamic_png", None),
        "asr_enabled": getattr(args, "asr_enabled", None),
        "prefer_subtitle": getattr(args, "prefer_subtitle", None),
        "video_workers": getattr(args, "video_workers", None),
        "segment_workers": getattr(args, "segment_workers", None),
        "video_quality": getattr(args, "video_quality", None),
        "asr_model": getattr(args, "asr_model", None),
        "asr_language": getattr(args, "asr_language", None),
        "summary_enabled": getattr(args, "summary_enabled", None),
        "mindmap_enabled": getattr(args, "mindmap_enabled", None),
        "summary_model": getattr(args, "summary_model", None),
        "summary_base_url": getattr(args, "summary_base_url", None),
        "summary_prompt_file": getattr(args, "summary_prompt_file", None),
        "mindmap_mmdc_path": getattr(args, "mindmap_mmdc_path", None),
        "mindmap_style": getattr(args, "mindmap_style", None),
        "interval_seconds": getattr(args, "interval_seconds", None),
        "max_pages": getattr(args, "max_pages", None),
    }


def _steps_from(args) -> set[str] | None:
    raw = getattr(args, "steps", None)
    if not raw:
        return None
    steps = {s.strip() for s in str(raw).split(",") if s.strip()}
    valid = {name for names in STEPS_BY_KIND.values() for name in names}
    unknown = steps - valid
    if unknown:
        raise ConfigError(f"未知步骤：{', '.join(sorted(unknown))}（可选 {', '.join(sorted(valid))}）")
    return steps


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def print_summary(summary: RunSummary, stream=None, ui=None) -> None:
    """运行摘要（人类可读版）。信息集与改造前一致，只是换了版式与配色。

    ``ui`` 由调用方注入（``main`` 复用同一个 Ui，避免重复探测终端能力）；
    不传时按 ``stream`` 自建，保持"print_summary(summary)"这类既有调用可用。
    """
    counts = summary.counts or {}
    order = ["done", "partial", "skipped", "denied", "invisible", "not_found", "failed"]
    label = {"done": "成功", "partial": "部分完成", "skipped": "跳过",
             "denied": "权限不足", "invisible": "不可见(62002)", "not_found": "不存在",
             "failed": "失败"}
    ui = ui or Ui(stream if stream is not None else sys.stdout)

    ui.blank()
    ui.rule(f"运行摘要 {summary.run_id}（{summary.mode}）")
    ui.kv([
        ("UP", f"{summary.author}（UID {summary.uid}）"),
        ("目录", str(summary.author_dir)),
        ("筛选", f"日期 {summary.date_from or '不限'} ~ {summary.date_to or '不限'}；"
                 f"最新 N = {summary.latest if summary.latest else '不限'}"),
        ("发现/选中", f"发现 {summary.discovered} 条 → 选中 {summary.selected} 条"),
    ])
    pairs = [(label.get(key, key), counts.get(key, 0), OUTCOME_TONES.get(key, ""))
             for key in order if counts.get(key)]
    if pairs:
        ui.badges(pairs)
    else:
        ui.line("处理结果：无（本次没有处理任何条目）", "dim")

    requests = (f"请求 {summary.http_requests} 次（重试 {summary.http_retries}，"
                f"风控 {summary.risk_events}），下载 {summary.bytes_downloaded / 1024:.0f} KiB")
    if summary.media_bytes:
        requests += f"（另媒体 {summary.media_bytes / 1024 / 1024:.1f} MiB）"
    ui.line(requests, "dim")

    if summary.results:
        rows: list[list] = []
        tones: list[str] = []
        cell_tones: list[list[str]] = []
        for result in summary.results:
            tone = OUTCOME_TONES.get(result.outcome, "")
            rows.append([ui.flag(result.outcome), result.kind, result.title,
                         result.outcome, result.message])
            tones.append(tone)
            cell_tones.append([tone, "", "", tone, "dim"])
        ui.blank()
        ui.line("条目：", "title")
        ui.table(["状态", "类型", "标题", "结果", "说明"], rows,
                 tones=tones, cell_tones=cell_tones)

    if summary.warnings:
        ui.blank()
        ui.panel("警告", list(summary.warnings), "warn")
    if summary.notes:
        ui.blank()
        ui.panel("说明", list(summary.notes[:40]), "dim")
    ui.rule()


def print_check_report(report: dict, stream=None, ui=None) -> None:
    """``check`` 报告：每段一个小标题 + 键值表。

    键值表**不截断**（``ui.kv``）：环境检查里的长文本（安装命令、配置摘要）
    必须完整可见，截断会丢信息。
    """
    ui = ui or Ui(stream if stream is not None else sys.stdout)
    ui.blank()
    ui.rule("环境与配置检查")
    for section, rows in report.items():
        ui.line(f"[{section}]", "title")
        ui.kv([(str(key), str(value)) for key, value in rows], indent=2)
    ui.rule()


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def run_check(args, loaded: LoadedConfig, logger, ui=None) -> int:
    report: dict[str, list[tuple[str, str]]] = {}
    cfg = loaded.config
    creds = loaded.credentials
    report["运行环境"] = [
        ("Python", sys.version.split()[0]),
        ("bili-sub-archive", __version__),
        ("功能面", "发现与归档 + 媒体与本地文字 + 总结与思维导图"),
    ]
    files = ", ".join(str(p) for p in cfg.config_files) or "（无，使用默认值）"
    report["配置"] = [
        ("配置文件", files),
        ("目标 UID", str(cfg.uid or "未设置")),
        ("输出目录", str(cfg.output_dir)),
        ("内容类型", cfg.kind_labels),
        ("筛选", cfg.describe_filter()),
        ("请求间隔", f"{cfg.interval_seconds}s（重试 {cfg.retries}，超时 {cfg.timeout_seconds}s）"),
        ("配图下载", "开启" if cfg.download_images else "关闭"),
        ("视频媒体", (f"开启（{cfg.video_quality}，视频并发 {cfg.video_workers}，"
                       f"分片并发 {cfg.segment_workers}）")
         if cfg.download_media else "关闭"),
        ("平台字幕", "优先使用" if cfg.prefer_subtitle else "关闭（只走本地 ASR）"),
        ("本地 ASR", (f"开启（模型 {cfg.asr_model}，{cfg.asr_device}/{cfg.asr_compute_type}，"
                       f"语言 {cfg.asr_language or '自动'}）")
         if cfg.asr_enabled else "关闭（无字幕的分 P 不产生文字稿）"),
        ("动态长图", (f"开启（宽 {cfg.render_width}，高度上限 {cfg.render_max_height}）"
                       if cfg.render_dynamic_png else "关闭")),
        ("总结（LLM）", _summary_state(cfg)),
        ("思维导图", _mindmap_state(cfg)),
    ]
    if cfg.warnings:
        # 配置告警（未知段/键、间隔偏小…）在这里显式列出：配错了必须看得见
        report["配置告警"] = [(f"{i + 1}", w.strip()) for i, w in enumerate(cfg.warnings)]
    report["凭据"] = [
        ("Cookie 来源", creds.source),
        ("Cookie 字段", ", ".join(creds.cookie_field_names[:8]) or "无（值为空）"),
        ("LLM 密钥", (f"已配置（来源 {creds.llm_key_source}）" if creds.has_llm_key else
                      "未配置（填 [summary] api_key 或设 BSA_LLM_API_KEY；本地端点可匿名）")),
    ]
    status = 0
    client = HttpClient(
        creds.cookie, interval=cfg.interval_seconds, timeout=cfg.timeout_seconds,
        retries=cfg.retries, user_agent=cfg.user_agent, logger=logger,
    )
    api = BilibiliApi(client, logger=logger)
    if not creds.has_cookie:
        report["登录态"] = [("状态", "缺少 Cookie（sync 无法运行）")]
        status = 3
    else:
        logged, mid, note = api.login_state()
        report["登录态"] = [
            ("状态", note),
            ("当前账号 mid", str(mid or "-")),
        ]
        if not logged:
            status = 3
    if cfg.uid:
        author, result = api.author_info(cfg.uid)
        if author is None:
            report["作者"] = [("状态", f"读取失败：{result.kind_label} {result.message}")]
            status = status or exit_code_for_kind(result.error_kind)
        else:
            report["作者"] = [
                ("名称", author.name),
                ("mid", str(author.mid)),
                ("粉丝", str(author.fans)),
                ("头像", "可读" if author.face else "缺失"),
            ]
    writable, writable_note = _check_writable(cfg.output_dir)
    report["输出目录"] = [("可写", writable_note)]
    if not writable:
        status = status or 2
    report["可选依赖"] = _dependency_report(cfg)
    if args.scan_output:
        from .redact import Redactor, scan_for_secrets

        redactor = Redactor.from_cookie(creds.cookie, [creds.llm_api_key])
        hits = scan_for_secrets(cfg.output_dir, redactor)
        report["产物凭据自检"] = [
            ("扫描目录", str(cfg.output_dir)),
            ("结果", "未发现凭据值 ✔" if not hits else f"发现 {len(hits)} 处凭据泄露 ✘"),
        ]
        if hits:
            status = status or 1
    if args.json:
        print(json.dumps({k: dict(v) for k, v in report.items()}, ensure_ascii=False, indent=2))
    else:
        print_check_report(report, ui=ui)
    return status


def _summary_state(cfg) -> str:
    """总结配置的一句话状态。"""
    if not cfg.summary_enabled:
        return "关闭（--no-summary / [summary].enabled=false）"
    if not cfg.summary_configured:
        return ("未配置（需 [summary] base_url + model，或 BSA_LLM_BASE_URL/MODEL）："
                "有文字稿的视频会记 skipped(llm_not_configured)")
    prompt = "内置 prompt"
    if cfg.summary_prompt_file:
        try:
            from .summarize.prompt import load_prompt_spec

            prompt = load_prompt_spec(cfg.summary_prompt_file).describe()
        except Exception as exc:  # noqa: BLE001 - 报告用，不掩盖真正的问题
            prompt = f"自定义 prompt 不可用（{exc}）"
    return (f"开启（{cfg.summary_model} @ {cfg.summary_base_url}，客户端 {cfg.summary_client}，"
            f"{cfg.summary_chunk_chars} 字/块，最多 {cfg.summary_max_chunks} 块；{prompt}）")


def _mindmap_state(cfg) -> str:
    """导图配置的一句话状态。"""
    if not cfg.mindmap_enabled:
        return "关闭（--no-mindmap / [mindmap].enabled=false）"
    from .summarize.mermaid_cli import probe_mmdc
    from .summarize.mindmap_style import STYLE_LABELS, build_style, normalize_style

    mmdc = probe_mmdc(cfg)
    style = normalize_style(getattr(cfg, "mindmap_style", ""))
    style_text = f"样式 {style}（{STYLE_LABELS.get(style, style)}）"
    size = int(getattr(cfg, "mindmap_font_size", 0) or 0)
    if size <= 0:
        size = build_style(style).config.get("themeVariables", {}).get("fontSize", "")
        style_text += f" / {size}" if size else ""
    base = (f"开启（节点上限 {cfg.mindmap_max_nodes} / 深度 {cfg.mindmap_max_depth} / "
            f"宽 {cfg.mindmap_width} / {style_text}）")
    if mmdc:
        return f"{base}；mmdc：{mmdc}"
    return (f"{base}；未找到 mmdc：mindmap.mmd 照常生成，PNG 记 failed(dependency_missing)"
            "（npm install -g @mermaid-js/mermaid-cli）")


def _check_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".bili_sub_archive_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, f"{path}（可写）"
    except OSError as exc:
        return False, f"{path}（不可写：{exc}）"


def _dependency_report(cfg=None) -> list[tuple[str, str]]:
    """可选依赖的可用性（``deps.probe_dependencies`` 统一出口）。"""
    from .deps import probe_dependencies

    rows: list[tuple[str, str]] = []
    for dep in probe_dependencies(cfg):
        detail = f"{dep.state}：{dep.detail}"
        if not dep.available:
            detail += f"；安装：{dep.hint}"
        rows.append((dep.label, f"{detail}（{dep.purpose}）"))
    try:
        from .render import available_fonts

        fonts = available_fonts()
        rows.append(("中文字体", f"{len(fonts)} 个候选，首选 {fonts[0]}" if fonts
                     else "未找到中文字体（动态长图中文可能显示为方块）"))
    except Exception as exc:  # noqa: BLE001 - 字体探测失败不该让 check 崩掉
        rows.append(("中文字体", f"探测失败：{type(exc).__name__}: {exc}"))
    return rows


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
#: 旧命令名（pyproject 里保留为弃用别名一个版本），命中时在 stderr 提示改名。
_LEGACY_SCRIPT_NAMES = frozenset({"subvideo", "subvideo.exe"})


def _warn_legacy_script_name() -> None:
    """旧脚本名 ``subvideo`` 仍能用，但提醒主命令已改为 ``bsa``。"""
    if Path(sys.argv[0]).name.lower() in _LEGACY_SCRIPT_NAMES:
        print("提示：命令 subvideo 已改名为 bsa（bili-sub-archive），该别名下个版本移除。",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # 必须在 parse_args 之前：帮助文本/参数错误也走 stdout/stderr，
    # 重定向时同样要是 UTF-8（否则中文帮助在 cp936 下写出去会乱码/报错）
    configure_stdio()
    _warn_legacy_script_name()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        loaded = load_config(root=Path.cwd(), config_file=args.config, overrides=_overrides(args))
    except BiliSubArchiveError as exc:
        setup_logger(args.verbose, args.quiet).error(f"配置错误：{exc}")
        return exc.exit_code
    # 两个流各建一个 Ui：摘要走 stdout（重定向时自动降级为纯文本，不挂进度），
    # 日志与实时进度走 stderr（`--json | Out-File` 时 stdout 保持纯 JSON）
    ui_out = Ui.from_args(args, stream=sys.stdout, progress=False)
    ui_err = Ui.from_args(args, stream=sys.stderr)
    logger = setup_logger(args.verbose, args.quiet,
                          redactor=_redactor_for(loaded), ui=ui_err)
    adapter = LogAdapter(logger)
    try:
        if args.command == "check":
            return run_check(args, loaded, adapter, ui_out)
        options = RunOptions(
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            steps=_steps_from(args),
        )
        runner = Runner(loaded, logger=adapter, root=Path.cwd(), ui=ui_err)
        summary = runner.retry(options) if args.command == "retry" else runner.sync(options)
        # 先收进度再输出结果：rich 的 Live.stop() 会补渲染最后一帧，
        # 若等 finally 才收，那一帧会印在运行摘要**后面**
        ui_err.close()
        if args.json:
            print(json.dumps(summary.to_json(), ensure_ascii=False, indent=2))
        else:
            print_summary(summary, ui=ui_out)
        return summary.exit_code
    except KeyboardInterrupt:
        logger.warning("已中断：已完成的产物保留，可执行 retry 补做未完成步骤")
        return 130
    except BiliSubArchiveError as exc:
        for line in str(exc).splitlines():
            logger.error(f"错误：{line}")
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - 兜底，保证退出码可预期
        logger.error(f"未预期的错误：{type(exc).__name__}: {exc}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return EXIT_UNEXPECTED
    finally:
        # 中断/异常路径也必须收尾，否则 rich 的进度条会把终端光标留在隐藏状态
        ui_err.close()
        ui_out.close()


def _redactor_for(loaded: LoadedConfig):
    from .redact import Redactor

    return Redactor.from_cookie(loaded.credentials.cookie, [loaded.credentials.llm_api_key])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
