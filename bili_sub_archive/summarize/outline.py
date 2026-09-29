"""受控层级大纲：把模型输出解析成有限深度/有限节点数的树。

计划 3.3：**"LLM 输出先解析为受控层级大纲，转义 Mermaid 特殊字符后写 ``mindmap.mmd``"**。
也就是说模型只负责"内容"，结构由本模块与 :mod:`bili_sub_archive.summarize.mermaid` 决定：

- 深度上限（``max_depth``）、节点上限（``max_nodes``）、标签字数上限（``label_chars``）
  都在这里强制执行 —— 模型给出 5 层 200 个节点也不会把导图撑爆；
- 模型没按格式给大纲时，:func:`derive_outline` 用**确定性规则**从摘要段落派生一份
  （首句作为节点），并在产物里标注 ``outline_source = derived``，不假装是模型给的；
- 解析失败不等于总结失败：总结（``summary.md``）照常保存，导图用派生大纲兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 大纲所在小节的名字（大小写不敏感，命中其一即可）
OUTLINE_TITLES = ("大纲", "思维导图", "导图", "outline", "mindmap")
#: 摘要所在小节
SUMMARY_TITLES = ("摘要", "总结", "summary")

_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
_LIST_RE = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])\s+(.*)$")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")
_LEAD_LABEL_RE = re.compile(r"^(主题|标题|核心|中心)[:：]\s*")

MAX_PARAGRAPH_NODES = 8


@dataclass
class OutlineNode:
    """大纲节点（``children`` 保持文档顺序）。"""

    label: str
    children: list["OutlineNode"] = field(default_factory=list)
    source: str = "llm"          # llm | derived

    @property
    def node_count(self) -> int:
        return 1 + sum(child.node_count for child in self.children)

    @property
    def depth(self) -> int:
        return 1 + max((child.depth for child in self.children), default=0)

    def walk(self, level: int = 0):
        yield level, self
        for child in self.children:
            yield from child.walk(level + 1)

    def to_json(self) -> dict:
        return {"label": self.label, "children": [c.to_json() for c in self.children]}


# --------------------------------------------------------------------------- #
# 标签清理
# --------------------------------------------------------------------------- #
def clean_label(text: str, *, limit: int = 24) -> str:
    """去掉 markdown 装饰、编号与多余空白，并截断到 ``limit`` 字。"""
    raw = str(text or "")
    raw = raw.replace("\r", " ").replace("\n", " ")
    raw = _EMPHASIS_RE.sub("", raw)
    raw = raw.strip().strip("-—·:：,，。;；")
    raw = re.sub(r"\s+", " ", raw).strip()
    limit = max(2, int(limit or 24))
    if len(raw) > limit:
        raw = raw[: max(1, limit - 1)].rstrip() + "…"
    return raw


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """按 ``##`` 级标题切小节，返回 ``[(标题, 正文)]``（标题为空表示前言）。"""
    lines = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) <= 2:
            sections.append((match.group(2).strip(), []))
            continue
        sections[-1][1].append(line)
    return [(title, "\n".join(body).strip()) for title, body in sections]


def _pick_section(markdown: str, names: tuple[str, ...]) -> str:
    for title, body in _split_sections(markdown):
        low = title.lower()
        if any(name in low for name in names):
            return body
    return ""


# --------------------------------------------------------------------------- #
# 解析模型输出
# --------------------------------------------------------------------------- #
def parse_outline_items(text: str) -> list[tuple[int, str]]:
    """把缩进列表解析为 ``[(缩进宽度, 标签)]``（保持文档顺序）。"""
    items: list[tuple[int, str]] = []
    for line in str(text or "").replace("\r", "").split("\n"):
        match = _LIST_RE.match(line)
        if not match:
            continue
        indent = match.group(1).replace("\t", "  ")
        label = match.group(2).strip()
        if label:
            items.append((len(indent), label))
    return items


