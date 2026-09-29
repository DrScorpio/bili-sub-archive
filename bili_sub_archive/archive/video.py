"""视频归档：元数据 → 媒体下载 → 文字稿 → 总结与思维导图（阶段 2/3 完整实现）。

需求 3.3 的分步落地：

| 步骤 | 内容 | 关键约束 |
| --- | --- | --- |
| ``fetch`` | 标题/简介/BV/分 P/权限探针 | 权限只看媒体流形态，不看 ``code``（阶段 0 3.3.4） |
| ``media`` | 各分 P 下载 + 校验 | 多 P 同目录不拼接；并发可配；续传；校验可播放且非空 |
| ``transcript`` | 字幕优先 → 本地 ASR 兜底 | 关闭 ASR 时不跑模型；无文字不编造 |
| ``summary`` | 分块总结（OpenAI 兼容接口） | 未配置 LLM 记 ``skipped(llm_not_configured)``；指纹一致则复用不重复调用 |
| ``mindmap`` | 大纲 → ``mindmap.mmd`` → ``mindmap.png`` | 渲染失败保留 ``.mmd``；PNG 失败只影响本步骤，可独立重试 |

**权限判定**（阶段 0 第 3.3.4 节，契约第 3 条）：充电视频无权限时 ``code`` 依旧是 0，
只有媒体流形态不同 —— 因此 ``fetch`` 取一次 ``playurl`` 做"访问探针"（不下载任何
媒体），把 ``permission`` 写进 metadata：

- ``dash.video`` 非空 → ``full``（真正片）；
- 只有单段 ``durl`` → ``preview_only``（试看片段，账号无充电权限）；
- 否则 ``unknown``。

**绝不把试看片段当成正片**：``preview_only`` 时 ``media`` 记
``skipped(permission_preview_only)`` 并留错误记录，不调用下载器。

**总结不重复花钱**（阶段 3）：``summary``/``mindmap`` 各自有"输入指纹"
（prompt 指纹 + 模型 + 端点 + 分块参数 + 文字稿摘要），指纹未变且产物存在就复用；
``--force`` 强制重做。
"""

from __future__ import annotations

from pathlib import Path

from ..bili.parse import parse_page_list, parse_video_detail
from ..deps import KIND_DEPENDENCY, executable_path
from ..errors import KIND_DENIED, KIND_PARSE
from ..media import download_media, media_notes
from ..models import STEP_DONE, STEP_FAILED, STEP_SKIPPED
from ..timeutil import fmt_local
from ..transcript import build_transcript
from . import ArchiveContext, ArchiveResult


