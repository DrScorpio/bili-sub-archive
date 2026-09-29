"""平台字幕提取（阶段 2 的文字主路径）。

阶段 0 第 3.3.3 节的三条关键结论，逐条落地：

1. ``/x/player/wbi/v2?bvid=&cid=``（WBI，需登录）返回
   ``data.subtitle.subtitles[]``，每项含 ``lan`` / ``lan_doc`` / ``subtitle_url``；
2. **未登录时该接口返回空数组而不是错误** —— 静默失败点。本项目 ``sync`` 强制
   要求登录态，因此"空数组"在这里可以如实解释为"该分 P 没有平台字幕"，
   但仍然把登录态检查留在调用链上游（不在这里假装成"无字幕"）；
3. 拿到 ``subtitle_url`` 后**字幕文件本体在 CDN 上匿名可下载**，结构
   ``{from, to, sid, content, ...}``，``from`` / ``to`` 为秒（浮点）。

字幕是首选文字来源（阶段 0：本项目样本公开列表前 10 条全部带 ``ai-zh``），
本地 ASR 只是兜底。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..bili.client import upgrade_url
from ..errors import KIND_PARSE
from ..paths import atomic_write_text, ensure_dir, rel_path
from .srt import PageTranscript, parse_bili_subtitle, segments_to_srt, segments_to_text

#: 首选字幕语言顺序（``ai-zh`` 是 B 站 AI 中文轨的实际取值，阶段 0 实测）
DEFAULT_PREFER_LANGS: tuple[str, ...] = (
    "zh-Hans", "zh-CN", "zh-Hant", "zh-TW", "ai-zh", "zh", "ai-en", "en",
)

#: 无字幕时的原因（写入 metadata，便于用户判断要不要开 ASR）
REASON_NO_SUBTITLE = "no_subtitle"
REASON_SUBTITLE_DISABLED = "subtitle_disabled"


def subtitle_entries(resp) -> list[dict]:
    """从 ``subtitle_list`` 响应里取出字幕条目（兼容两种包裹形状）。"""
    data = getattr(resp, "data", None)
    if not isinstance(data, dict):
        return []
    block = data.get("subtitle")
    if not isinstance(block, dict):
        block = data if isinstance(data.get("subtitles"), list) else {}
    entries = block.get("subtitles")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def pick_subtitle(entries: list[dict], prefer: tuple[str, ...] = DEFAULT_PREFER_LANGS) -> dict | None:
    """按语言优先级挑一条字幕；``is_lock`` 的条目跳过。"""
    usable = [e for e in entries if not e.get("is_lock")]
    if not usable:
        return None
    by_lan = {str(e.get("lan") or "").lower(): e for e in usable}
    for lang in prefer:
        hit = by_lan.get(lang.lower())
        if hit is not None:
            return hit
    for entry in usable:                      # 退而求其次：任意中文轨
        if str(entry.get("lan") or "").lower().startswith("zh"):
            return entry
    return usable[0]


def subtitle_candidates(entries: list[dict]) -> list[str]:
    return [str(e.get("lan") or "") for e in entries if e.get("lan")]


def fetch_page_subtitle(
    api,
    *,
    bvid: str,
    page,
    cid: int,
    entry_dir: Path,
    referer: str = "",
    prefer_langs: tuple[str, ...] = DEFAULT_PREFER_LANGS,
    logger=None,
) -> PageTranscript:
    """取一个分 P 的平台字幕并落盘（``transcript/P01.subtitle.json`` + ``.srt`` + ``.txt``）。

    任何失败都写进返回的 :class:`PageTranscript`（``error_kind`` / ``message``），
    不抛异常 —— 上游据此决定"降级到 ASR"还是"如实记 skipped"。
    """
    number = int(getattr(page, "page", 0) or 0)
    record = PageTranscript(
        page=number,
        cid=int(cid or 0),
        part=str(getattr(page, "part", "") or ""),
        duration=float(getattr(page, "duration", 0) or 0),
    )

    resp = api.subtitle_list(bvid, cid)
    if not resp.ok:
        record.error_kind = resp.error_kind
        record.message = f"字幕列表接口失败：{resp.kind_label} {resp.message}"
        return record

    entries = subtitle_entries(resp)
    chosen = pick_subtitle(entries, prefer_langs)
    if chosen is None:
        record.skipped_reason = REASON_NO_SUBTITLE
        record.message = (
            "该分 P 没有平台字幕（登录态下返回空数组，阶段 0 第 3.3.3 节）"
            if not entries else "字幕条目全部被锁定（is_lock）"
        )
        return record

    record.lang = str(chosen.get("lan") or "")
    record.lang_label = str(chosen.get("lan_doc") or "")
    url = upgrade_url(str(chosen.get("subtitle_url") or ""))
    record.subtitle_url = url
    if not url:
        record.error_kind = KIND_PARSE
        record.message = "字幕条目缺少 subtitle_url"
        return record

    transcript_dir = ensure_dir(Path(entry_dir) / "transcript")
    raw_path = transcript_dir / f"P{number:02d}.subtitle.json"
    result = api.download(url, raw_path, referer=referer)
    if not result.ok:
        record.error_kind = result.error_kind
        record.message = f"字幕文件下载失败：{result.message}"
        return record

    segments = _load_segments(raw_path, number)
    if not segments:
        record.error_kind = KIND_PARSE
        record.message = "字幕文件结构不符合预期（缺少 body[].content）"
        return record

    record.segments = segments
    record.source = "subtitle"
    record.skipped_reason = ""
    record.message = ""
    record.raw_name = rel_path(raw_path, entry_dir)
    write_page_files(record, entry_dir)
    if logger is not None:
        logger.debug(
            f"    P{number:02d} 字幕：{record.lang}（{record.lang_label}）"
            f"{len(segments)} 段 / {record.chars} 字"
        )
    return record


def _load_segments(raw_path: Path, number: int) -> list:
    try:
        text = Path(raw_path).read_text(encoding="utf-8-sig")
    except OSError:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    return parse_bili_subtitle(payload, page=number)


def write_page_files(record: PageTranscript, entry_dir: Path) -> None:
    """写单 P 的 ``.srt`` 与 ``.txt``（需求 3.3.2：各 P 保留分段与时间戳）。

    字幕与 ASR 两条来源共用这一步，保证产物形态一致。
    """
    number = int(record.page)
    transcript_dir = ensure_dir(Path(entry_dir) / "transcript")
    srt_path = transcript_dir / f"P{number:02d}.srt"
    text_path = transcript_dir / f"P{number:02d}.txt"
    atomic_write_text(srt_path, segments_to_srt(record.segments))
    atomic_write_text(text_path, segments_to_text(record.segments) + "\n")
    record.srt_name = rel_path(srt_path, entry_dir)
    record.text_name = rel_path(text_path, entry_dir)
