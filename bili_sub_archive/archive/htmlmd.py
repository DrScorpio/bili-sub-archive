"""HTML → Markdown（专栏旧格式 read/cv 用，纯标准库 ``html.parser`` 实现）。

需求 3.4.1：保存标题、作者、发布时间、原链接、正文及正文中可获取的图片，
**保留基本段落、标题、列表与图片顺序**。

不引入 markdownify/html2text 等第三方依赖（依赖面越小越好，且本项目要求
"不把未验证接口/依赖散布到业务代码中"）。实现策略：

- 块级标签（``p``/``div``/``h1``~``h6``/``li``/``blockquote``/``figcaption``）触发换行；
- 行内标签只做轻量强调（``strong``/``b`` → ``**``，``em``/``i`` → ``*``，``a`` → ``[文字](链接)``）；
- ``img`` 按出现顺序收集，交给调用方下载后改写为相对路径；
- ``script``/``style``/``iframe`` 内容整体丢弃；
- 表格退化为逐行文本（专栏正文里的表格极少，保内容不保版式）。
"""

from __future__ import annotations

from html.parser import HTMLParser

BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "blockquote", "figure", "figcaption", "pre", "tr", "table", "hr", "dl", "dt", "dd",
}
HEADING_TAGS = {f"h{i}": i for i in range(1, 7)}
SKIP_TAGS = {"script", "style", "iframe", "noscript", "svg"}
EMPHASIS_TAGS = {"strong": "**", "b": "**", "em": "*", "i": "*"}
LIST_TAGS = {"ul", "ol"}


class _MarkdownExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.images: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0
        self._list_stack: list[dict] = []
        self._quote_depth = 0
        self._in_pre = False
        self._link_stack: list[str] = []

    # ---------------- 输出辅助 ----------------
    def _flush(self, *, force: bool = False) -> None:
        text = "".join(self._buf)
        self._buf = []
        text = text.strip()
        if text:
            prefix = "> " * self._quote_depth
            self.lines.append(prefix + text)
        elif force and self.lines and self.lines[-1] != "":
            self.lines.append("")

    def _blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def _close_emphasis(self, marker: str) -> None:
        """关闭强调标记：把标记前的尾随空白挪到标记之后，避免 ``**加粗 **``。"""
        tail = ""
        while self._buf:
            chunk = self._buf[-1]
            if not chunk:
                self._buf.pop()
                continue
            stripped = chunk.rstrip()
            tail = chunk[len(stripped):] + tail
            self._buf[-1] = stripped
            if stripped:
                break
            self._buf.pop()
        self._buf.append(marker + tail)

    # ---------------- 解析回调 ----------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._flush()
            return
        if tag == "img":
            src = attrs_d.get("src") or attrs_d.get("data-src") or ""
            if src:
                self.images.append(src)
                self._buf.append(f"![]({{{{IMG:{len(self.images) - 1}}}}})")
            return
        if tag == "hr":
            self._flush()
            self.lines.append("---")
            self._blank()
            return
        if tag in HEADING_TAGS:
            self._flush()
            self._blank()
            self._buf.append("#" * (HEADING_TAGS[tag] + 1) + " ")
            return
        if tag in LIST_TAGS:
            self._flush()
            self._blank()
            self._list_stack.append({"ordered": tag == "ol", "index": 0})
            return
        if tag == "li":
            self._flush()
            depth = max(0, len(self._list_stack) - 1)
            indent = "  " * depth
            if self._list_stack and self._list_stack[-1]["ordered"]:
                self._list_stack[-1]["index"] += 1
                self._buf.append(f"{indent}{self._list_stack[-1]['index']}. ")
            else:
                self._buf.append(f"{indent}- ")
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth += 1
            return
        if tag == "pre":
            self._flush()
            self._in_pre = True
            self._buf.append("```\n")
            return
        if tag == "a":
            href = attrs_d.get("href") or ""
            self._link_stack.append(href)
            self._buf.append("[")
            return
        if tag in EMPHASIS_TAGS:
            self._buf.append(EMPHASIS_TAGS[tag])
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._link_stack:
            href = self._link_stack.pop()
            self._buf.append(f"]({href})" if href else "]")
            return
        if tag in EMPHASIS_TAGS:
            self._close_emphasis(EMPHASIS_TAGS[tag])
            return
        if tag in LIST_TAGS:
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            self._blank()
            return
        if tag == "li":
            self._flush()
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth = max(0, self._quote_depth - 1)
            self._blank()
            return
        if tag == "pre":
            self._buf.append("\n```")
            self._flush()
            self._in_pre = False
            self._blank()
            return
        if tag in HEADING_TAGS or tag in BLOCK_TAGS:
            self._flush(force=True)
            self._blank()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._in_pre:
            self._buf.append(data.rstrip("\n") + "\n")
            return
        self._buf.append(" ".join(data.split()) + " " if data.strip() else "")

    def result(self) -> str:
        self._flush()
        out: list[str] = []
        blank = False
        for line in self.lines:
            if not line.strip():
                if not blank:
                    out.append("")
                blank = True
                continue
            blank = False
            out.append(line)
        text = "\n".join(out).strip()
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text


def html_to_markdown(html: str) -> tuple[str, list[str]]:
    """返回 ``(markdown, 图片 src 列表)``。

    markdown 中的图片先写成占位符 ``![]({{IMG:n}})``，调用方在拿到本地相对路径后
    用 :func:`apply_image_paths` 替换 —— 这样"下载失败的图片保留原链接"也能实现。
    """
    parser = _MarkdownExtractor()
    parser.feed(str(html or ""))
    parser.close()
    return parser.result(), parser.images


def apply_image_paths(markdown: str, mapping: dict[int, str], srcs: list[str]) -> str:
    """把占位符替换为本地相对路径；未命中的索引保留原始链接（需求 3.4.1）。"""
    out = markdown
    for idx, src in enumerate(srcs):
        token = "{{IMG:%d}}" % idx
        replacement = mapping.get(idx) or src
        out = out.replace(token, replacement)
    return out