def archive_video(ctx: ArchiveContext, item, entry) -> ArchiveResult:
    bvid = item.platform_id
    doc, pages, page_source, access, access_note = _fetch(ctx, item, entry, bvid)
    if isinstance(doc, ArchiveResult):        # 取数失败已写好步骤状态
        return doc

    permission = access.mode if access is not None else "unknown"

    # ---- 步骤 4/5：总结与思维导图（阶段 3） ---- #
    media_outcome, media_plan, media_notes_list = _archive_media(
        ctx, item, entry, bvid, pages, permission, access
    )
    transcript_plan = _archive_transcript(ctx, item, entry, bvid, pages)
    stage3_artifacts, stage3_notes, stage3_failed, stage3_plan = _archive_stage3(
        ctx, item, entry, transcript_plan
    )

    # ---- 结果汇总 ---- #
    notes = ([access_note] if access_note else []) + media_notes_list + stage3_notes
    artifacts: list[str] = []
    media_bytes = media_plan.bytes_written if media_plan is not None else 0

    outcome = "done"
    has_text = transcript_plan is not None and transcript_plan.has_text
    if media_outcome == "denied":
        # 正文（媒体）无权限：有文字稿则算部分完成，否则整体记权限不足
        outcome = "partial" if has_text else "denied"
        notes.append("media 未下载（无充电权限，只有试看片段）；未把试看当正片")
    elif media_outcome == "failed":
        # 媒体全失败但有文字稿 → 该条目是"部分完成"（文字已归档，媒体可 retry 补做）
        outcome = "partial" if has_text else "failed"
    elif media_outcome == "partial":
        outcome = "partial"

    if transcript_plan is not None and transcript_plan.has_text:
        artifacts.extend([transcript_plan.transcript_txt, transcript_plan.transcript_srt])
        notes.append(f"文字稿：{transcript_plan.summary()}")
    elif transcript_plan is not None:
        notes.append(f"文字稿缺失：{transcript_plan.message or transcript_plan.reason}")

    if stage3_failed and outcome == "done":
        # 媒体与文字都齐了，只是总结/导图失败：如实记"部分完成"，失败步骤可单独 retry
        outcome = "partial"
    artifacts.extend(stage3_artifacts)

    if media_plan is not None and media_plan.done:
        artifacts.extend([r.name for r in media_plan.records if r.status == "done" and r.name])

    entry.extra["scope_note"] = ("阶段 3 已实现总结（OpenAI 兼容接口）与 Mermaid 思维导图；"
                                 "总结/导图状态见 steps 与 extra.summary")
    entry.extra.pop("stage2_scope", None)
    ctx.store.write_metadata(entry)

    return ArchiveResult(
        outcome=outcome,
        error_kind=KIND_DENIED if outcome == "denied" else "",
        artifacts=artifacts,
        notes=notes,
        media_bytes=media_bytes,
        message=_result_message(bvid, pages, media_plan, transcript_plan, stage3_plan),
    )


# --------------------------------------------------------------------------- #
# summary / mindmap（阶段 3）
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def _fetch(ctx: ArchiveContext, item, entry, bvid: str):
    """取详情/分 P/权限探针；失败时返回 :class:`ArchiveResult`（已写好步骤状态）。"""
    cache_key = ("video", bvid)
    cached = ctx.cache_get(cache_key)
    if cached is None:
        resp = ctx.api.video_detail(bvid)
        if not resp.ok:
            return ctx.problem(entry, "fetch", resp, note="视频详情接口失败，可重跑补做")
        doc = parse_video_detail(resp.data)
        if doc is None:
            ctx.fail_step(entry, "fetch", error_kind=KIND_PARSE,
                          message="view 响应缺少 pages/owner 等字段")
            return ArchiveResult(outcome="failed", error_kind=KIND_PARSE,
                                 message="视频详情结构无法解析")
        pages_resp = ctx.api.page_list(bvid)
        pages = parse_page_list(pages_resp.data) if pages_resp.ok else []
        page_source = "pages(view)"
        if not pages and pages_resp.ok:
            pages = parse_page_list(pages_resp.data)
            page_source = "pagelist"
        elif pages_resp.ok:
            page_source = "pages(view) + pagelist(对照)"
        ctx.cache_put(cache_key, (doc, pages, page_source))
    else:
        doc, pages, page_source = cached

    # ---- 访问探针：只看媒体流形态，不看 code ---- #
    access = None
    access_note = ""
    if not ctx.config.probe_video_access:
        access_note = "已按配置跳过 playurl 访问探针（permission=unknown）"
    elif not pages:
        access_note = "无分 P（cid 缺失），跳过 playurl 访问探针（permission=unknown）"
    else:
        access = ctx.api.access(bvid, pages[0].cid)
        access_note = access.message or ""

    permission = access.mode if access is not None else "unknown"

    missing: list[str] = []
    if doc.desc and not pages:
        missing.append("分 P 列表为空")

    entry.title = doc.title or entry.title
    entry.author = doc.owner_name or entry.author
    entry.flags.update({
        "is_charging_arc": bool(item.extras.get("is_charging_arc")),
        "is_upower_exclusive": bool(doc.is_upower_exclusive),
        "permission": permission,
    })
    entry.source.update({
        "api": "/x/web-interface/view",
        "bvid": bvid,
        "aid": doc.aid,
        "page_source": page_source,
    })
    entry.extra.update({
        "bvid": bvid,
        "aid": doc.aid,
        "title": doc.title,
        "description": doc.desc,
        "pub_time": fmt_local(doc.published_at),
        "duration": doc.duration,
        "cover": doc.cover,
        "owner": {"mid": doc.owner_mid, "name": doc.owner_name, "face": doc.owner_face},
        "is_upower_exclusive": doc.is_upower_exclusive,
        "is_charging_arc": bool(item.extras.get("is_charging_arc")),
        "rights": doc.rights,
        "stat": doc.stat,
        "pages": [
            {"cid": p.cid, "page": p.page, "part": p.part, "duration": p.duration,
             "width": p.width, "height": p.height}
            for p in pages
        ],
        "page_source": page_source,
        "access": {
            "permission": permission,
            "dash_video_streams": getattr(access, "dash_video_streams", 0),
            "dash_audio_streams": getattr(access, "dash_audio_streams", 0),
            "durl_segments": getattr(access, "durl_segments", 0),
            "accept_quality": list(getattr(access, "accept_quality", []) or []),
            "accept_description": list(getattr(access, "accept_description", []) or []),
            "note": "按媒体流形态判定，故 code=0 不代表有权限（阶段 0 3.3.4）",
            "probe_note": access_note,
        },
        "missing": missing,
    })
    ctx.store.write_metadata(entry)

    ctx.finish_step(entry, "fetch", artifacts=[],
                    message=(f"分 P {len(pages)} 个（cid 来源：{page_source}）"
                             if pages else "无分 P"))
    return doc, pages, page_source, access, access_note


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
def _tool_paths(ctx: ArchiveContext) -> tuple[str, str]:
    """定位 ffmpeg / ffprobe（按条目缓存，避免每条都查 PATH）。"""
    key = ("tools", "media")
    cached = ctx.cache_get(key)
    if cached is None:
        cached = (
            executable_path("ffmpeg", getattr(ctx.config, "ffmpeg_path", "")),
            executable_path("ffprobe", getattr(ctx.config, "ffprobe_path", "")),
        )
        ctx.cache_put(key, cached)
    return cached


