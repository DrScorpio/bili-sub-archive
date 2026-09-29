"""视频总结与思维导图（阶段 3）：编排层。

需求 3.3.3 / 3.3.4 的落地：

```text
transcript.txt（阶段 2 产物，按 P 分节）
  → 分块（chunk.py：不丢尾段，块数超上限就放大每块）
  → 逐块要点（prompt.map）
  → 合并总结 + 受控大纲（prompt.reduce；单块时直接 prompt.full）
  → summary.md（模型 / prompt 指纹 / 生成时间 / 分块与 token 统计）
  → 大纲解析（outline.py：深度、节点数、标签长度都设上限）
  → mindmap.mmd（mermaid.py：确定性序列化 + 全角转义）
  → mindmap.png（mermaid_cli.py：mmdc 渲染 + PNG 头校验；失败保留 .mmd 可重试）
```

关键取舍：

- **文字不足就不编**（需求 3.3.6）：无文字 → ``skipped: no_transcript``；
  文字少于 ``[summary] min_chars`` → ``skipped: insufficient_text``；
- **未配置 LLM 不算失败**（计划第 4 节）：``skipped: llm_not_configured``，归档与文字稿照常，
  配置好之后 ``retry`` 直接补做（``models.stale_skip_map`` 会把这条跳过视为"应重做"）；
- **不重复花钱**：总结与导图各自有"输入指纹"（prompt 指纹 + 模型 + 端点主机 + 分块参数 +
  文字稿摘要），指纹一致且产物存在就复用，不再调用模型；prompt/模型变了才重做；
- **失败可独立重试**：``summary`` 与 ``mindmap`` 是两个步骤；渲染失败只影响 ``mindmap``，
  ``summary.md`` 与 ``mindmap.mmd`` 都保留。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import atomic_write_text, ensure_dir
from ..timeutil import now_iso
from .chunk import ChunkPlan, format_points, split_header, split_transcript
from .llm import KIND_LLM_INVALID, ChatClient, ChatResult, LlmUnavailable, build_chat_client
from .mermaid import MMD_NAME, MermaidResult, to_mermaid
from .mermaid_cli import PNG_NAME, RenderOutcome, probe_mmdc, render_mindmap
from .outline import OutlineNode, derive_outline, extract_outline
from .prompt import PromptError, PromptSpec, load_prompt_spec

SUMMARY_MD = "summary.md"
MINDMAP_MMD = MMD_NAME
MINDMAP_PNG = PNG_NAME

#: ``summary.md`` 里正文的起止标记（复用已有产物时按标记切出正文）
SUMMARY_BEGIN = "<!-- bili-sub-archive:summary:begin -->"
SUMMARY_END = "<!-- bili-sub-archive:summary:end -->"
#: 改名前的旧标记（项目名还是 SubVideo 时写出的产物）：**只读兼容**，新产物不再写。
LEGACY_SUMMARY_BEGIN = "<!-- subvideo:summary:begin -->"
LEGACY_SUMMARY_END = "<!-- subvideo:summary:end -->"
#: 读取时按序尝试的标记对：新标记优先，旧产物仍能准确切回正文。
_MARKER_PAIRS = (
    (SUMMARY_BEGIN, SUMMARY_END),
    (LEGACY_SUMMARY_BEGIN, LEGACY_SUMMARY_END),
)

# 跳过/失败原因（写进 metadata 的 ``steps.<name>.reason``）
REASON_NO_TRANSCRIPT = "no_transcript"
REASON_INSUFFICIENT_TEXT = "insufficient_text"
REASON_LLM_NOT_CONFIGURED = "llm_not_configured"
REASON_SUMMARY_DISABLED = "summary_disabled"
REASON_MINDMAP_DISABLED = "mindmap_disabled"
REASON_SUMMARY_FAILED = "summary_failed"
REASON_LLM_FAILED = "llm_failed"
REASON_RENDER_FAILED = "render_failed"
REASON_DEPENDENCY_MISSING = "dependency_missing"

#: reduce 阶段最多做几轮"要点再压缩"（防止超长输入无限套娃）
MAX_REDUCE_ROUNDS = 3


# --------------------------------------------------------------------------- #
# 计划对象
# --------------------------------------------------------------------------- #
@dataclass
class SummaryPlan:
    """一个视频条目的总结 + 导图结果（可直接序列化进 ``metadata.json``）。"""

    status: str = "done"                 # done | skipped | failed
    reason: str = ""
    error_kind: str = ""
    message: str = ""

    summary_md: str = ""
    text: str = ""                       # 总结正文（不写进 metadata）
    model: str = ""
    client: str = ""
    endpoint_host: str = ""
    prompt_source: str = ""
    prompt_fingerprint: str = ""
    signature: str = ""
    generated_at: str = ""

    transcript_chars: int = 0
    transcript_sha1: str = ""
    chunk_plan: dict = field(default_factory=dict)
    chunks: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated_calls: int = 0

    outline_source: str = ""
    outline_nodes: int = 0
    outline_depth: int = 0
    outline_dropped: int = 0

    mindmap_status: str = "pending"      # pending | done | skipped | failed
    mindmap_reason: str = ""
    mindmap_error_kind: str = ""
    mindmap_message: str = ""
    mindmap_mmd: str = ""
    mindmap_png: str = ""
    mindmap_signature: str = ""
    mindmap_nodes: int = 0
    mindmap_width: int = 0
    mindmap_height: int = 0
    mindmap_bytes: int = 0

    reused: bool = False
    mindmap_reused: bool = False
    notes: list[str] = field(default_factory=list)

    # ---------------- 序列化 ---------------- #
    def to_json(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "error_kind": self.error_kind,
            "message": self.message,
            "summary_md": self.summary_md,
            "model": self.model,
            "client": self.client,
            "endpoint_host": self.endpoint_host,
            "prompt": {"source": self.prompt_source, "fingerprint": self.prompt_fingerprint},
            "signature": self.signature,
            "generated_at": self.generated_at,
            "transcript": {"chars": self.transcript_chars, "sha1": self.transcript_sha1},
            "chunk_plan": dict(self.chunk_plan),
            "chunks": self.chunks,
            "calls": self.calls,
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
            "truncated_calls": self.truncated_calls,
            "outline": {"source": self.outline_source, "nodes": self.outline_nodes,
                        "depth": self.outline_depth, "dropped": self.outline_dropped},
            "mindmap": {
                "status": self.mindmap_status,
                "reason": self.mindmap_reason,
                "error_kind": self.mindmap_error_kind,
                "message": self.mindmap_message,
                "mmd": self.mindmap_mmd,
                "png": self.mindmap_png,
                "signature": self.mindmap_signature,
                "nodes": self.mindmap_nodes,
                "width": self.mindmap_width,
                "height": self.mindmap_height,
                "bytes": self.mindmap_bytes,
                "reused": self.mindmap_reused,
            },
            "reused": self.reused,
            "notes": list(self.notes),
            "note": "不含 API 密钥；端点只记录主机名（计划 3.2）",
        }

    @classmethod
    def from_json(cls, data: dict | None) -> "SummaryPlan":
        data = data or {}
        mind = data.get("mindmap") or {}
        prompt = data.get("prompt") or {}
        transcript = data.get("transcript") or {}
        outline = data.get("outline") or {}
        tokens = data.get("tokens") or {}
        plan = cls(
            status=str(data.get("status") or "done"),
            reason=str(data.get("reason") or ""),
            error_kind=str(data.get("error_kind") or ""),
            message=str(data.get("message") or ""),
            summary_md=str(data.get("summary_md") or ""),
            model=str(data.get("model") or ""),
            client=str(data.get("client") or ""),
            endpoint_host=str(data.get("endpoint_host") or ""),
            prompt_source=str(prompt.get("source") or ""),
            prompt_fingerprint=str(prompt.get("fingerprint") or ""),
            signature=str(data.get("signature") or ""),
            generated_at=str(data.get("generated_at") or ""),
            transcript_chars=int(transcript.get("chars") or 0),
            transcript_sha1=str(transcript.get("sha1") or ""),
            chunk_plan=dict(data.get("chunk_plan") or {}),
            chunks=int(data.get("chunks") or 0),
            calls=int(data.get("calls") or 0),
            prompt_tokens=int(tokens.get("prompt") or 0),
            completion_tokens=int(tokens.get("completion") or 0),
            truncated_calls=int(data.get("truncated_calls") or 0),
            outline_source=str(outline.get("source") or ""),
            outline_nodes=int(outline.get("nodes") or 0),
            outline_depth=int(outline.get("depth") or 0),
            outline_dropped=int(outline.get("dropped") or 0),
            mindmap_status=str(mind.get("status") or "pending"),
            mindmap_reason=str(mind.get("reason") or ""),
            mindmap_error_kind=str(mind.get("error_kind") or ""),
            mindmap_message=str(mind.get("message") or ""),
            mindmap_mmd=str(mind.get("mmd") or ""),
            mindmap_png=str(mind.get("png") or ""),
            mindmap_signature=str(mind.get("signature") or ""),
            mindmap_nodes=int(mind.get("nodes") or 0),
            mindmap_width=int(mind.get("width") or 0),
            mindmap_height=int(mind.get("height") or 0),
            mindmap_bytes=int(mind.get("bytes") or 0),
            reused=bool(data.get("reused")),
            mindmap_reused=bool(mind.get("reused")),
            notes=list(data.get("notes") or []),
        )
        return plan

    # ---------------- 文案 ---------------- #
    def summary_line(self) -> str:
        if self.status != "done":
            return f"总结未完成（{self.reason or self.status}）"
        base = (f"总结 {self.transcript_chars} 字 → {self.chunks} 块，模型 {self.model}"
                f"（{self.client}），调用 {self.calls} 次")
        if self.reused:
            base += "；沿用已有产物（输入指纹未变）"
        return base

    def mindmap_line(self) -> str:
        if self.mindmap_status != "done":
            return f"导图未完成（{self.mindmap_reason or self.mindmap_status}）"
        size = f"{self.mindmap_width}×{self.mindmap_height}" if self.mindmap_width else "已渲染"
        return (f"导图 {self.mindmap_nodes} 节点 / {self.outline_depth} 层，PNG {size}"
                f"（{self.mindmap_bytes / 1024:.1f} KiB）")


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #
def transcript_digest(text: str) -> tuple[int, str]:
    blob = str(text or "").encode("utf-8")
    return len(str(text or "")), hashlib.sha1(blob).hexdigest()[:12]


def summary_signature(config, spec: PromptSpec, transcript_text: str) -> str:
    """总结的"输入指纹"：任一要素变化都应重做总结（计划 3.2：变更 prompt/模型只重做总结/导图）。"""
    chars, sha1 = transcript_digest(transcript_text)
    payload = {
        "prompt": spec.fingerprint,
        "model": str(getattr(config, "summary_model", "") or ""),
        "endpoint": str(getattr(config, "summary_base_url", "") or ""),
        "client": str(getattr(config, "summary_client", "auto") or "auto"),
        "chunk_chars": int(getattr(config, "summary_chunk_chars", 6000) or 6000),
        "max_chunks": int(getattr(config, "summary_max_chunks", 40) or 40),
        "overlap": int(getattr(config, "summary_overlap_chars", 200) or 0),
        "temperature": float(getattr(config, "summary_temperature", 0.2) or 0.0),
        "min_chars": int(getattr(config, "summary_min_chars", 200) or 0),
        "transcript": f"{chars}:{sha1}",
    }
    blob = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def mindmap_signature(config, summary_sig: str) -> str:
    payload = {
        "summary": summary_sig,
        "max_nodes": int(getattr(config, "mindmap_max_nodes", 60) or 60),
        "max_depth": int(getattr(config, "mindmap_max_depth", 3) or 3),
        "label_chars": int(getattr(config, "mindmap_label_chars", 24) or 24),
        "width": int(getattr(config, "mindmap_width", 1600) or 1600),
        "background": str(getattr(config, "mindmap_background", "white") or ""),
    }
    blob = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 总结
# --------------------------------------------------------------------------- #
def build_summary(
    *,
    transcript_text: str,
    config,
    entry_dir: Path,
    prompt_spec: PromptSpec | None = None,
    chat_client: ChatClient | None = None,
    api_key: str = "",
    title: str = "",
    author: str = "",
    logger=None,
) -> SummaryPlan:
    """生成 ``summary.md``。所有失败都写进返回的 :class:`SummaryPlan`，不抛异常。"""
    entry_dir = Path(entry_dir)
    plan = SummaryPlan(generated_at=now_iso())
    text = str(transcript_text or "").strip()
    plan.transcript_chars, plan.transcript_sha1 = transcript_digest(text)

    if not text:
        plan.status = "skipped"
        plan.reason = REASON_NO_TRANSCRIPT
        plan.message = ("缺少文字来源（需求 3.3.6）：无平台字幕且未启用本地 ASR；"
                        "开启 --asr 并 retry 补做文字稿后可重跑")
        return plan

    if not bool(getattr(config, "summary_enabled", True)):
        plan.status = "skipped"
        plan.reason = REASON_SUMMARY_DISABLED
        plan.message = "总结已关闭（--no-summary / [summary].enabled=false）"
        return plan

    base_url = str(getattr(config, "summary_base_url", "") or "").strip()
    model = str(getattr(config, "summary_model", "") or "").strip()
    if not base_url or not model:
        plan.status = "skipped"
        plan.reason = REASON_LLM_NOT_CONFIGURED
        plan.message = ("未配置 OpenAI 兼容接口：请在 config.local.toml 的 [summary] 段填 base_url 与 "
                        "model（或设 BSA_LLM_BASE_URL / BSA_LLM_MODEL），配置后 retry 可补做")
        return plan

    min_chars = int(getattr(config, "summary_min_chars", 200) or 0)
    # 只按**正文**算字数：transcript.txt 头部的标题与统计行不是内容，
    # 否则"只有一行字幕"的短稿会因为头部凑够字数而被误判为可总结。
    _header, body_text = split_header(text)
    content = body_text or text
    if min_chars and len(content) < min_chars:
        plan.status = "skipped"
        plan.reason = REASON_INSUFFICIENT_TEXT
        plan.message = (f"文字稿正文仅 {len(content)} 字，少于 [summary].min_chars={min_chars}，"
                        "不足以支撑可靠总结（需求 3.3.6）；补齐文字后可重跑")

    try:
        spec = prompt_spec or load_prompt_spec(
            getattr(config, "summary_prompt_file", "") or None, base=entry_dir
        )
    except PromptError as exc:
        plan.status = "failed"
        plan.reason = REASON_LLM_FAILED
        plan.error_kind = "config_error"
        plan.message = str(exc)
        return plan

    plan.prompt_source = spec.source
    plan.prompt_fingerprint = spec.fingerprint
    plan.model = model
    plan.signature = summary_signature(config, spec, text)
    if plan.status == "skipped":          # 文字不足：指纹仍记录，便于人工核对
        return plan

    client = chat_client
    if client is None:
        try:
            client = build_chat_client(config, api_key, logger=logger)
        except LlmUnavailable as exc:
            plan.status = "failed"
            plan.reason = REASON_DEPENDENCY_MISSING
            plan.error_kind = "dependency_missing"
            plan.message = str(exc)
            return plan
    plan.client = str(getattr(client, "name", "") or "")
    plan.endpoint_host = str(getattr(client, "endpoint_host", "") or "")

    chunk_plan = split_transcript(
        text,
        chunk_chars=int(getattr(config, "summary_chunk_chars", 6000) or 6000),
        overlap_chars=int(getattr(config, "summary_overlap_chars", 200) or 0),
        max_chunks=int(getattr(config, "summary_max_chunks", 40) or 40),
    )
    plan.chunk_plan = chunk_plan.to_json()
    plan.chunks = chunk_plan.count
    if chunk_plan.note:
        plan.notes.append(chunk_plan.note)
    if not chunk_plan.chunks:
        plan.status = "skipped"
        plan.reason = REASON_NO_TRANSCRIPT
        plan.message = "文字稿正文为空（只有头部统计），无可总结内容"
        return plan

    pages_label = _pages_label(chunk_plan)

    # ---- 单块：直接完整总结 ---- #
    if chunk_plan.count == 1:
        result = _call(client, spec.render_full(transcript=chunk_plan.chunks[0].text,
                                                chars=len(chunk_plan.body), pages=pages_label,
                                                title=title, author=author), plan, logger)
        if not result.ok:
            return _llm_failure(plan, result)
        body = result.text
    else:
        # ---- 多块：逐块要点 → 合并 ---- #
        partials: list[str] = []
        for chunk in chunk_plan.chunks:
            result = _call(client, spec.render_map(transcript=chunk.text, chars=chunk.chars,
                                                   pages=chunk.pages_label(), index=chunk.index,
                                                   chunks=chunk_plan.count, title=title,
                                                   author=author), plan, logger)
            if not result.ok:
                plan.notes.append(f"第 {chunk.index}/{chunk_plan.count} 块要点提取失败，"
                                  "已停止（不生成缺失内容的总结）")
                return _llm_failure(plan, result)
            partials.append(result.text)
        points = format_points(partials, chunk_plan.chunks)
        body, error = _reduce(client, spec, points, plan, chunk_plan, pages_label, title, author,
                              logger)
        if error is not None:
            return _llm_failure(plan, error)

    plan.text = body.strip()
    if not plan.text:
        plan.status = "failed"
        plan.reason = REASON_LLM_FAILED
        plan.error_kind = KIND_LLM_INVALID
        plan.message = "模型没有返回可用的总结正文"
        return plan

    plan.status = "done"
    ensure_dir(entry_dir)
    atomic_write_text(entry_dir / SUMMARY_MD, _summary_document(plan, title, pages_label))
    plan.summary_md = SUMMARY_MD
    plan.message = plan.summary_line()
    return plan


def _call(client: ChatClient, messages: list[dict[str, str]], plan: SummaryPlan, logger) -> ChatResult:
    result = client.complete(messages)
    plan.calls += 1
    plan.prompt_tokens += int(result.prompt_tokens or 0)
    plan.completion_tokens += int(result.completion_tokens or 0)
    if result.ok and result.truncated:
        plan.truncated_calls += 1
        plan.notes.append("模型因长度上限截断了输出（finish_reason=length），"
                          "可提高 [summary] max_tokens 后重做")
    if not result.ok and logger is not None:
        logger.warning(f"[总结] 模型调用失败：{result.brief()}")
    return result


def _llm_failure(plan: SummaryPlan, result: ChatResult) -> SummaryPlan:
    plan.status = "failed"
    plan.reason = REASON_LLM_FAILED
    plan.error_kind = result.error_kind or KIND_LLM_INVALID
    plan.message = (f"模型调用失败（{result.error_kind}，HTTP {result.http_status}）："
                    f"{result.message or '无错误说明'}")[:400]
    return plan


def _reduce(client: ChatClient, spec: PromptSpec, points: str, plan: SummaryPlan,
            chunk_plan: ChunkPlan, pages_label: str, title: str, author: str,
            logger) -> tuple[str, ChatResult | None]:
    """要点合并：输入超预算时先做一轮"要点再压缩"，再出最终总结（最多 MAX_REDUCE_ROUNDS 轮）。"""
    budget = max(2000, int(chunk_plan.chunk_chars or 6000))
    rounds = 0
    while len(points) > budget and rounds < MAX_REDUCE_ROUNDS:
        groups = _group_points(points, budget)
        if len(groups) <= 1:
            break
        merged: list[str] = []
        for group in groups:
            result = _call(client, spec.render_reduce(points=group, chunks=len(groups),
                                                      pages=pages_label,
                                                      chars=len(group), title=title,
                                                      author=author), plan, logger)
            if not result.ok:
                return "", result
            merged.append(result.text)
        plan.notes.append(f"分段要点 {len(points)} 字超出合并预算，已做第 {rounds + 1} 轮压缩"
                          f"（{len(groups)} 组）")
        points = "\n\n".join(f"### 压缩要点 {idx + 1}\n{text}"
                             for idx, text in enumerate(merged))
        rounds += 1
    if len(points) > budget:
        plan.notes.append("要点仍超出合并预算，已按当前长度直接合并（可能触及模型上下文上限）")

    result = _call(client, spec.render_reduce(points=points, chunks=chunk_plan.count,
                                              pages=pages_label, chars=len(points),
                                              title=title, author=author), plan, logger)
    if not result.ok:
        return "", result
    return result.text, None


def _group_points(points: str, budget: int) -> list[str]:
    """按 ``### `` 小节把要点切成不超过 ``budget`` 的组（保序、不丢尾组）。"""
    blocks: list[str] = []
    current: list[str] = []
    for line in points.split("\n"):
        if line.startswith("### ") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    groups: list[str] = []
    buf: list[str] = []
    size = 0
    for block in blocks:
        if buf and size + len(block) > budget:
            groups.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(block)
        size += len(block) + 2
    if buf:
        groups.append("\n\n".join(buf))
    return groups


