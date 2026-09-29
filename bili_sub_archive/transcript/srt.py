"""文字稿基础层：时间戳、片段模型、SRT 与纯文本序列化（纯函数，便于离线单测）。

需求 3.3.2 的落地要点：

- 各 P 的文字稿**保留分段和时间戳**（``transcript/P01.srt``）；
- 合并成整视频文字稿时**标注来源**（字幕 / ASR）与**时间基准**；
- **不得**把标题/简介冒充完整文字稿（需求 3.3.2 末句）——本模块只处理真实的
  字幕/ASR 片段，没有任何"拿简介兜底"的分支。

平台字幕文件的结构（阶段 0 第 3.3.3 节实测，900 段样本）：

```json
{"type": "AIsubtitle", "lang": "zh", "body": [
  {"from": 0.24, "to": 0.76, "sid": 1, "location": 2, "content": "大家好", "music": 0.0}
]}
```

``from`` / ``to`` 为**秒（浮点）**，直接就是需求要的带时间戳文字稿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 合并纯文本时的段落切分阈值
PARAGRAPH_MAX_CHARS = 220
PARAGRAPH_MAX_GAP = 2.0
PARAGRAPH_MAX_SPAN = 60.0

_TS_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)


@dataclass
class Segment:
    """一段带时间戳的文字（字幕或 ASR 片段）。"""

    start: float = 0.0
    end: float = 0.0
    text: str = ""
    page: int = 1
    source: str = "subtitle"        # subtitle | asr
    sid: Any = None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))

    def to_json(self) -> dict:
        return {
            "start": round(float(self.start), 3),
            "end": round(float(self.end), 3),
            "text": self.text,
            "page": self.page,
            "source": self.source,
            "sid": self.sid,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Segment":
        data = data or {}
        return cls(
            start=float(data.get("start") or 0.0),
            end=float(data.get("end") or 0.0),
            text=str(data.get("text") or ""),
            page=int(data.get("page") or 1),
            source=str(data.get("source") or "subtitle"),
            sid=data.get("sid"),
        )


def format_timestamp(seconds: float) -> str:
    """秒 → ``HH:MM:SS,mmm``（SRT 规范，逗号分隔毫秒）。"""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0
    if total < 0:
        total = 0.0
    millis = int(round(total * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_timestamp(text: str) -> float:
    """``HH:MM:SS,mmm`` → 秒；无法解析返回 0.0。"""
    match = _TS_RE.search(str(text or ""))
    if not match:
        return 0.0
    ms = match.group("ms").ljust(3, "0")[:3]
    return (int(match.group("h")) * 3600 + int(match.group("m")) * 60
            + int(match.group("s")) + int(ms) / 1000.0)


def parse_bili_subtitle(payload: Any, *, page: int = 1) -> list[Segment]:
    """平台字幕 JSON（``body[]``）→ :class:`Segment` 列表。"""
    if isinstance(payload, dict):
        body = payload.get("body")
    elif isinstance(payload, list):
        body = payload
    else:
        body = None
    if not isinstance(body, list):
        return []
    out: list[Segment] = []
    for raw in body:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("content") or "").strip()
        if not text:
            continue
        out.append(
            Segment(
                start=float(raw.get("from") or 0.0),
                end=float(raw.get("to") or raw.get("from") or 0.0),
                text=text,
                page=page,
                source="subtitle",
                sid=raw.get("sid"),
            )
        )
    return out


def segments_to_srt(segments: list[Segment], *, page_prefix: bool = False,
                    marker: str = "", index_start: int = 1) -> str:
    """片段 → SRT 文本。

    ``page_prefix=True`` 时每条文本前加 ``[P01] ``，用于**合并文件**里标注 P 号
    （各 P 的媒体文件没有拼接，保留 P 内原始时间戳才是可用的）。
    ``marker`` 非空时在开头插入一条标记 cue，写明 P 号与时间基准。
    """
    lines: list[str] = []
    index = int(index_start)
    if marker:
        lines.extend([str(index), "00:00:00,000 --> 00:00:00,000", marker, ""])
        index += 1
    for segment in segments:
        text = str(segment.text or "").strip()
        if not text:
            continue
        if page_prefix:
            text = f"[P{int(segment.page):02d}] {text}"
        lines.extend([
            str(index),
            f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}",
            text,
            "",
        ])
        index += 1
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def parse_srt(text: str) -> list[Segment]:
    """SRT 文本 → 片段列表（供往返测试与既有产物再读入）。"""
    out: list[Segment] = []
    blocks = re.split(r"\r?\n\s*\r?\n", str(text or "").strip())
    for block in blocks:
        rows = [r for r in block.splitlines() if r.strip()]
        if len(rows) < 2:
            continue
        ts_row = next((r for r in rows if "-->" in r), None)
        if ts_row is None:
            continue
        head, _, tail = ts_row.partition("-->")
        body = rows[rows.index(ts_row) + 1:]
        page = 1
        content = "\n".join(body).strip()
        prefix = re.match(r"^\[P(\d{1,3})\]\s*", content)
        if prefix:
            page = int(prefix.group(1))
            content = content[prefix.end():].strip()
        out.append(
            Segment(
                start=parse_timestamp(head),
                end=parse_timestamp(tail),
                text=content,
                page=page,
            )
        )
    return out


def segments_to_text(segments: list[Segment], *, paragraph_chars: int = PARAGRAPH_MAX_CHARS,
                     max_gap: float = PARAGRAPH_MAX_GAP,
                     max_span: float = PARAGRAPH_MAX_SPAN) -> str:
    """片段 → 便于阅读的纯文本（按停顿与长度自然分段）。"""
    paragraphs: list[str] = []
    buffer: list[str] = []
    chars = 0
    span_start: float | None = None
    previous_end: float | None = None

    def flush() -> None:
        nonlocal buffer, chars, span_start, previous_end
        if buffer:
            paragraphs.append("".join(buffer).strip())
        buffer = []
        chars = 0
        span_start = None
        previous_end = None

    for segment in segments:
        text = str(segment.text or "").strip()
        if not text:
            continue
        gap = (segment.start - previous_end) if previous_end is not None else 0.0
        span = (segment.end - span_start) if span_start is not None else 0.0
        if buffer and (chars + len(text) > paragraph_chars or gap > max_gap or span > max_span):
            flush()
        if span_start is None:
            span_start = segment.start
        buffer.append(text)
        chars += len(text)
        previous_end = segment.end
    flush()
    return "\n\n".join(p for p in paragraphs if p)


def segments_chars(segments: list[Segment]) -> int:
    return sum(len(str(s.text or "").strip()) for s in segments)


@dataclass
class PageTranscript:
    """单个分 P 的文字稿产物与来源记录。"""

    page: int
    cid: int = 0
    part: str = ""
    duration: float = 0.0
    source: str = ""                # subtitle | asr | none
    lang: str = ""
    lang_label: str = ""
    segments: list[Segment] = field(default_factory=list)
    srt_name: str = ""              # 相对条目目录
    text_name: str = ""
    raw_name: str = ""
    #: 带 auth_key 的临时签名地址：**只在内存里用于日志**，绝不落盘（见 to_json）
    subtitle_url: str = ""
    error_kind: str = ""
    message: str = ""
    skipped_reason: str = ""

    @property
    def chars(self) -> int:
        return segments_chars(self.segments)

    @property
    def has_text(self) -> bool:
        return bool(self.segments)

    def to_json(self) -> dict:
        # 注意：**不落盘** subtitle_url —— 它是带 auth_key 的临时签名 CDN 地址
        # （计划 3.2：metadata 不含 Cookie、API 密钥或临时下载 URL）。
        # 原始字幕内容已存进 transcript/P0N.subtitle.json，重做时重新调接口取即可。
        return {
            "page": self.page, "cid": self.cid, "part": self.part,
            "duration": round(float(self.duration), 3),
            "source": self.source, "lang": self.lang, "lang_label": self.lang_label,
            "segments": len(self.segments), "chars": self.chars,
            "srt": self.srt_name, "text": self.text_name, "raw": self.raw_name,
            "error_kind": self.error_kind, "message": self.message,
            "skipped_reason": self.skipped_reason,
        }