def _archive_media(ctx, item, entry, bvid: str, pages, permission: str, access):
    """下载各分 P 并校验。返回 ``(outcome, plan, notes)``。

    ``outcome`` 取 ``done`` / ``partial`` / ``failed`` / ``denied`` / ``skipped``。
    """
    cfg = ctx.config
    if permission == "preview_only":
        # 权限判定优先于"是否下载"开关：内容不可访问这个事实与开关无关，
        # 必须先如实记下来（否则 --no-media 会把无权限的充电视频报成 done）
        ctx.store.add_error(
            entry, "media", KIND_DENIED, None,
            f"账号对该充电视频无完整权限：playurl 只有 {getattr(access, 'durl_segments', 0)} 段试看，"
            f"dash 视频流 {getattr(access, 'dash_video_streams', 0)} 路",
        )
        reason = "media_disabled" if not cfg.download_media else "permission_preview_only"
        ctx.skip_step(entry, "media", reason,
                      message="无充电权限：不下载试看片段（需求 3.3.1 / 计划 3.3）"
                              + ("；本次也关闭了媒体下载" if not cfg.download_media else ""))
        return "denied", None, []
    if not cfg.download_media:
        ctx.skip_step(entry, "media", "media_disabled",
                      message="媒体下载已关闭（--no-media / [download].media=false）")
        return "skipped", None, []
    if not pages:
        ctx.skip_with_error(entry, "media", "no_pages", KIND_PARSE,
                            "分 P 列表为空（cid 缺失），无法定位媒体流")
        return "failed", None, ["分 P 列表为空，未下载媒体"]

    ffmpeg, ffprobe = _tool_paths(ctx)
    entry_dir = ctx.entry_dir(entry)
    plan = download_media(
        bvid=bvid,
        pages=pages,
        entry_dir=entry_dir,
        referer=item.url,
        cookie=ctx.cookie or _cookie_of(ctx),
        user_agent=cfg.user_agent,
        quality=cfg.video_quality,
        video_workers=cfg.video_workers,
        segment_workers=cfg.segment_workers,
        timeout=cfg.video_timeout_seconds,
        retries=cfg.retries,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        previous=list(entry.extra.get("media") or []),
        downloader=ctx.downloader,
        probe_runner=ctx.ffmpeg_runner,
        logger=ctx.logger,
        ui=ctx.ui,
    )
    entry.extra["media"] = plan.to_json()
    entry.extra["media_summary"] = {
        "downloader": plan.downloader,
        "format_selector": plan.format_selector,
        "verify_mode": plan.verify_mode,
        "pages": len(pages),
        "done": plan.done,
        "failed": plan.failed,
        "bytes": plan.bytes_written,
        "ffmpeg": ffmpeg or "",
        "ffprobe": ffprobe or "",
    }
    ctx.store.write_metadata(entry)

    notes = media_notes(plan)
    if plan.bytes_written:
        notes.append(f"媒体下载 {plan.bytes_written / 1024 / 1024:.1f} MiB")

    if plan.failed and not plan.done:
        first = next(r for r in plan.records if r.status == "failed")
        ctx.fail_step(entry, "media", error_kind=first.error_kind or KIND_DEPENDENCY,
                      message=f"{plan.failed} 个分 P 全部下载失败：{first.message}"[:400],
                      reason="download_failed")
        return "failed", plan, notes
    if plan.failed:
        first = next(r for r in plan.records if r.status == "failed")
        ctx.fail_step(entry, "media",
                      error_kind=first.error_kind or "partial_download",
                      message=f"{plan.done}/{len(plan.records)} 个分 P 下载成功，"
                              f"{plan.failed} 个失败：{first.message}"[:400],
                      reason="partial_download")
        return "partial", plan, notes

    ctx.finish_step(
        entry, "media",
        artifacts=[r.name for r in plan.records if r.status == "done" and r.name],
        message=f"{plan.summary()}（{plan.format_selector}，校验方式 {plan.verify_mode}）",
    )
    return "done", plan, notes