def _pages_label(chunk_plan: ChunkPlan) -> str:
    pages = sorted({p for chunk in chunk_plan.chunks for p in chunk.pages})
    if not pages:
        return "未知"
    return "、".join(f"P{p:02d}" for p in pages)


def _summary_document(plan: SummaryPlan, title: str, pages_label: str) -> str:
    """``summary.md``：元信息头（需求 3.3.3 要求保留模型/prompt/时间）+ 正文标记区。"""
    lines = [
        f"# 视频总结：{title or '未命名视频'}",
        "",
        f"- 模型：{plan.model}（客户端 {plan.client or '未记录'}，端点 {plan.endpoint_host or '未记录'}）",
        f"- prompt：{plan.prompt_source}，指纹 {plan.prompt_fingerprint}",
        f"- 生成时间：{plan.generated_at}",
        f"- 文字稿：{plan.transcript_chars} 字（sha1 {plan.transcript_sha1}），"
        f"分 P：{pages_label}，切成 {plan.chunks} 块",
        f"- 模型调用：{plan.calls} 次；tokens prompt {plan.prompt_tokens} / "
        f"completion {plan.completion_tokens}",
        f"- 输入指纹：{plan.signature}（prompt/模型/分块参数变化时重做）",
        "",
        SUMMARY_BEGIN,
        plan.text.strip(),
        SUMMARY_END,
        "",
    ]
    return "\n".join(lines)


