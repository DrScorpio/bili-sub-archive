"""运行编排：``sync`` / ``retry`` 两条主流程。

数据流（计划第 3 节）：

```text
CLI / 配置校验 → 登录态检查 → BilibiliApi 三类分页发现
  → 时间范围过滤 + 全局发布时间排序 + 合计最新 N 条
  → 按 (类型, 平台 ID) 建立/查找条目目录与状态
  → 动态/专栏: 正文 + 原图 → content.md / article.md
  → 视频: 元数据 + 分 P 清点 + 充电权限探针（阶段 1 到此为止）
  → 更新 index.json → 输出运行摘要
```
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from .archive import ArchiveContext, ArchiveResult
from .archive.article import archive_article
from .archive.dynamic import archive_dynamic
from .archive.video import archive_video
from .bili.api import BilibiliApi
from .bili.client import HttpClient
from .config import LoadedConfig
from .credentials import Credentials
from .discover import Discoverer, items_from_entries
from .errors import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_PARTIAL,
    KIND_AUTH,
    KIND_RISK,
    ConfigError,
    CredentialError,
    OutputError,
    RiskControlStop,
    describe_kind,
    exit_code_for_kind,
)
from .filters import select
from .models import (
    KIND_ARTICLE,
    KIND_DYNAMIC,
    KIND_VIDEO,
    Item,
    RunSummary,
    EntryResult,
    stale_skip_map,
)
from .paths import DirLock, resolve_author_dir
from .store import EntryState, Store
from .timeutil import now_iso
from .ui import NULL_TASK

#: 连续风控次数达到该阈值即停止本次运行（契约 5：不绕过、提示人工处理）
RISK_STOP_THRESHOLD = 3

ARCHIVERS = {
    KIND_DYNAMIC: archive_dynamic,
    KIND_VIDEO: archive_video,
    KIND_ARTICLE: archive_article,
}


@dataclass
class RunOptions:
    force: bool = False
    dry_run: bool = False
    steps: set[str] | None = None


class Runner:
    def __init__(self, loaded: LoadedConfig, logger=None, root: Path | None = None,
                 api=None, downloader=None, transcriber=None, ffmpeg_runner=None,
                 chat_client=None, mmdc_runner=None, ui=None):
        self.loaded = loaded
        self.config = loaded.config
        self.credentials: Credentials = loaded.credentials
        self.logger = logger
        #: 终端呈现层（进度条）：不传 = 无进度（离线测试与重定向场景）
        self.ui = ui
        self.root = Path(root or Path.cwd())
        self._api = api
        #: 阶段 2 的可注入依赖（离线测试注入替身；None = 用真实实现）
        self.downloader = downloader
        self.transcriber = transcriber
        self.ffmpeg_runner = ffmpeg_runner
        #: 阶段 3 的可注入依赖：总结 Chat 客户端 + mmdc 命令执行器
        self.chat_client = chat_client
        self.mmdc_runner = mmdc_runner
        self.cache: dict = {}

    # ---------------- 装配 ---------------- #
    @property
    def api(self):
        if self._api is None:
            client = HttpClient(
                self.credentials.cookie,
                interval=self.config.interval_seconds,
                timeout=self.config.timeout_seconds,
                retries=self.config.retries,
                user_agent=self.config.user_agent,
                logger=self.logger,
            )
            self._api = BilibiliApi(client, logger=self.logger)
        return self._api

    def _client(self):
        api = self.api
        return getattr(api, "client", None)

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)

    def _progress_task(self, total, label: str):
        """条目级进度任务；没有 ui（或非 TTY）时返回 no-op 句柄。"""
        if self.ui is None:
            return NULL_TASK
        return self.ui.task(total=total, label=label)

    def _stale_reasons(self) -> dict[str, set[str]]:
        """"应重做"的 skipped 步骤与原因：阶段推进 + 本次配置开关（见 models.stale_skip_map）。"""
        return stale_skip_map(self.config)

    def _adopt_config_warnings(self, summary: RunSummary) -> None:
        """把配置加载期的告警（未知段/键、间隔偏小等）带进运行摘要，否则只存在于内存里。"""
        for warning in self.config.warnings:
            if warning not in summary.warnings:
                summary.warnings.append(warning)
                self._log("warning", f"[配置] {warning}")

    # ---------------- 输入指纹（阶段 3） ---------------- #
    def _signature_steps(self, store: Store, entry) -> set[str]:
        """已 ``done`` 但**输入指纹已过时**的步骤（计划 3.2：变更 prompt/模型只重做总结/导图）。

        步骤状态本身看不出"换了模型或 prompt"，必须比对产物里记录的指纹与当前配置
        算出来的指纹；不一致就把该步骤重新纳入本次运行，其余步骤（下载、字幕）不动。
        """
        from .summarize import (
            SummaryPlan,
            mindmap_signature,
            summary_signature,
        )
        from .summarize.prompt import PromptError, load_prompt_spec

        if entry.kind != KIND_VIDEO:
            return set()
        previous = SummaryPlan.from_json(entry.extra.get("summary") or {})
        if previous.status != "done" or not previous.signature:
            return set()
        if not self.config.summary_enabled or not self.config.summary_configured:
            return set()

        entry_dir = Path(store.author_dir) / entry.dir
        rel_txt = str((entry.extra.get("transcript") or {}).get("transcript_txt")
                      or "transcript.txt")
        try:
            text = (entry_dir / rel_txt).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return set()
        if not text:
            return set()
        try:
            spec = load_prompt_spec(self.config.summary_prompt_file or None, base=entry_dir)
        except PromptError:
            return {"summary", "mindmap"}      # prompt 坏了 → 让它去记失败，别静默沿用

        out: set[str] = set()
        if summary_signature(self.config, spec, text) != previous.signature:
            out.add("summary")
        if (self.config.mindmap_enabled
                and mindmap_signature(self.config, previous.signature)
                != previous.mindmap_signature):
            out.add("mindmap")
        return out

    # ---------------- sync ---------------- #
    def sync(self, options: RunOptions | None = None) -> RunSummary:
        options = options or RunOptions()
        cfg = self.config
        summary = RunSummary(
            run_id=uuid.uuid4().hex[:8],
            mode="sync",
            uid=cfg.uid,
            output_dir=str(cfg.output_dir),
            kinds=cfg.enabled_kinds,
            date_from=cfg.date_from.isoformat() if cfg.date_from else "",
            date_to=cfg.date_to.isoformat() if cfg.date_to else "",
            latest=cfg.latest,
        )
        self._adopt_config_warnings(summary)
        if not self.credentials.has_cookie:
            raise CredentialError(
                "缺少 Cookie：三类列表接口都需要登录态（匿名访问 space 类接口不稳定，"
                "阶段 0 第 3.3.1 节）。请设置环境变量 BSA_COOKIE 或写入 config.local.toml"
            )
        api = self.api
        logged, mid, note = api.login_state()
        if not logged:
            raise CredentialError(f"登录态检查失败：{note}")
        self._log("info", f"[登录] {note}（当前账号 mid={mid}）")

        author, card_result = api.author_info(cfg.uid)
        if author is None:
            message = (f"无法读取 UID {cfg.uid} 的作者信息："
                       f"{card_result.kind_label} {card_result.message}")
            if exit_code_for_kind(card_result.error_kind) == EXIT_CONFIG:
                raise ConfigError(f"{message}；请确认 UID 是否正确")
            raise CredentialError(message)
        summary.author = author.name
        self._log("info", f"[作者] {author.name}（mid={author.mid}，粉丝 {author.fans}）")

        author_dir, warnings = resolve_author_dir(cfg.output_dir, cfg.uid, author.name)
        summary.author_dir = author_dir.name
        summary.warnings.extend(warnings)
        for warning in warnings:
            self._log("warning", f"[警告] {warning}")

        with DirLock(author_dir, cfg.lock_stale_hours, logger=self.logger):
            store = Store(author_dir, cfg.uid, author.name, author.mid,
                          logger=self.logger, redactor=self._redactor())
            store.load()
            if store.notes:
                summary.notes.extend(store.notes)

            discovery = Discoverer(api, cfg, self.logger, self._redactor(),
                                   detail_cache=self.cache).run(cfg.uid, author.name)
            summary.scans = discovery.stats
            summary.discovered = discovery.discovered
            summary.notes.extend(discovery.notes)

            selection = select(discovery.items, cfg.date_from, cfg.date_to, cfg.latest)
            summary.selected = len(selection.items)
            summary.notes.append(
                f"候选 {selection.candidates} 条（日期范围内），已选 {len(selection.items)} 条；"
                f"范围外 {selection.out_of_range} 条"
            )
            if selection.unknown_time_count:
                summary.warnings.append(
                    f"{selection.unknown_time_count} 条内容的发布时间未取到（详情请求失败），"
                    "未计入 N 候选；重跑可补做"
                )

            fatal_kind = discovery.fatal_kind
            if not options.dry_run and not fatal_kind:
                fatal_kind = self._archive_selected(store, selection.items, summary, options)
            elif options.dry_run:
                for item in selection.items:
                    summary.results.append(
                        EntryResult(
                            kind=item.kind, platform_id=item.platform_id, dir="",
                            title=item.title, published_at=item.published_at,
                            outcome="skipped", message="dry-run：仅发现与筛选，未落盘",
                        )
                    )
                    summary.bump("skipped")

            summary.exit_code = EXIT_OK
            self._finalize(store, summary)
            if fatal_kind:
                self._raise_fatal(fatal_kind, discovery.fatal_message or summary.warnings[-1:])
        return summary

    # ---------------- retry ---------------- #
    def retry(self, options: RunOptions | None = None) -> RunSummary:
        options = options or RunOptions()
        cfg = self.config
        summary = RunSummary(
            run_id=uuid.uuid4().hex[:8],
            mode="retry",
            uid=cfg.uid,
            output_dir=str(cfg.output_dir),
            kinds=cfg.enabled_kinds,
        )
        self._adopt_config_warnings(summary)
        if not self.credentials.has_cookie:
            raise CredentialError("缺少 Cookie：retry 需要重新取详情，请提供登录态")
        from .paths import find_existing_author_dir

        author_dir, warnings = find_existing_author_dir(cfg.output_dir, cfg.uid)
        if author_dir is None:
            raise ConfigError(
                f"输出目录 {cfg.output_dir} 下找不到 UID {cfg.uid} 的作者目录，无法 retry"
            )
        summary.author_dir = author_dir.name
        summary.warnings.extend(warnings)
        api = self.api
        logged, mid, note = api.login_state()
        if not logged:
            raise CredentialError(f"登录态检查失败：{note}")
        author, _card = api.author_info(cfg.uid)
        author_name = author.name if author else author_dir.name
        summary.author = author_name

        with DirLock(author_dir, cfg.lock_stale_hours, logger=self.logger):
            store = Store(author_dir, cfg.uid, author_name,
                          author.mid if author else 0, logger=self.logger,
                          redactor=self._redactor())
            store.load()
            summary.notes.extend(store.notes)
            entries = store.entries_with_pending(options.steps, self._stale_reasons(),
                                                 lambda state: self._signature_steps(store, state))
            if not entries:
                summary.notes.append("没有需要补做的条目（所有步骤已 done/skipped）")
            summary.selected = len(entries)
            fatal_kind = ""
            if entries:
                items = items_from_entries(entries)
                fatal_kind = self._archive_selected(store, items, summary, options)
            summary.exit_code = EXIT_OK
            self._finalize(store, summary)
            if fatal_kind:
                self._raise_fatal(fatal_kind, "补做过程中遇到风控/登录态问题")
        return summary

    # ---------------- 归档循环 ---------------- #
    def _archive_selected(self, store: Store, items: list[Item], summary: RunSummary,
                          options: RunOptions) -> str:
        """返回致命错误分类（空串表示正常结束）。"""
        ctx = ArchiveContext(api=self.api, store=store, config=self.config,
                             logger=self.logger, redactor=self._redactor(),
                             detail_cache=self.cache, cookie=self.credentials.cookie,
                             llm_api_key=self.credentials.llm_api_key,
                             downloader=self.downloader, transcriber=self.transcriber,
                             ffmpeg_runner=self.ffmpeg_runner,
                             chat_client=self.chat_client, mmdc_runner=self.mmdc_runner,
                             force=bool(options.force), ui=self.ui)
        fatal = ""
        task = self._progress_task(len(items), f"归档条目 0/{len(items)}")
        for idx, item in enumerate(items, 1):
            client = self._client()
            if client is not None and client.consecutive_risk >= RISK_STOP_THRESHOLD:
                fatal = KIND_RISK
                summary.warnings.append(
                    f"连续 {client.consecutive_risk} 次风控失败，停止后续 {len(items) - idx + 1} 条"
                    "（不绕过风控：请降低频率或稍后重试）"
                )
                break
            task.update(description=f"归档条目 {idx}/{len(items)}")
            entry = store.get(item.kind, item.platform_id)
            try:
                if (entry is not None and not options.force
                        and not store.pending_steps(entry, options.steps, self._stale_reasons(),
                                                    lambda state: self._signature_steps(store, state))):
                    summary.results.append(
                        EntryResult(
                            kind=item.kind, platform_id=item.platform_id, dir=entry.dir,
                            title=entry.title or item.title, published_at=item.published_at,
                            outcome="skipped", message="重跑：该条目所有步骤已完成，跳过",
                        )
                    )
                    summary.bump("skipped")
                    self._log("info", f"[{idx}/{len(items)}] {item.kind} {item.platform_id} 已完成，跳过")
                    continue
                try:
                    if entry is None:
                        entry = store.allocate(item)
                    self._log("info",
                              f"[{idx}/{len(items)}] 归档 {item.kind} {item.platform_id}：{item.title[:40]}")
                    result = self._run_archiver(ctx, item, entry)
                except OutputError as exc:
                    summary.results.append(
                        EntryResult(kind=item.kind, platform_id=item.platform_id,
                                    dir=entry.dir if entry else "", title=item.title,
                                    published_at=item.published_at, outcome="failed",
                                    error_kind="output_error", message=self._redact(str(exc)))
                    )
                    summary.bump("failed")
                    summary.warnings.append(f"落盘失败：{self._redact(str(exc))}")
                    continue

                entry_result = EntryResult(
                    kind=item.kind, platform_id=item.platform_id, dir=entry.dir,
                    title=entry.title or item.title, published_at=item.published_at,
                    outcome=result.outcome, error_kind=result.error_kind,
                    error_code=result.error_code, message=self._redact(result.message),
                    artifacts=list(result.artifacts),
                )
                summary.results.append(entry_result)
                summary.bump(result.outcome)
                summary.media_bytes += int(getattr(result, "media_bytes", 0) or 0)
                for line in result.notes:
                    summary.notes.append(f"{item.kind} {item.platform_id}：{self._redact(line)}")
                level = "info" if result.outcome in ("done", "skipped") else "warning"
                self._log(level, f"    → {result.outcome}：{result.message}")
                store.write_metadata(entry)
            finally:
                # 中断（KeyboardInterrupt）也要把这一格补上，进度条才不停在半路
                task.advance()
        task.done(description=f"归档条目 {len(items)}/{len(items)}")
        return fatal

    def _run_archiver(self, ctx: ArchiveContext, item: Item, entry: EntryState) -> ArchiveResult:
        archiver = ARCHIVERS.get(item.kind)
        if archiver is None:
            return ArchiveResult(outcome="failed", error_kind="bad_request",
                                 message=f"未知内容类型 {item.kind}")
        return archiver(ctx, item, entry)

    # ---------------- 收尾 ---------------- #
    def _finalize(self, store: Store, summary: RunSummary) -> None:
        summary.finished_at = now_iso()
        client = self._client()
        if client is not None:
            summary.http_requests = client.requests
            summary.http_retries = client.retries_used
            summary.bytes_downloaded = client.bytes_downloaded
            summary.risk_events = client.risk_events
        failures = sum(summary.counts.get(k, 0) for k in
                       ("failed", "invisible", "denied", "not_found", "partial"))
        summary.exit_code = EXIT_PARTIAL if failures else EXIT_OK
        summary.notes.extend(self._capability_notes())
        store.save_index(last_run=summary.to_json())
        store.write_run_summary(summary.to_json())
        stats = store.stats()
        summary.notes.append(
            f"作者目录条目总数 {stats['entries']}（{', '.join(f'{k}={v}' for k, v in stats['by_status'].items())}）"
        )

    def _capability_notes(self) -> list[str]:
        """如实报告本次运行"能做什么/不能做什么"（缺依赖不静默）。"""
        from .deps import missing, probe_dependencies

        notes: list[str] = []
        gaps = missing(probe_dependencies(self.config))
        if gaps:
            notes.append(
                "缺失依赖（对应步骤会记 failed(dependency_missing)，装好后 retry 可补做）："
                + "、".join(f"{d.label}（{d.hint}）" for d in gaps)
            )
        if not self.config.download_media:
            notes.append("媒体下载已关闭（--no-media / [download].media=false）：视频只归档元数据与文字")
        if self.config.asr_enabled:
            notes.append(
                f"本地 ASR 已开启（模型 {self.config.asr_model}/{self.config.asr_device}）："
                "仅对无平台字幕的分 P 执行，不调用任何云端识别服务"
            )
        else:
            notes.append("本地 ASR 关闭：无平台字幕的分 P 不产生文字稿（开启 --asr 后可 retry 补做）")
        notes.extend(self._stage3_notes())
        return notes

    def _stage3_notes(self) -> list[str]:
        """如实说明总结/导图这次能不能做（阶段 3）。"""
        from .deps import by_key, probe_dependencies
        from .summarize.mermaid_cli import MMDC_HINT

        cfg = self.config
        notes: list[str] = []
        if not cfg.summary_enabled:
            notes.append("总结已关闭（--no-summary / [summary].enabled=false）：summary/mindmap 记 skipped")
        elif not cfg.summary_configured:
            notes.append(
                "未配置 LLM（[summary] base_url 与 model，或 BSA_LLM_BASE_URL / "
                "BSA_LLM_MODEL）：有文字稿的视频其 summary/mindmap 记 "
                "skipped(llm_not_configured)，配置好后 retry 可补做"
            )
        else:
            from .summarize.llm import endpoint_host

            key_note = "已配置密钥" if self.credentials.has_llm_key else "未配置密钥（按匿名请求发送）"
            notes.append(
                f"总结走 OpenAI 兼容接口 {endpoint_host(cfg.summary_base_url) or '(未填端点)'}"
                f"，模型 {cfg.summary_model}，客户端 {cfg.summary_client}，{key_note}；"
                "只发送文字稿，不发送 Cookie、媒体文件或原视频地址"
            )
        if not cfg.mindmap_enabled:
            notes.append("思维导图已关闭（--no-mindmap / [mindmap].enabled=false）：mindmap 记 skipped")
        else:
            mmdc = by_key(probe_dependencies(cfg)).get("mmdc")
            if mmdc is not None and not mmdc.available:
                notes.append(
                    "未找到 mermaid-cli（mmdc）：mindmap.mmd 照常生成，mindmap.png 记 "
                    f"failed(dependency_missing)；安装：{MMDC_HINT}"
                )
        return notes

    def _raise_fatal(self, fatal_kind: str, message) -> None:
        if isinstance(message, list):
            message = message[0] if message else ""
        if fatal_kind == KIND_AUTH:
            raise CredentialError(f"登录态失效：{message}")
        raise RiskControlStop(
            f"遇到{describe_kind(fatal_kind)}：{message}；已停止本次运行，"
            "产物已保留，稍后可 retry 补做（不做验证码绕过或账号轮换）"
        )

    def _redact(self, message: str) -> str:
        redactor = self._redactor()
        return redactor.redact(message) if redactor else message

    def _redactor(self):
        from .redact import Redactor

        if not hasattr(self, "_redactor_obj"):
            self._redactor_obj = Redactor.from_cookie(
                self.credentials.cookie, [self.credentials.llm_api_key]
            )
        return self._redactor_obj