def _cookie_of(ctx: ArchiveContext) -> str:
    """兜底从 API 客户端取 Cookie（替身/自定义注入时 ctx.cookie 可能为空）。"""
    client = getattr(ctx.api, "client", None)
    return str(getattr(client, "cookie", "") or "")


# --------------------------------------------------------------------------- #
# transcript
# --------------------------------------------------------------------------- #
def _archive_transcript(ctx, item, entry, bvid: str, pages):
    """字幕优先 → ASR 兜底 → 合并落盘。返回 :class:`TranscriptPlan` 或 ``None``。"""
    if not pages:
        ctx.skip_step(entry, "transcript", "no_pages", message="无分 P，无文字来源")
        return None
    cfg = ctx.config
    ffmpeg, _ffprobe = _tool_paths(ctx)
    plan = build_transcript(
        api=ctx.api,
        bvid=bvid,
        pages=pages,
        entry_dir=ctx.entry_dir(entry),
        config=cfg,
        referer=item.url,
        transcriber=ctx.transcriber,
        ffmpeg=ffmpeg,
        logger=ctx.logger,
        runner=ctx.ffmpeg_runner,
        ui=ctx.ui,
    )
    entry.extra["transcript"] = plan.to_json()
    entry.extra["transcript_summary"] = plan.summary()
    ctx.store.write_metadata(entry)

    if plan.status == "failed":
        ctx.fail_step(entry, "transcript", error_kind=plan.error_kind or "transcript_failed",
                      reason=plan.reason or "transcript_failed", message=plan.message[:400])
        return plan
    if plan.status == "skipped":
        ctx.skip_step(entry, "transcript", plan.reason or "no_transcript",
                      message=plan.message)
        return plan

    artifacts = [plan.transcript_txt, plan.transcript_srt]
    artifacts.extend(p.srt_name for p in plan.pages if p.srt_name)
    artifacts.extend(p.text_name for p in plan.pages if p.text_name)
    ctx.finish_step(entry, "transcript", artifacts=artifacts, message=plan.summary())
    return plan


