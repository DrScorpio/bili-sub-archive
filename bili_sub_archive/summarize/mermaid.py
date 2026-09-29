"""大纲 → Mermaid ``mindmap`` 源文件（确定性序列化 + 特殊字符转义）。

需求 3.3.4：保存可编辑的 Mermaid mindmap 源文件 ``mindmap.mmd``。

**为什么是确定性序列化**：计划 3.3 与风险表都点名"LLM 直接写 Mermaid 容易语法出错"。
因此模型只产出缩进大纲（见 :mod:`bili_sub_archive.summarize.outline`），由本模块生成语法 ——
同样的树永远得到同样的文件，不依赖模型"记得怎么写 mermaid"。

**转义策略**：Mermaid mindmap 用 ``( ) [ ] { }`` 决定节点形状、``%%`` 是注释、
``;`` 是语句分隔符。中文标签里这些字符多数是"半角混写"（例如 ``(下)``、``1:2``），
统一换成**全角**（``（``、``：``）既保持可读性又彻底避开语法歧义；``\\``、反引号、
``#``、``%``、``|``、``<``、``>`` 同样处理。换行压成空格，超长标签截断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .outline import OutlineNode, clean_label

MMD_NAME = "mindmap.mmd"

#: 半角 → 全角（避开 Mermaid 语法字符）
_CHAR_MAP = {
    "(": "（", ")": "）", "[": "【", "]": "】", "{": "｛", "}": "｝",
    '"': "”", "'": "’", ":": "：", ";": "；", "#": "＃", "%": "％",
    "|": "｜", "<": "＜", ">": "＞", "\\": "／", "`": "｀", "&": "＆",
    "*": "＊", "@": "＠", "$": "＄", "^": "＾", "~": "～", "=": "＝",
}

_WS_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class MermaidResult:
    """一次序列化的结果与统计。"""

    text: str = ""
    nodes: int = 0
    depth: int = 0
    dropped: int = 0
    outline_source: str = "llm"
    notes: list[str] = field(default_factory=list)


def sanitize_label(text: str, *, limit: int = 24) -> str:
    """清掉 markdown 装饰 + 转义 Mermaid 语法字符 + 截断。"""
    # 先用大上限过一遍 clean_label（去 markdown 装饰），截断留到转义之后再做
    base = clean_label(str(text or ""), limit=10_000)
    base = _CONTROL_RE.sub("", str(base))
    base = _WS_RE.sub(" ", base).strip()
    out = "".join(_CHAR_MAP.get(ch, ch) for ch in base)
    out = out.strip().strip("－—·")
    out = _WS_RE.sub(" ", out).strip()
    if not out:
        out = "未命名"
    limit = max(2, int(limit or 24))
    if len(out) > limit:
        out = out[: max(1, limit - 1)].rstrip() + "…"
    return out


def to_mermaid(root: OutlineNode, *, title: str = "", max_nodes: int = 60,
               max_depth: int = 3, label_chars: int = 24,
               outline_source: str = "") -> MermaidResult:
    """受控树 → ``mindmap`` 文本（节点数/深度再次兜底，双保险）。"""
    max_nodes = max(2, int(max_nodes or 60))
    max_depth = max(1, int(max_depth or 3))
    label_chars = max(2, int(label_chars or 24))

    source = outline_source or getattr(root, "source", "llm") or "llm"
    header = ["%% bili-sub-archive 思维导图（确定性序列化，非模型直出 Mermaid）"]
    if title:
        header.append(f"%% 视频：{sanitize_label(title, limit=120)}")
    header.append(f"%% 大纲来源：{source}（llm = 模型输出，derived = 由摘要段落派生）")
    lines = header + ["mindmap", f"  root(({sanitize_label(root.label, limit=label_chars)}))"]

    used = 1
    dropped = 0
    max_depth_used = 1

    def emit(node: OutlineNode, level: int, depth: int) -> None:
        nonlocal used, dropped, max_depth_used
        for child in node.children:
            if depth > max_depth or used >= max_nodes:
                dropped += _count(child)
                continue
            used += 1
            max_depth_used = max(max_depth_used, depth)
            lines.append("  " * level + sanitize_label(child.label, limit=label_chars))
            emit(child, level + 1, depth + 1)

    emit(root, 2, 2)
    notes: list[str] = []
    if dropped:
        notes.append(f"节点数超上限（max_nodes={max_nodes} / max_depth={max_depth}），"
                     f"已省略 {dropped} 个节点")
    return MermaidResult(text="\n".join(lines) + "\n", nodes=used, depth=max_depth_used,
                         dropped=dropped, outline_source=source, notes=notes)


def _count(node: OutlineNode) -> int:
    return 1 + sum(_count(child) for child in node.children)


__all__ = ["MMD_NAME", "MermaidResult", "sanitize_label", "to_mermaid"]
