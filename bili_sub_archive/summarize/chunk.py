"""长文字稿分块（需求 3.3.3："长稿应分段处理再合并，**不直接丢弃尾段**"）。

分块规则：

1. 先剥掉 ``transcript.txt`` 的头部（标题 + 时间基准/分 P 统计），只把**正文**送去总结；
2. 按空行切成块，``## Pxx`` 标题行开启新的分 P 归属 —— 每个分块都记录它覆盖的分 P 号，
   便于在 prompt 里说明"这段来自哪几个 P"，也便于总结里标注时间来源；
3. 累加到 ``chunk_chars`` 就切一刀，并保留 ``overlap_chars`` 的尾部重叠，避免恰好切在
   结论中间导致信息断裂；
4. **块数超上限时不是丢尾段，而是放大每块字数**：``max_chunks`` 是"最多调用多少次模型"
   的成本上限，超了就按 ``ceil(正文长度 / max_chunks)`` 重新分块（:attr:`ChunkPlan.scaled`），
   宁可每块更长，也不让最后几个分 P 消失。这条是本模块最重要的不变量，
   :func:`uncovered_ranges` 供测试证明正文被完整覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 分 P 标题行：``## P01 分P标题``
PAGE_HEADING_RE = re.compile(r"^##\s*P(\d{1,3})\b", re.MULTILINE)
#: 连续非空行组成的块（空行分隔）
_BLOCK_RE = re.compile(r"[^\n](?:[^\n]|\n(?![ \t]*\n))*")

DEFAULT_OVERLAP_CHARS = 200


@dataclass
class Chunk:
    """一个待总结的文字片段。"""

    index: int
    text: str
    start: int = 0          # 正文中的主区间（不含重叠部分）
    end: int = 0
    pages: list[int] = field(default_factory=list)
    overlap_chars: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)

    def pages_label(self) -> str:
        if not self.pages:
            return "未知分 P"
        return "、".join(f"P{p:02d}" for p in self.pages)


@dataclass
class ChunkPlan:
    """分块方案与统计（写进 ``metadata.json`` 便于核对"没丢内容"）。"""

    chunks: list[Chunk] = field(default_factory=list)
    header: str = ""
    body: str = ""
    total_chars: int = 0
    body_chars: int = 0
    chunk_chars: int = 0
    requested_chunk_chars: int = 0
    max_chunks: int = 0
    overlap_chars: int = 0
    scaled: bool = False
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.chunks)

    def to_json(self) -> dict:
        return {
            "chunks": self.count,
            "chunk_chars": self.chunk_chars,
            "requested_chunk_chars": self.requested_chunk_chars,
            "max_chunks": self.max_chunks,
            "overlap_chars": self.overlap_chars,
            "scaled": self.scaled,
            "body_chars": self.body_chars,
            "total_chars": self.total_chars,
            "pages": sorted({p for c in self.chunks for p in c.pages}),
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# 头部与正文
# --------------------------------------------------------------------------- #
def split_header(text: str) -> tuple[str, str]:
    """把合并文字稿拆成 ``(头部, 正文)``；正文从第一个 ``## Pxx`` 标题开始。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for idx, line in enumerate(lines):
        if PAGE_HEADING_RE.match(line):
            return "\n".join(lines[:idx]).strip(), "\n".join(lines[idx:]).strip()
    return "", "\n".join(lines).strip()


def _blocks(body: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _BLOCK_RE.finditer(body)]