def _archive_stage3(ctx, item, entry, transcript_plan):
    """``summary`` + ``mindmap`` 两步（阶段 3）。返回 ``(产物, 说明, 是否有失败步骤, 计划)``。

    需求 3.3.6 是这里的第一条判断：**没有文字就不生成任何总结**，并把
    "缺少文字来源"显式写进两个步骤的 ``reason``/``message``（用户开启 ASR 后可重跑）。

    复用策略：``summary`` 与 ``mindmap`` 的输入指纹一致且产物齐全时直接沿用
    （不重复调用模型、不重复渲染）；``--force`` 强制重做。
    """
    from ..summarize import (
        MINDMAP_MMD,
        MINDMAP_PNG,
        SUMMARY_MD,
        SummaryPlan,
        build_mindmap,
        build_summary,
        mindmap_signature,
        mindmap_style_of,
        read_summary_body,
        summary_signature,
    )
    from ..summarize.prompt import PromptError, load_prompt_spec

    cfg = ctx.config
    entry_dir = ctx.entry_dir(entry)
    artifacts: list[str] = []
    notes: list[str] = []

    # ---- 没有文字来源：两个步骤都如实记 skipped，不编造 ---- #
    if transcript_plan is None or not transcript_plan.has_text:
        message = ("缺少文字来源（需求 3.3.6）：无平台字幕且未启用本地 ASR；"
                   "开启 --asr 并 retry 补做文字稿后可重跑")
        for name in ("summary", "mindmap"):
            ctx.skip_step(entry, name, "no_transcript", message=message)
        entry.extra["summary"] = {"status": "skipped", "reason": "no_transcript",
                                  "message": message, "mindmap": {"status": "skipped",
                                                                  "reason": "no_transcript"}}
        ctx.store.write_metadata(entry)
        return artifacts, notes, False, None

    # ---- 读回阶段 2 的文字稿 ---- #
    rel_txt = transcript_plan.transcript_txt or "transcript.txt"
    # 统一 strip：指纹是对"正文"算的，读文件时多出的首尾空白不能影响指纹
    transcript_text = _read_text(entry_dir / rel_txt).strip()
    if not transcript_text:
        message = (f"文字稿文件缺失或为空（{rel_txt}）：metadata 记录有文字但产物不在；"
                   "可先 retry 补做 transcript 步骤")
        for name in ("summary", "mindmap"):
            ctx.fail_step(entry, name, error_kind="missing_artifact", reason="transcript_missing",
                          message=message)
        return artifacts, notes, True, None

    # ---- 自定义 prompt（配置已在 check 阶段校验过，这里再兜一次） ---- #
    try:
        spec = load_prompt_spec(cfg.summary_prompt_file or None, base=entry_dir)
    except PromptError as exc:
        for name in ("summary", "mindmap"):
            ctx.fail_step(entry, name, error_kind="config_error", reason="prompt_invalid",
                          message=str(exc))
        return artifacts, notes, True, None

    previous = SummaryPlan.from_json(entry.extra.get("summary") or {})
    signature = summary_signature(cfg, spec, transcript_text)

    # ---- summary ---- #
    reuse = (not ctx.force and entry.step("summary").status == STEP_DONE
             and previous.signature == signature and (entry_dir / SUMMARY_MD).is_file())
    if reuse:
        plan = previous
        plan.status = "done"
        plan.reused = True
        plan.text = read_summary_body(entry_dir / SUMMARY_MD)
        plan.summary_md = SUMMARY_MD
        notes.append("总结沿用已有产物（prompt/模型/文字稿指纹未变，未重复调用模型）")
    else:
        plan = build_summary(
            transcript_text=transcript_text, config=cfg, entry_dir=entry_dir,
            prompt_spec=spec, chat_client=ctx.chat_client,
            api_key=getattr(ctx, "llm_api_key", "") or "",
            title=entry.title or item.title, author=entry.author, logger=ctx.logger,
        )
    notes.extend(plan.notes)
    if plan.status == "done":
        artifacts.append(plan.summary_md)
        ctx.finish_step(entry, "summary", artifacts=[plan.summary_md],
                        message=plan.summary_line())
    elif plan.status == STEP_SKIPPED:
        ctx.skip_step(entry, "summary", plan.reason, message=plan.message)
    else:
        ctx.fail_step(entry, "summary", error_kind=plan.error_kind or "summary_failed",
                      reason=plan.reason or "summary_failed", message=plan.message)
        notes.append(f"总结失败：{plan.message}")

    # ---- mindmap ---- #
    mm_signature = mindmap_signature(cfg, plan.signature) if plan.status == "done" else ""
    mm_reuse = (not ctx.force and plan.status == "done"
                and entry.step("mindmap").status == STEP_DONE
                and previous.mindmap_signature == mm_signature
                and (entry_dir / MINDMAP_MMD).is_file()
                and (entry_dir / MINDMAP_PNG).is_file())
    if mm_reuse:
        plan.mindmap_status = "done"
        plan.mindmap_reason = ""
        plan.mindmap_error_kind = ""
        plan.mindmap_reused = True
        plan.mindmap_mmd = MINDMAP_MMD
        plan.mindmap_png = MINDMAP_PNG
        # 指纹一致 ⇒ 样式一定没变；复用时不重新渲染，但 metadata 仍要如实记下用的是哪套样式
        plan.mindmap_style = mindmap_style_of(cfg)
        notes.append("思维导图沿用已有产物（输入指纹未变，未重复渲染）")
    else:
        plan = build_mindmap(plan=plan, config=cfg, entry_dir=entry_dir,
                             title=entry.title or item.title,
                             mmdc_runner=ctx.mmdc_runner, logger=ctx.logger)
    notes.extend(plan.notes)
    failed = plan.status == STEP_FAILED
    if plan.mindmap_status == "done":
        artifacts.extend([plan.mindmap_mmd or MINDMAP_MMD, plan.mindmap_png or MINDMAP_PNG])
        ctx.finish_step(entry, "mindmap",
                        artifacts=[plan.mindmap_mmd or MINDMAP_MMD,
                                   plan.mindmap_png or MINDMAP_PNG],
                        message=plan.mindmap_line())
    elif plan.mindmap_status == STEP_SKIPPED:
        ctx.skip_step(entry, "mindmap", plan.mindmap_reason, message=plan.mindmap_message)
    else:
        failed = True
        ctx.fail_step(entry, "mindmap", error_kind=plan.mindmap_error_kind or "render_failed",
                      reason=plan.mindmap_reason or "render_failed",
                      message=plan.mindmap_message)
        notes.append(f"导图未完成：{plan.mindmap_message}")

    entry.extra["summary"] = plan.to_json()
    ctx.store.write_metadata(entry)
    return artifacts, notes, failed, plan


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# 结果文案
# --------------------------------------------------------------------------- #
def _result_message(bvid: str, pages, media_plan, transcript_plan, summary_plan=None) -> str:
    parts = [f"视频 {bvid}：{len(pages)} P"]
    if media_plan is not None:
        parts.append(f"媒体 {media_plan.summary()}")
    else:
        parts.append("媒体未下载")
    if transcript_plan is not None:
        parts.append(f"文字 {transcript_plan.summary()}")
    if summary_plan is not None:
        parts.append(summary_plan.summary_line())
        parts.append(summary_plan.mindmap_line())
    return "；".join(parts)
