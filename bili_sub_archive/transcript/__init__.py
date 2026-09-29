"""文字稿编排：字幕优先 → 本地 ASR 兜底 → 跨 P 合并（阶段 2）。

需求 3.3.2 的完整落地：

```text
各分 P:
  平台字幕（/x/player/wbi/v2 → subtitle_url → CDN JSON）   ← 主路径
    ↓ 没有字幕且开关开启
  本地 ASR（ffmpeg 提音频 → faster-whisper）               ← 兜底，默认关闭
    ↓ 都没有
  如实记 skipped(no_transcript)，**不生成虚构文字稿**
合并:
  transcript.txt        按 P 分节的可读纯文本（标注来源与字数）
  transcript_timed.srt  带时间戳，每条前缀 [P01] 标 P 号，P 边界插标记 cue 说明时间基准
```

**时间基准的取舍**：需求明确"各 P 媒体文件保存在同一文件夹内，不强制拼接为单个
视频文件"，因此合并文件保留**各 P 内部的原始时间戳**（可直接对 ``videos/P01.mp4``
定位），而不是累加成一条虚构的连续时间轴。P 号与时间基准都写进标记 cue 与 metadata。

**来源标注**：每条分 P 记录 ``source``（``subtitle`` / ``asr``），合并纯文本的每节
标题下写明来源与语言，需求 3.3.2 的"标注字幕、ASR 各自来源"由此满足。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..paths import atomic_write_text, rel_path
from ..ui import NULL_TASK
from .asr import (
    KIND_NO_MEDIA,
    KIND_NO_SPEECH,
    FasterWhisperTranscriber,
    Transcriber,
    extract_audio,
    find_media_file,
    transcribe_page,
)
from .srt import (
    PageTranscript,
    Segment,
    format_timestamp,
    parse_bili_subtitle,
    parse_srt,
    segments_chars,
    segments_to_srt,
    segments_to_text,
)
from .subtitle import (
    DEFAULT_PREFER_LANGS,
    REASON_NO_SUBTITLE,
    REASON_SUBTITLE_DISABLED,
    fetch_page_subtitle,
    pick_subtitle,
    subtitle_candidates,
    subtitle_entries,
    write_page_files,
)

#: 无任何文字来源时的步骤原因（需求 3.3.6）
REASON_NO_TRANSCRIPT = "no_transcript"
#: 有媒体但缺依赖（ASR 开着但没装 faster-whisper / ffmpeg）
REASON_DEPENDENCY_MISSING = "dependency_missing"

TRANSCRIPT_TXT = "transcript.txt"
TRANSCRIPT_SRT = "transcript_timed.srt"

SOURCE_LABEL = {"subtitle": "平台字幕", "asr": "本地 ASR"}


@dataclass
class TranscriptPlan:
    """一个视频条目的文字稿结果。"""

    pages: list[PageTranscript] = field(default_factory=list)
    transcript_txt: str = ""
    transcript_srt: str = ""
    sources: list[str] = field(default_factory=list)
    time_base: str = "per_page"
    chars: int = 0
    segment_count: int = 0
    status: str = "done"                 # done | skipped | failed
    reason: str = ""
    error_kind: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return any(p.has_text for p in self.pages)

    @property
    def pages_with_text(self) -> int:
        return sum(1 for p in self.pages if p.has_text)

    @property
    def source_labels(self) -> str:
        return "、".join(SOURCE_LABEL.get(s, s) for s in self.sources) or "无"

    def to_json(self) -> dict:
        return {
            "time_base": self.time_base,
            "sources": list(self.sources),
            "pages": [p.to_json() for p in self.pages],
            "chars": self.chars,
            "segments": self.segment_count,
            "transcript_txt": self.transcript_txt,
            "transcript_srt": self.transcript_srt,
            "status": self.status,
            "reason": self.reason,
            "error_kind": self.error_kind,
            "message": self.message,
            "notes": list(self.notes),
            "note": "各 P 时间戳相对其自身媒体文件；各 P 媒体未拼接（需求 3.3.1）",
        }

    def summary(self) -> str:
        if not self.has_text:
            return f"无文字来源（{self.reason or 'no_transcript'}）"
        return (f"{self.pages_with_text}/{len(self.pages)} P 有文字"
                f"（{self.source_labels}），{self.segment_count} 段 / {self.chars} 字")


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def merge_text(pages: list[PageTranscript]) -> str:
    """合并纯文本：每 P 一节，节内标注来源/语言/段数。"""
    chunks: list[str] = [
        "# 合并文字稿",
        "",
        "- 时间基准：各 P 内部计时（各 P 媒体未拼接，见 transcript_timed.srt 的标记）",
        f"- 分 P 数：{len(pages)}；有文字：{sum(1 for p in pages if p.has_text)}",
        "",
    ]
    for record in pages:
        if not record.has_text:
            continue
        title = f"## P{int(record.page):02d}"
        if record.part:
            title += f" {record.part}"
        label = SOURCE_LABEL.get(record.source, record.source or "未知")
        lang = f"（{record.lang_label or record.lang}）" if (record.lang_label or record.lang) else ""
        meta = (f"> 来源：{label}{lang}；{len(record.segments)} 段 / {record.chars} 字；"
                f"本 P 时长 {format_timestamp(record.duration)}")
        chunks.extend([title, "", meta, "", segments_to_text(record.segments), ""])
    return "\n".join(chunks).rstrip() + "\n"


def merge_srt(pages: list[PageTranscript]) -> str:
    """合并带时间戳文字稿：每条前缀 ``[P01]``，P 边界插标记 cue 说明时间基准。"""
    blocks: list[str] = []
    index = 1
    for record in pages:
        if not record.has_text:
            continue
        label = SOURCE_LABEL.get(record.source, record.source or "未知")
        lang = f"（{record.lang_label or record.lang}）" if (record.lang_label or record.lang) else ""
        marker = (f"【P{int(record.page):02d}】{record.part or '未命名分 P'} · "
                  f"来源 {label}{lang} · 时间基准：本 P 起点 {format_timestamp(0)}"
                  f"（时间戳相对 videos/P{int(record.page):02d}.*，各 P 未拼接）")
        block = segments_to_srt(record.segments, page_prefix=True, marker=marker,
                                index_start=index)
        if block:
            blocks.append(block)
            index += 1 + sum(1 for s in record.segments if str(s.text or "").strip())
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def build_transcript(
    *,
    api,
    bvid: str,
    pages: list,
    entry_dir: Path,
    config,
    referer: str = "",
    transcriber: Transcriber | None = None,
    ffmpeg: str = "",
    logger=None,
    runner=None,
    ui=None,
) -> TranscriptPlan:
    """为一个视频条目构建完整文字稿（各 P 提取 + 合并落盘）。

    ``pages`` 为 :class:`bili_sub_archive.bili.parse.PageInfo` 列表（按 page 升序）。
    任何失败都写进返回的 :class:`TranscriptPlan`，不抛异常。
    ``ui`` 给出时，走本地 ASR 的分 P 会挂一个"已产出 N 段"的进度任务。
    """
    entry_dir = Path(entry_dir)
    plan = TranscriptPlan()
    prefer_subtitle = bool(getattr(config, "prefer_subtitle", True))
    asr_enabled = bool(getattr(config, "asr_enabled", False))
    keep_audio = bool(getattr(config, "asr_keep_audio", False))

    engine = transcriber
    if engine is None and asr_enabled:
        engine = FasterWhisperTranscriber(
            model=getattr(config, "asr_model", "small"),
            device=getattr(config, "asr_device", "cpu"),
            compute_type=getattr(config, "asr_compute_type", "int8"),
            language=getattr(config, "asr_language", "zh"),
            beam_size=getattr(config, "asr_beam_size", 5),
            logger=logger,
        )

    for page in pages:
        number = int(getattr(page, "page", 0) or 0)
        cid = int(getattr(page, "cid", 0) or 0)
        record: PageTranscript
        if prefer_subtitle:
            record = fetch_page_subtitle(
                api, bvid=bvid, page=page, cid=cid, entry_dir=entry_dir,
                referer=referer, logger=logger,
            )
        else:
            record = PageTranscript(
                page=number, cid=cid, part=str(getattr(page, "part", "") or ""),
                duration=float(getattr(page, "duration", 0) or 0),
                skipped_reason=REASON_SUBTITLE_DISABLED,
                message="已按配置关闭平台字幕提取",
            )

        if not record.has_text and asr_enabled and engine is not None:
            task = (ui.task(total=None, label=f"P{number:02d} 本地转写", keep=False)
                    if ui is not None else NULL_TASK)

            def _on_segment(count: int, _task=task, _page=number) -> None:
                _task.update(completed=count,
                             description=f"P{_page:02d} 本地转写 {count} 段")

            outcome = transcribe_page(
                engine, entry_dir=entry_dir, page=page, ffmpeg=ffmpeg,
                keep_audio=keep_audio, logger=logger, runner=runner,
                on_segment=_on_segment if task.active else None,
            )
            if task.active:
                task.done(description=f"P{number:02d} 转写完成" if outcome.ok
                          else f"P{number:02d} 未产出文字")
            if outcome.ok:
                record.segments = outcome.segments
                record.source = "asr"
                record.lang = outcome.language or str(getattr(config, "asr_language", "") or "")
                record.lang_label = f"本地 ASR（{outcome.model}）"
                record.error_kind = ""
                record.message = ""
                record.skipped_reason = ""
                write_page_files(record, entry_dir)
            else:
                # 良性跳过：上游没给媒体、或识别跑完确实没有人声 —— 重跑不会变。
                # 其余（转写失败、缺依赖）是硬失败，必须记 failed 让 retry 补做。
                benign = outcome.error_kind in (KIND_NO_MEDIA, KIND_NO_SPEECH)
                record.error_kind = "" if benign else outcome.error_kind
                record.message = outcome.message
                if outcome.error_kind == KIND_NO_MEDIA:
                    record.skipped_reason = KIND_NO_MEDIA
                elif outcome.error_kind == KIND_NO_SPEECH:
                    record.skipped_reason = KIND_NO_SPEECH
                elif outcome.error_kind == "dependency_missing":
                    record.skipped_reason = REASON_DEPENDENCY_MISSING
                else:
                    record.skipped_reason = "asr_failed"
        elif not record.has_text and not asr_enabled and not record.error_kind:
            # 无平台字幕 + ASR 关闭：**不是**终态 —— 用户开启 ASR 后应能 retry 补做，
            # 因此原因记 ``asr_disabled``（"当时少做了一步"），把"确实没有字幕"
            # 这个事实留在 message 里（写进 metadata.extra.transcript.pages[].message）。
            if record.skipped_reason == REASON_SUBTITLE_DISABLED:
                record.message = record.message or "已按配置关闭平台字幕提取，且未开启本地 ASR"
            else:
                had_no_subtitle = record.skipped_reason == REASON_NO_SUBTITLE
                record.skipped_reason = "asr_disabled"
                record.message = (
                    "该分 P 无平台字幕，且未开启本地 ASR；按需求 3.3.6 不生成虚构文字稿"
                    if had_no_subtitle else
                    record.message or "未开启本地 ASR"
                )

        plan.pages.append(record)

    # ---- 来源汇总 ---- #
    # 固定顺序（字幕优先）而不是字母序，读起来更符合"主路径 → 兜底"的语义
    plan.sources = [s for s in ("subtitle", "asr")
                    if any(p.has_text and p.source == s for p in plan.pages)]
    plan.segment_count = sum(len(p.segments) for p in plan.pages if p.has_text)
    plan.chars = sum(p.chars for p in plan.pages if p.has_text)

    if not plan.has_text:
        # 取文字这件事本身失败（字幕接口/下载/结构错误、ASR 失败、缺依赖）→ 硬失败，
        # 必须记 failed 让 retry 补做；只有"确实没有文字来源"才是良性跳过。
        broken = [p for p in plan.pages if not p.has_text and p.error_kind]
        if broken:
            plan.status = "failed"
            plan.error_kind = broken[0].error_kind
            plan.reason = ("dependency_missing" if plan.error_kind == "dependency_missing"
                           else "transcript_failed")
            plan.message = broken[0].message or "文字稿获取失败"
            plan.notes.append(
                f"{len(broken)} 个分 P 的文字稿获取失败：{plan.message}（可 retry 补做）"
            )
            return plan
        plan.status = "skipped"
        plan.reason = _no_text_reason(plan.pages, asr_enabled=asr_enabled)
        plan.message = ("该视频没有可用文字来源（无平台字幕，且未启用本地 ASR）；"
                        "按需求 3.3.6 不生成虚构文字稿")
        plan.notes.append(
            "无文字稿：按需求 3.3.6，总结/导图将标记为缺少文字来源；"
            "开启 --asr 后 retry 可补做"
        )
        return plan

    # ---- 落盘 ---- #
    txt_path = entry_dir / TRANSCRIPT_TXT
    srt_path = entry_dir / TRANSCRIPT_SRT
    atomic_write_text(txt_path, merge_text(plan.pages))
    atomic_write_text(srt_path, merge_srt(plan.pages))
    plan.transcript_txt = rel_path(txt_path, entry_dir)
    plan.transcript_srt = rel_path(srt_path, entry_dir)

    partial = [p for p in plan.pages if not p.has_text]
    if partial:
        plan.notes.append(
            f"{len(partial)} 个分 P 没有文字来源（"
            + "、".join(f"P{p.page:02d}" for p in partial)
            + "）；其余分 P 已归档，可开启 ASR 后 retry 补做"
        )
    for record in plan.pages:
        if record.error_kind:
            plan.notes.append(f"P{record.page:02d}：{record.message}")
    return plan


def _no_text_reason(pages: list[PageTranscript], *, asr_enabled: bool) -> str:
    if any(p.skipped_reason == REASON_DEPENDENCY_MISSING for p in pages):
        return REASON_DEPENDENCY_MISSING
    if any(p.skipped_reason == "asr_disabled" for p in pages):
        # 可重做：用户开启 --asr 后 retry 会重新进入 transcript 步骤
        return "asr_disabled"
    if any(p.skipped_reason == KIND_NO_MEDIA for p in pages):
        # 上游没有媒体（权限门控等）：重跑不会改变，按终态跳过
        return KIND_NO_MEDIA
    if asr_enabled:
        return "no_transcript"
    if all(p.skipped_reason == REASON_SUBTITLE_DISABLED for p in pages if p.skipped_reason):
        return REASON_SUBTITLE_DISABLED
    return REASON_NO_TRANSCRIPT


__all__ = [
    "DEFAULT_PREFER_LANGS",
    "KIND_NO_MEDIA",
    "KIND_NO_SPEECH",
    "REASON_DEPENDENCY_MISSING",
    "REASON_NO_SUBTITLE",
    "REASON_NO_TRANSCRIPT",
    "REASON_SUBTITLE_DISABLED",
    "SOURCE_LABEL",
    "TRANSCRIPT_SRT",
    "TRANSCRIPT_TXT",
    "FasterWhisperTranscriber",
    "PageTranscript",
    "Segment",
    "TranscriptPlan",
    "Transcriber",
    "build_transcript",
    "extract_audio",
    "fetch_page_subtitle",
    "find_media_file",
    "format_timestamp",
    "merge_srt",
    "merge_text",
    "parse_bili_subtitle",
    "parse_srt",
    "pick_subtitle",
    "segments_chars",
    "segments_to_srt",
    "segments_to_text",
    "subtitle_candidates",
    "subtitle_entries",
    "transcribe_page",
    "write_page_files",
]