def _hard_split(body: str, start: int, end: int, limit: int):
    """把超长块按行切成不超过 ``limit`` 的片段（找不到换行就硬切）。"""
    pos = start
    while pos < end:
        stop = min(pos + limit, end)
        if stop < end:
            cut = body.rfind("\n", pos, stop)
            if cut > pos + max(1, limit // 2):
                stop = cut
        yield (pos, stop)
        pos = stop


def split_transcript(text: str, *, chunk_chars: int = 6000, overlap_chars: int = DEFAULT_OVERLAP_CHARS,
                     max_chunks: int = 40) -> ChunkPlan:
    """把文字稿切成若干块（见模块 docstring 的四条规则）。"""
    chunk_chars = max(200, int(chunk_chars or 6000))
    overlap = max(0, min(int(overlap_chars or 0), chunk_chars // 4))
    max_chunks = max(1, int(max_chunks or 1))

    header, body = split_header(text)
    plan = ChunkPlan(header=header, body=body, total_chars=len(str(text or "")),
                     body_chars=len(body), requested_chunk_chars=chunk_chars,
                     max_chunks=max_chunks, overlap_chars=overlap,
                     chunk_chars=chunk_chars)
    if not body:
        return plan

    # 规则 4：块数超上限 → 放大每块，而不是丢尾段
    if body_chars_needs_scaling(len(body), chunk_chars, max_chunks):
        scaled = -(-len(body) // max_chunks)          # ceil
        plan.scaled = True
        plan.chunk_chars = max(chunk_chars, scaled)
        plan.note = (f"正文 {len(body)} 字，按 {chunk_chars} 字/块会超过 max_chunks={max_chunks}，"
                     f"已放大为 {plan.chunk_chars} 字/块以完整覆盖全文（不丢尾段）")

    # ---- 片段化：块 → 片段（带分 P 归属） ---- #
    pieces: list[tuple[int, int, int]] = []           # (start, end, page)
    current_page = 0
    for start, end in _blocks(body):
        head = PAGE_HEADING_RE.match(body[start:end])
        if head:
            current_page = int(head.group(1))
        for piece in _hard_split(body, start, end, plan.chunk_chars):
            pieces.append((piece[0], piece[1], current_page))
    if not pieces:
        return plan

    # ---- 片段 → 分块（按**累计字数**装填，块边界落在行边界上） ---- #
    groups: list[list[tuple[int, int, int]]] = []
    current: list[tuple[int, int, int]] = []
    size = 0
    for piece in pieces:
        length = piece[1] - piece[0]
        if current and size + length > plan.chunk_chars:
            groups.append(current)
            current, size = [], 0
        current.append(piece)
        size += length
    if current:
        groups.append(current)

    for idx, group in enumerate(groups):
        span_start = group[0][0]
        # 把分块之间的空行也划进本块，保证主区间首尾相接、无缝隙
        span_end = groups[idx + 1][0][0] if idx + 1 < len(groups) else len(body)
        overlap_text = ""
        if idx > 0 and overlap:
            cut = max(span_start - overlap, groups[idx - 1][0][0])
            overlap_text = body[cut:span_start]
        text = (overlap_text + "\n\n" + body[span_start:span_end]).strip() if overlap_text \
            else body[span_start:span_end].strip()
        pages = sorted({p for _s, _e, p in group if p})
        if overlap_text:
            prev_pages = {p for _s, _e, p in groups[idx - 1] if p}
            pages = sorted(set(pages) | prev_pages)
        plan.chunks.append(Chunk(index=idx + 1, text=text, start=span_start, end=span_end,
                                 pages=pages, overlap_chars=len(overlap_text)))
    if plan.scaled:
        plan.note += f"；实际 {plan.count} 块"
    return plan


def body_chars_needs_scaling(body_chars: int, chunk_chars: int, max_chunks: int) -> bool:
    return body_chars > chunk_chars * max_chunks


def uncovered_ranges(body_len: int, chunks: list[Chunk]) -> list[tuple[int, int]]:
    """正文中**没有被任何分块覆盖**的区间（应恒为空 —— "不丢尾段"的机器可验证形式）。"""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for chunk in sorted(chunks, key=lambda c: c.start):
        if chunk.start > cursor:
            gaps.append((cursor, chunk.start))
        cursor = max(cursor, chunk.end)
    if cursor < body_len:
        gaps.append((cursor, body_len))
    return [(s, e) for s, e in gaps if e > s]


def format_points(partials: list[str], chunks: list[Chunk]) -> str:
    """把分段要点拼成 reduce 的输入（标注"第几部分 / 哪些分 P"，便于模型按时间线合并）。"""
    blocks: list[str] = []
    for idx, text in enumerate(partials):
        label = f"### 第 {idx + 1}/{len(partials)} 部分"
        if idx < len(chunks):
            label += f"（分 P：{chunks[idx].pages_label()}）"
        blocks.append(f"{label}\n{str(text).strip()}")
    return "\n\n".join(blocks)


__all__ = [
    "DEFAULT_OVERLAP_CHARS",
    "PAGE_HEADING_RE",
    "Chunk",
    "ChunkPlan",
    "body_chars_needs_scaling",
    "format_points",
    "split_header",
    "split_transcript",
    "uncovered_ranges",
]