def read_summary_body(path: str | Path) -> str:
    """从 ``summary.md`` 取回正文（复用产物时用；没有标记则整篇当正文）。

    兼容改名前后两套标记：新产物写 ``bili-sub-archive``，SubVideo 时期的旧产物
    写的是 ``subvideo``，两套都能切出正文。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for begin, end in _MARKER_PAIRS:
        if begin in raw and end in raw:
            start = raw.index(begin) + len(begin)
            stop = raw.index(end, start)
            return raw[start:stop].strip()
    # 没有标记（人工编辑过 / 旧产物）：剥掉开头的标题与元信息列表
    lines = raw.replace("\r\n", "\n").split("\n")
    idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("# ") or stripped.startswith("- "):
            idx = i + 1
            continue
        break
    return "\n".join(lines[idx:]).strip()


# --------------------------------------------------------------------------- #
# 导图
# --------------------------------------------------------------------------- #
def build_mindmap(
    *,
    plan: SummaryPlan,
    config,
    entry_dir: Path,
    title: str = "",
    mmdc_runner=None,
    logger=None,
) -> SummaryPlan:
    """由总结正文派生大纲 → 写 ``mindmap.mmd`` → 渲染 ``mindmap.png``（就地更新 ``plan``）。"""
    entry_dir = Path(entry_dir)
    if not bool(getattr(config, "mindmap_enabled", True)):
        plan.mindmap_status = "skipped"
        plan.mindmap_reason = REASON_MINDMAP_DISABLED
        plan.mindmap_message = "导图已关闭（--no-mindmap / [mindmap].enabled=false）"
        return plan
    if plan.status != "done":
        plan.mindmap_status = "failed" if plan.status == "failed" else "skipped"
        plan.mindmap_reason = plan.reason or REASON_SUMMARY_FAILED
        plan.mindmap_error_kind = plan.error_kind
        plan.mindmap_message = (f"上游总结未完成（{plan.mindmap_reason}）；"
                               "导图基于总结生成，补做 summary 后再重试")
        return plan

    body = plan.text or read_summary_body(entry_dir / SUMMARY_MD)
    if not body:
        plan.mindmap_status = "failed"
        plan.mindmap_reason = REASON_SUMMARY_FAILED
        plan.mindmap_message = "读不到总结正文，无法生成导图"
        return plan

    max_nodes = int(getattr(config, "mindmap_max_nodes", 60) or 60)
    max_depth = int(getattr(config, "mindmap_max_depth", 3) or 3)
    label_chars = int(getattr(config, "mindmap_label_chars", 24) or 24)

    root: OutlineNode | None = extract_outline(body, max_depth=max_depth, max_nodes=max_nodes,
                                              label_chars=label_chars)
    if root is None:
        root = derive_outline(body, title=title, max_depth=max_depth, max_nodes=max_nodes,
                              label_chars=label_chars)
        plan.outline_source = "derived"
        plan.notes.append("模型输出里没有可解析的“大纲”小节，已按摘要段落派生导图大纲"
                          "（outline_source=derived）")
    else:
        plan.outline_source = "llm"

    result: MermaidResult = to_mermaid(root, title=title, max_nodes=max_nodes,
                                       max_depth=max_depth, label_chars=label_chars,
                                       outline_source=plan.outline_source)
    plan.outline_nodes = result.nodes
    plan.outline_depth = result.depth
    plan.outline_dropped = result.dropped
    plan.notes.extend(result.notes)

    ensure_dir(entry_dir)
    atomic_write_text(entry_dir / MINDMAP_MMD, result.text)
    plan.mindmap_mmd = MINDMAP_MMD
    plan.mindmap_nodes = result.nodes
    plan.mindmap_signature = mindmap_signature(config, plan.signature)

    mmdc = probe_mmdc(config)
    outcome: RenderOutcome = render_mindmap(
        mmd_path=entry_dir / MINDMAP_MMD,
        png_path=entry_dir / MINDMAP_PNG,
        mmdc=mmdc,
        width=int(getattr(config, "mindmap_width", 1600) or 1600),
        background=str(getattr(config, "mindmap_background", "white") or "white"),
        timeout=float(getattr(config, "mindmap_timeout_seconds", 120.0) or 120.0),
        puppeteer_config=str(getattr(config, "mindmap_puppeteer_config", "") or ""),
        runner=mmdc_runner,
        logger=logger,
    )
    if outcome.ok:
        plan.mindmap_status = "done"
        plan.mindmap_png = MINDMAP_PNG
        plan.mindmap_width = outcome.width
        plan.mindmap_height = outcome.height
        plan.mindmap_bytes = outcome.bytes_written
        plan.mindmap_message = plan.mindmap_line()
    else:
        plan.mindmap_status = "failed"
        plan.mindmap_reason = (REASON_DEPENDENCY_MISSING
                               if outcome.error_kind == "dependency_missing"
                               else REASON_RENDER_FAILED)
        plan.mindmap_error_kind = outcome.error_kind
        plan.mindmap_message = (f"{outcome.message}；mindmap.mmd 已保存，装好 mermaid-cli 后 "
                               "retry 只补渲染")
    return plan


__all__ = [
    "LEGACY_SUMMARY_BEGIN",
    "LEGACY_SUMMARY_END",
    "MAX_REDUCE_ROUNDS",
    "MINDMAP_MMD",
    "MINDMAP_PNG",
    "REASON_DEPENDENCY_MISSING",
    "REASON_INSUFFICIENT_TEXT",
    "REASON_LLM_FAILED",
    "REASON_LLM_NOT_CONFIGURED",
    "REASON_MINDMAP_DISABLED",
    "REASON_NO_TRANSCRIPT",
    "REASON_RENDER_FAILED",
    "REASON_SUMMARY_DISABLED",
    "REASON_SUMMARY_FAILED",
    "SUMMARY_BEGIN",
    "SUMMARY_END",
    "SUMMARY_MD",
    "SummaryPlan",
    "build_mindmap",
    "build_summary",
    "mindmap_signature",
    "read_summary_body",
    "summary_signature",
    "transcript_digest",
]
