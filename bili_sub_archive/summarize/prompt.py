"""总结用的 prompt 模板：内置默认 + 用户自定义文件 + 指纹。

需求 2「总结配置」要求"用户配置接口地址、模型、凭据及**自定义 prompt**"，
需求 3.3.3 要求"保留使用的模型、**prompt 版本**、生成时间和失败状态"。

设计：

- 内置默认 prompt 分四种角色：``system``（角色约束）、``map``（分段要点）、
  ``reduce``（要点 → 总结 + 大纲）、``full``（单块直接总结，结构与 reduce 相同）；
- 自定义 prompt 文件支持**两种写法**（缺哪节用哪节的内置默认）：

  INI 风格（任意扩展名）：

  ```text
  [system]
  你是一个……（可选）
  [map]
  这一段讲的是……{transcript}
  [reduce]
  以下是分段要点：{points}
  [full]
  以下是完整文字稿：{transcript}
  ```

  真 TOML（``prompt_file = "prompts/summary.toml"`` 这种命名下更自然；多行文本用
  TOML 的三引号字符串）：

  ```toml
  system = "你是一个……"
  map = "这一段讲的是……{transcript}"
  ```

  文件里**一个分节头都没有**时，整个文件当作 ``full`` 模板（同时兼容"只想改最终总结"的用法）。
  TOML 只在"顶层键全是字符串、且都在 system/map/reduce/full 之内"时才按 TOML 解释，
  其余情况交回 INI 解析器 —— 一段普通散文不会被误判成 TOML。
- 占位符用 ``str.replace`` 而非 ``format``：用户 prompt 里出现 ``{`` ``}``（例如要求模型
  输出 JSON）不会被当成格式化语法而报错。
- :attr:`PromptSpec.fingerprint` 是四个模板的 sha256 前 12 位；它与模型名、端点主机一起
  构成"输入指纹"，写进 ``metadata.json`` 与 ``summary.md``，用于判断"要不要重做总结"。

**只发送文字稿文本**：模板里的占位符只有文字稿与统计信息，没有 Cookie、媒体地址。
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: prompt 版本（结构变化时递增；写进产物便于溯源）
PROMPT_VERSION = "stage3-v1"

#: 允许出现在模板里的占位符（校验与 ``check`` 展示用；``index`` 只有 ``map`` 会替换）
PLACEHOLDERS = ("transcript", "points", "chars", "chunks", "pages", "title", "author", "index")

DEFAULT_SYSTEM = (
    "你是一个严谨的中文视频内容分析师。你只依据用户提供的视频文字稿工作："
    "不臆造原文没有的事实、数字、人名或结论；原文含糊的地方宁可略过。"
    "输出使用简体中文，使用 Markdown。"
)

DEFAULT_MAP = (
    "以下是一个视频文字稿的第 {index}/{chunks} 部分"
    "（涉及分 P：{pages}；本部分约 {chars} 字）。\n\n"
    "----- 文字稿开始 -----\n{transcript}\n----- 文字稿结束 -----\n\n"
    "请只针对**这一部分**提取关键信息，输出 3~8 条要点：\n"
    "- 每条一行，以 \"- \" 开头；\n"
    "- 保留原文出现的数字、专有名词、时间与结论；\n"
    "- 不要写开场白或收尾语，不要总结整个视频，不要补充原文没有的内容。"
)

#: ``reduce`` 与 ``full`` 共用同一份结构约束，保证"总结 + 受控大纲"两个产物都拿得到
_STRUCTURE_RULES = (
    "严格按下面的 Markdown 结构输出，除这两节外不要输出任何其他文字：\n\n"
    "## 摘要\n"
    "（3~8 段，覆盖主线、关键数据与结论；不要出现\"第几部分\"\"上一段\"这类分块痕迹）\n\n"
    "## 大纲\n"
    "（用缩进列表表示层级：一级缩进 2 个空格，最多 3 层，6~30 个节点，"
    "每个节点不超过 20 个字）\n"
    "- 主题：<一句话主题>\n"
    "  - <一级要点>\n"
    "    - <二级细节>\n"
)

DEFAULT_REDUCE = (
    "以下是一个视频各部分文字稿的**分段要点**（共 {chunks} 段，已按时间顺序排列；"
    "覆盖分 P：{pages}；原文字稿约 {chars} 字）。\n\n"
    "----- 分段要点开始 -----\n{points}\n----- 分段要点结束 -----\n\n"
    "请基于这些要点写出该视频的完整总结。\n\n" + _STRUCTURE_RULES
    + "\n要求：只使用上面提供的内容，不要遗漏最后几段要点里的结论。"
)

DEFAULT_FULL = (
    "以下是一个视频的完整文字稿（标题：{title}；作者：{author}；约 {chars} 字；"
    "分 P：{pages}）。\n\n"
    "----- 文字稿开始 -----\n{transcript}\n----- 文字稿结束 -----\n\n"
    "请阅读全文后写出该视频的总结。\n\n" + _STRUCTURE_RULES
    + "\n要求：结尾部分（最后几个分 P）的内容同样重要，不要只总结开头。"
)

DEFAULT_SECTIONS = {
    "system": DEFAULT_SYSTEM,
    "map": DEFAULT_MAP,
    "reduce": DEFAULT_REDUCE,
    "full": DEFAULT_FULL,
}


class PromptError(ValueError):
    """prompt 文件不可用（配置错误，应在 ``check`` 阶段暴露）。"""


@dataclass(frozen=True)
class PromptSpec:
    """四个角色的模板 + 来源 + 指纹。"""

    system: str = DEFAULT_SYSTEM
    map: str = DEFAULT_MAP
    reduce: str = DEFAULT_REDUCE
    full: str = DEFAULT_FULL
    source: str = "builtin"
    version: str = PROMPT_VERSION
    overridden: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        """四模板 + 版本号的指纹（前 12 位十六进制）。"""
        blob = json.dumps(
            {"version": self.version, "system": self.system, "map": self.map,
             "reduce": self.reduce, "full": self.full},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    def describe(self) -> str:
        if self.source == "builtin":
            return f"内置 prompt（{self.version}，指纹 {self.fingerprint}）"
        extra = f"，覆盖 {', '.join(self.overridden)}" if self.overridden else ""
        return f"{self.source}（{self.version}，指纹 {self.fingerprint}{extra}）"

    # ---------------- 渲染 ---------------- #
    def _messages(self, user: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        system = (self.system or "").strip()
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user.strip()})
        return messages

    def render_full(self, *, transcript: str, chars: int, pages: str, title: str,
                    author: str = "") -> list[dict[str, str]]:
        return self._messages(_fill(self.full, transcript=transcript, chars=chars,
                                    pages=pages, title=title, author=author,
                                    chunks=1, points="", index=1))

    def render_map(self, *, transcript: str, chars: int, pages: str, index: int,
                   chunks: int, title: str = "", author: str = "") -> list[dict[str, str]]:
        return self._messages(_fill(self.map, transcript=transcript, chars=chars,
                                    pages=pages, index=index, chunks=chunks,
                                    title=title, author=author, points=""))

    def render_reduce(self, *, points: str, chunks: int, pages: str, chars: int,
                      title: str = "", author: str = "") -> list[dict[str, str]]:
        return self._messages(_fill(self.reduce, points=points, chunks=chunks, pages=pages,
                                    chars=chars, title=title, author=author, transcript=""))


def _fill(template: str, **values) -> str:
    out = str(template)
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _parse_toml_prompt(text: str) -> dict[str, str] | None:
    """把"真 TOML"形式的 prompt 读成分节字典；不像 prompt TOML 时返回 ``None``。

    保守判定（宁可回落到 INI 解析，也不误判）：必须是合法 TOML、顶层全是字符串、
    键都在 :data:`DEFAULT_SECTIONS` 之内、且至少一节非空。这样 ``prompts/summary.toml``
    可以写成 ``map = \"\"\"…{transcript}…\"\"\"``，而一段普通散文仍然走 INI 路径。
    """
    try:
        data = tomllib.loads(str(text))
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    if any(not isinstance(value, str) for value in data.values()):
        return None
    if set(data) - set(DEFAULT_SECTIONS):
        return None
    sections = {key: value.strip() for key, value in data.items() if value.strip()}
    return sections or None


def parse_prompt_text(text: str) -> dict[str, str]:
    """解析 prompt 文本：优先真 TOML，其次 ``[section]`` 分节；无分节头则整篇作为 ``full``。"""
    toml_sections = _parse_toml_prompt(text)
    if toml_sections is not None:
        return toml_sections
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    saw_header = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 2:
            name = stripped[1:-1].strip().lower()
            current = name
            saw_header = True
            sections.setdefault(name, [])
            continue
        if current is None:
            continue
        sections[current].append(line)
    if not saw_header:
        return {"full": str(text)}
    return {name: "\n".join(body).strip() for name, body in sections.items()}


def load_prompt_spec(path: str | Path | None = None, *, base: Path | None = None) -> PromptSpec:
    """读取自定义 prompt 文件；``path`` 为空时返回内置模板。

    文件不存在、分节名未知、缺少必要占位符都抛 :class:`PromptError`（配置错误）。
    """
    if not path or not str(path).strip():
        return PromptSpec()
    target = Path(str(path))
    if not target.is_absolute() and base is not None:
        target = Path(base) / target
    if not target.is_file():
        raise PromptError(f"prompt 文件不存在：{target}")

    try:
        raw = target.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise PromptError(f"prompt 文件读取失败 {target}：{exc}") from exc

    parsed = parse_prompt_text(raw)
    unknown = sorted(set(parsed) - set(DEFAULT_SECTIONS))
    if unknown:
        raise PromptError(
            f"prompt 文件 {target} 含未知分节 {', '.join(unknown)}；"
            f"可用分节：{', '.join(DEFAULT_SECTIONS)}"
        )
    resolved = dict(DEFAULT_SECTIONS)
    overridden = tuple(sorted(name for name, body in parsed.items() if body))
    for name, body in parsed.items():
        if body:
            resolved[name] = body

    spec = PromptSpec(system=resolved["system"], map=resolved["map"],
                      reduce=resolved["reduce"], full=resolved["full"],
                      source=f"file:{target.name}", overridden=overridden)
    _validate(spec, target)
    return spec


#: 每个分节必须保留的占位符（缺了就等于"文字稿不会发给模型"）
REQUIRED_PLACEHOLDER = {"map": "transcript", "reduce": "points", "full": "transcript"}


def _validate(spec: PromptSpec, target: Path) -> None:
    """逐节校验：**被覆盖**的分节必须带自己的关键占位符（未覆盖的用内置默认，天然合规）。"""
    for name in spec.overridden:
        need = REQUIRED_PLACEHOLDER.get(name)
        if need and f"{{{need}}}" not in getattr(spec, name):
            raise PromptError(
                f"prompt 文件 {target} 的 [{name}] 模板缺少 {{{need}}} 占位符，"
                f"文字稿不会被发送给模型（可用占位符：{', '.join('{' + p + '}' for p in PLACEHOLDERS)}）"
            )


def prompt_placeholders(spec: PromptSpec) -> dict[str, bool]:
    """各模板里出现了哪些占位符（``check`` 展示用）。"""
    return {
        name: any(f"{{{key}}}" in getattr(spec, name) for key in PLACEHOLDERS)
        for name in DEFAULT_SECTIONS
    }


__all__ = [
    "DEFAULT_FULL",
    "DEFAULT_MAP",
    "DEFAULT_REDUCE",
    "DEFAULT_SECTIONS",
    "DEFAULT_SYSTEM",
    "PLACEHOLDERS",
    "PROMPT_VERSION",
    "PromptError",
    "PromptSpec",
    "load_prompt_spec",
    "parse_prompt_text",
    "prompt_placeholders",
]