def build_tree(items: list[tuple[int, str]], *, max_depth: int, max_nodes: int,
               label_chars: int, source: str = "llm") -> OutlineNode | None:
    """缩进列表 → 受控树（按缩进栈确定父子关系，深度与节点数都设上限）。"""
    if not items:
        return None
    max_depth = max(1, int(max_depth or 3))
    max_nodes = max(2, int(max_nodes or 60))

    # 缩进宽度去重排序，映射为层级：同一缩进 = 同一层
    widths = sorted({width for width, _label in items})
    level_of = {width: idx for idx, width in enumerate(widths)}
    # 缩进种类多于深度上限时，把过深的层压平到最后一层（不丢节点）
    capped = {width: min(level_of[width], max_depth - 1) for width in widths}

    root_label = ""
    roots: list[OutlineNode] = []
    stack: list[tuple[int, OutlineNode]] = []
    used = 0
    for width, raw in items:
        label = clean_label(raw, limit=label_chars)
        if not label:
            continue
        level = capped[width]
        if not roots and not root_label:
            lead = _LEAD_LABEL_RE.match(label)
            if lead:                       # 首行是"主题：xxx" → 当作导图根
                root_label = clean_label(label[lead.end():], limit=label_chars) or label
                continue
        if used >= max_nodes:
            break
        node = OutlineNode(label=label, source=source)
        used += 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))

    if not roots and not root_label:
        return None
    root = OutlineNode(label=root_label or "视频总结", children=roots, source=source)
    _dedupe(root)
    return root


def _dedupe(node: OutlineNode) -> None:
    """同一层去掉重复标签（模型偶尔会把同一句话写两遍）。"""
    seen: set[str] = set()
    kept: list[OutlineNode] = []
    for child in node.children:
        if child.label in seen:
            continue
        seen.add(child.label)
        _dedupe(child)
        kept.append(child)
    node.children = kept


def extract_outline(markdown: str, *, max_depth: int = 3, max_nodes: int = 60,
                    label_chars: int = 24) -> OutlineNode | None:
    """从模型输出里取"大纲"小节；没有该小节时退回全文列表（≥3 条才认）。"""
    section = _pick_section(markdown, OUTLINE_TITLES)
    items = parse_outline_items(section) if section else []
    if not items:
        all_items = parse_outline_items(markdown)
        items = all_items if len(all_items) >= 3 else []
    if not items:
        return None
    return build_tree(items, max_depth=max_depth, max_nodes=max_nodes,
                      label_chars=label_chars, source="llm")


# --------------------------------------------------------------------------- #
# 派生兜底
# --------------------------------------------------------------------------- #
def derive_outline(markdown: str, *, title: str = "", max_depth: int = 3, max_nodes: int = 60,
                   label_chars: int = 24) -> OutlineNode:
    """模型没给出可用大纲时，用确定性规则从摘要派生（``outline_source = derived``）。

    规则：``## 摘要`` 小节里的**列表项**优先；没有列表就用各段首句（最多 8 个）。
    这条路径不调用模型、结果可复现，因此导图始终能产出。
    """
    section = _pick_section(markdown, SUMMARY_TITLES) or str(markdown or "")
    items = parse_outline_items(section)
    if items:
        tree = build_tree(items, max_depth=max_depth, max_nodes=max_nodes,
                          label_chars=label_chars, source="derived")
        if tree is not None:
            root_label = clean_label(title, limit=label_chars) or tree.label
            tree.label = root_label
            tree.source = "derived"
            return tree

    labels: list[str] = []
    for paragraph in re.split(r"\n\s*\n", section):
        text = paragraph.strip()
        if not text or text.startswith("#"):
            continue
        text = _EMPHASIS_RE.sub("", text)
        first = re.split(r"[。！？!?；;]", text, maxsplit=1)[0]
        label = clean_label(first, limit=label_chars)
        if label:
            labels.append(label)
        if len(labels) >= MAX_PARAGRAPH_NODES:
            break

    children = [OutlineNode(label=label, source="derived") for label in labels]
    root_label = clean_label(title, limit=label_chars) or "视频总结"
    return OutlineNode(label=root_label, children=children, source="derived")


__all__ = [
    "MAX_PARAGRAPH_NODES",
    "OUTLINE_TITLES",
    "SUMMARY_TITLES",
    "OutlineNode",
    "build_tree",
    "clean_label",
    "derive_outline",
    "extract_outline",
    "parse_outline_items",
]
