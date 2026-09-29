"""动态归档：``content.md`` + ``images/``（原图按原顺序）。

需求 3.2 与阶段 0 第 3.4 节：

- 保存动态原文、发布时间、原链接、配图原文件，保留原图顺序与图文对应关系；
- 对转发动态、视频卡片、投票、表情等复杂类型，尽量保留可获取的文本、图片、
  链接和类型说明；无法完整渲染的部分在元数据中标记；
- 充电专属动态在未授权时正文被门控（``MODULE_TYPE_BLOCKED``）→ **如实记录**，
  不把 15 字的截断正文当成完整内容归档；
- 正文优先取 ``opus/detail`` 的 ``paragraphs[]``（完整、含图片段落），
  ``/detail`` 的 ``major.opus.summary`` 只作降级来源。

阶段 2 才生成 ``render.png``（单张长图），此处记 ``skipped``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..bili.parse import (
    DynamicDoc,
    dynamic_body_text,
    major_images,
    parse_dynamic_item,
    parse_opus_detail,
)
from ..errors import KIND_BLOCKED, KIND_PARSE
from ..paths import atomic_write_text, ensure_dir
from ..timeutil import fmt_local
from . import ArchiveContext, ArchiveResult, markdown_header, render_blocks
from .images import download_images, guess_ext, strip_resize
from ..bili.client import upgrade_url

#: 需要额外取 ``opus/detail`` 的动态类型（正文以 opus 段落承载）
_OPUS_BODY_TYPES = {"DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"}


class _UrlPlan:
    """关闭图片下载时的降级 plan：正文里引用原始 CDN 链接。"""

    def __init__(self, occurrences: list[dict]):
        self._urls = [str((o or {}).get("url") or "") for o in occurrences]

    def path_for(self, index: int) -> str:
        return self._urls[index - 1] if 0 < index <= len(self._urls) else ""

    def summary(self) -> str:
        return "未下载（已关闭配图下载）"


@dataclass
class ImageLayout:
    """图片序号布局：保证「下载了的图一定在正文里被引用」。

    序号规则（1 起）：

    - 主动态段落图：``1 .. main_body``
    - 主动态卡片图（视频卡封面 / 图文 summary.pics）：紧随其后
    - 转发原文段落图、转发原文卡片图：再往后

    重复 URL 也各占一个序号（下载器按 URL 复用同一份文件），这样序号与渲染
    一一对应，不会出现"图下载了但正文没引用"或"引用指向别的图"。
    """

    occurrences: list[dict] = field(default_factory=list)
    main_body: int = 0
    main_card: int = 0
    orig_body: int = 0
    orig_card: int = 0

    @property
    def main_card_start(self) -> int:
        return self.main_body + 1

    @property
    def orig_body_start(self) -> int:
        return self.main_body + self.main_card + 1

    @property
    def orig_card_start(self) -> int:
        return self.main_body + self.main_card + self.orig_body + 1

    @property
    def total(self) -> int:
        return self.main_body + self.main_card + self.orig_body + self.orig_card


def _block_pics(doc: DynamicDoc) -> list[dict]:
    out: list[dict] = []
    for block in doc.blocks:
        if block.is_image:
            out.extend(p for p in block.pics if p.get("url"))
    return out


def _collect_occurrences(doc: DynamicDoc, orig_doc: DynamicDoc | None = None) -> ImageLayout:
    """按正文出现顺序收集主动态与转发原文的全部图片。"""
    layout = ImageLayout()
    main_body = _block_pics(doc)
    main_card = [p for p in major_images(doc.major) if p.get("url")]
    layout.occurrences.extend(main_body)
    layout.occurrences.extend(main_card)
    layout.main_body = len(main_body)
    layout.main_card = len(main_card)
    if orig_doc is not None:
        orig_body = _block_pics(orig_doc)
        orig_card = [p for p in major_images(orig_doc.major) if p.get("url")]
        layout.occurrences.extend(orig_body)
        layout.occurrences.extend(orig_card)
        layout.orig_body = len(orig_body)
        layout.orig_card = len(orig_card)
    return layout


def _render_body(doc: DynamicDoc, plan, start: int = 1) -> tuple[str, int]:
    """正文块 → markdown（图片按出现顺序内联），返回 ``(文本, 下一序号)``。"""
    if doc.blocks:
        path_for = (lambda index: plan.path_for(index) if plan else "")
        return render_blocks(doc.blocks, path_for, start)
    text = dynamic_body_text(doc)
    return text.strip(), start


def _render_leftover(plan, start: int, count: int) -> str:
    """正文里没被消费的图片（卡片封面等）单独成节，确保每张图都被引用。"""
    if not plan or count <= 0:
        return ""
    lines = []
    for index in range(start, start + count):
        path = plan.path_for(index)
        lines.append(f"![图片{index}]({path})" if path else f"（图片{index}未取得）")
    return "\n\n".join(lines)


def _render_forward(orig: dict | None, orig_doc: DynamicDoc | None, plan, layout: ImageLayout) -> str:
    """转发原文（``orig`` 与主条目同形）→ markdown 片段。"""
    if not orig:
        return "（转发原文缺失：可能已被删除或当前账号无权访问）"
    if orig_doc is None:
        return "（转发原文结构无法解析）"
    lines = [
        f"- 原作者：{orig_doc.author_name or '未知'}（mid {orig_doc.author_mid or '-'}）",
        f"- 原动态 ID：{orig_doc.id_str or '-'}",
        f"- 类型：{orig_doc.dyn_type or '-'} / {orig_doc.major_type or '-'}",
        f"- 发布时间：{fmt_local(orig_doc.published_at)}",
    ]
    body, _cursor = _render_body(orig_doc, plan, layout.orig_body_start)
    lines.append("")
    lines.append(body or "（转发原文无可读文本）")
    card = _card_section(orig_doc)
    if card:
        lines.append("")
        lines.append(card)
    leftover = _render_leftover(plan, layout.orig_card_start, layout.orig_card)
    if leftover:
        lines.append("")
        lines.append(leftover)
    return "\n".join(lines)


def _card_section(doc: DynamicDoc) -> str:
    """卡片类动态（视频卡 / 问答卡 / 直播卡等）的可读摘要。"""
    if not doc.major_type or doc.major_type == "MAJOR_TYPE_OPUS":
        return ""
    lines = [f"- 卡片类型：{doc.major_type}"]
    major = doc.major if isinstance(doc.major, dict) else {}
    if doc.major_type == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
        lines.append(f"- 视频标题：{archive.get('title') or '-'}")
        lines.append(f"- BV 号：{archive.get('bvid') or '-'}")
        lines.append(f"- 时长：{archive.get('duration_text') or '-'}")
        if archive.get("bvid"):
            lines.append(f"- 视频链接：https://www.bilibili.com/video/{archive.get('bvid')}")
    elif doc.major_type == "MAJOR_TYPE_COMMON":
        common = major.get("common") if isinstance(major.get("common"), dict) else {}
        lines.append(f"- 标题：{common.get('title') or '-'}")
        if common.get("jump_url"):
            lines.append(f"- 跳转链接：{common.get('jump_url')}")
    elif doc.major_type == "MAJOR_TYPE_ARTICLE":
        article = major.get("article") if isinstance(major.get("article"), dict) else {}
        lines.append(f"- 专栏标题：{article.get('title') or '-'}")
        if article.get("jump_url"):
            lines.append(f"- 跳转链接：{article.get('jump_url')}")
    elif doc.major_type == "MAJOR_TYPE_LIVE_RCMD":
        lines.append("- 说明：直播推荐卡，仅保留卡片文本与链接，不采集直播内容（需求 §7）")
    if doc.parse_note:
        lines.append(f"- 说明：{doc.parse_note}")
    return "\n".join(lines)


def _render_content(item, doc: DynamicDoc, plan, layout: ImageLayout,
                    avatar_path: str, sources: list[str], missing: list[str],
                    orig_doc: DynamicDoc | None = None) -> str:
    pairs = [
        ("UP 主", f"{doc.author_name or item.author}（mid {doc.author_mid or '-'}）"),
        ("类型", f"{doc.dyn_type or '-'} / {doc.major_type or '-'}"),
        ("发布时间", fmt_local(doc.published_at)),
        ("原链接", item.url),
        ("可见范围", "充电专属（is_only_fans=true）" if doc.is_only_fans else "公开"),
        ("是否置顶", "是" if doc.pinned else "否"),
        ("平台标记", doc.badge or "-"),
        ("数据", f"点赞 {doc.stat.get('like')} / 评论 {doc.stat.get('comment')} / 转发 {doc.stat.get('forward')}"
                 if doc.stat else ""),
        ("头像", f"![头像]({avatar_path})" if avatar_path else doc.author_face),
        ("正文来源", "、".join(sources) if sources else "-"),
    ]
    out = [markdown_header(f"{item.title or '动态'}（动态）", pairs)]
    if missing:
        out.append("## 未获取到的内容\n")
        out.extend(f"- {note}" for note in missing)
        out.append("")
    body, _cursor = _render_body(doc, plan, 1)
    out.append("## 正文\n")
    out.append(body or "（正文字段为空：该动态可能只有卡片内容，见下方卡片信息）")
    out.append("")
    leftover = _render_leftover(plan, layout.main_card_start, layout.main_card)
    if leftover:
        out.append("## 配图\n")
        out.append(leftover)
        out.append("")
    if doc.orig is not None or doc.dyn_type == "DYNAMIC_TYPE_FORWARD":
        out.append("## 转发原文\n")
        out.append(_render_forward(doc.orig, orig_doc, plan, layout))
        out.append("")
    card = _card_section(doc)
    if card:
        out.append("## 卡片信息\n")
        out.append(card)
        out.append("")
    if doc.parse_note and not card:
        out.append("## 卡片信息\n")
        out.append(f"- {doc.parse_note}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def archive_dynamic(ctx: ArchiveContext, item, entry) -> ArchiveResult:
    """动态归档主流程。"""
    pid = item.platform_id
    entry_dir = ctx.entry_dir(entry)
    ensure_dir(entry_dir)
    sources: list[str] = []
    missing: list[str] = []

    cache_key = ("dynamic", pid)
    cached = ctx.cache_get(cache_key)
    if cached is None:
        detail = ctx.api.dynamic_detail(pid)
        if not detail.ok:
            return ctx.problem(entry, "fetch", detail, note="动态详情接口失败，可重跑补做")
        item_raw = (detail.data or {}).get("item") if isinstance(detail.data, dict) else None
        doc = parse_dynamic_item(item_raw)
        if doc is None or not doc.id_str:
            ctx.fail_step(entry, "fetch", error_kind=KIND_PARSE,
                          message="detail 响应缺少 item/modules 结构")
            return ArchiveResult(outcome="failed", error_kind=KIND_PARSE,
                                 message="动态详情结构无法解析")
        cached = {
            "doc": doc,
            "opus": None,
            "opus_tried": False,
            "sources": ["/x/polymer/web-dynamic/v1/detail"],
            "missing": [],
        }
        ctx.cache_put(cache_key, cached)
    doc = cached["doc"]
    sources = cached["sources"]
    missing = cached["missing"]

    # opus 段落正文（比 detail 的 summary 完整，且带图片段落顺序）：
    # 即便 detail 来自缓存（例如由发现阶段的次级来源预取），也要补这一步
    if not cached["opus_tried"] and (
            doc.major_type == "MAJOR_TYPE_OPUS" or doc.dyn_type in _OPUS_BODY_TYPES):
        cached["opus_tried"] = True
        opus_resp = ctx.api.opus_detail(pid)
        if opus_resp.ok:
            opus_doc = parse_opus_detail(opus_resp.data)
            if opus_doc is not None:
                cached["opus"] = opus_doc
                sources.append("/x/polymer/web-dynamic/v1/opus/detail")
                if opus_doc.blocks:
                    doc.blocks = opus_doc.blocks
                if opus_doc.pub_ts:
                    doc.pub_ts = opus_doc.pub_ts
        else:
            missing.append(
                f"opus/detail 未取到（{opus_resp.kind_label} code={opus_resp.code}），"
                "正文降级为列表/详情卡片的 summary"
            )
    opus_doc = cached["opus"]

    if doc is None:
        ctx.fail_step(entry, "fetch", error_kind=KIND_PARSE, message="缓存解析结果缺失")
        return ArchiveResult(outcome="failed", error_kind=KIND_PARSE, message="详情解析失败")

    if opus_doc is not None and opus_doc.blocked:
        # 充电正文被门控：详情本身取到了（fetch 成功），但正文按权限不可读 →
        # 如实记 ``content=skipped(content_blocked)`` 并留错误记录，**不写伪造正文**。
        ctx.finish_step(entry, "fetch", message="、".join(sources))
        ctx.skip_with_error(
            entry, "content", "content_blocked", KIND_BLOCKED,
            "正文被平台门控（MODULE_TYPE_BLOCKED）：当前账号无该充电动态的读取权限",
        )
        ctx.skip_step(entry, "images", "upstream_blocked")
        ctx.skip_step(entry, "render", "upstream_blocked",
                      message="正文被门控，没有可渲染的内容")
        entry.extra.update({
            "blocked": True,
            "sources": sources,
            "dynamic_type": doc.dyn_type,
            "major_type": doc.major_type,
            "is_only_fans": doc.is_only_fans,
            "permission": "blocked",
            "missing": ["正文被平台门控（充电专属，当前账号不可读）"],
        })
        ctx.store.write_metadata(entry)
        return ArchiveResult(
            outcome="denied", error_kind=KIND_BLOCKED,
            message="动态正文被门控（充电专属，当前账号不可读）",
            notes=["正文被门控：未写入 content.md"],
        )

    # ---- fetch 完成 ---- #
    ctx.finish_step(entry, "fetch", artifacts=[], reason="",
                    message=f"来源：{'、'.join(sources)}")

    # ---- images ---- #
    orig_doc = parse_dynamic_item(doc.orig) if doc.orig else None
    layout = _collect_occurrences(doc, orig_doc)
    occurrences = layout.occurrences
    prev_images = list(entry.extra.get("images") or [])
    prev_body = [r for r in prev_images if r.get("role") == "body"]
    prev_avatar = [r for r in prev_images if r.get("role") == "avatar"]
    referer = item.url

    plan = None
    avatar_path = ""
    image_records: list[dict] = []
    avatar_records: list[dict] = []
    if not ctx.config.download_images:
        ctx.skip_step(entry, "images", "download_disabled")
    else:
        if occurrences:
            plan = download_images(ctx.api, occurrences, entry_dir=entry_dir,
                                   referer=referer, previous=prev_body, logger=ctx.logger,
                                   role="body", ui=ctx.ui)
            image_records = plan.to_json()
        face = upgrade_url(strip_resize(doc.author_face))
        if face:
            avatar_plan = download_images(
                ctx.api, [{"url": face}], entry_dir=entry_dir, referer=referer,
                previous=prev_avatar, logger=ctx.logger, role="avatar",
                names=[f"avatar{guess_ext(face)}"], ui=ctx.ui,
            )
            avatar_records = avatar_plan.to_json()
            avatar_path = avatar_plan.path_for(1)

        total_failed = (plan.failed if plan else 0) + sum(
            1 for r in avatar_records if r.get("status") == "failed"
        )
        if not occurrences and not face:
            ctx.skip_step(entry, "images", "no_images")
        elif total_failed:
            ctx.fail_step(entry, "images", error_kind="image_download",
                          message=f"{total_failed} 张图片下载失败（产物保留原链接，可重跑补做）",
                          reason="partial_image_failure")
        else:
            artifacts = [r["name"] for r in image_records + avatar_records
                         if r.get("status") == "done" and r.get("name")]
            ctx.finish_step(entry, "images", artifacts=artifacts,
                            message=(plan.summary() if plan else "仅头像"))

    # ---- content ---- #
    image_notes = [
        f"配图 {record.get('index')} 下载失败（{record.get('error_kind')}）："
        f"{record.get('url')}（正文保留原链接，重跑只补失败项）"
        for record in image_records + avatar_records
        if record.get("status") == "failed"
    ]
    missing = list(missing) + image_notes  # 不改动缓存里的列表，避免重跑时重复累积
    if plan is None and occurrences:
        # 关闭图片下载时仍要在正文里保留原图链接（需求 3.2.1 / 3.4.1）
        plan = _UrlPlan(occurrences)
    markdown = _render_content(item, doc, plan, layout, avatar_path, sources,
                               missing, orig_doc=orig_doc)
    content_path = entry_dir / "content.md"
    atomic_write_text(content_path, markdown)

    # ---- metadata ---- #
    entry.title = item.title or entry.title
    entry.url = item.url
    entry.author = doc.author_name or entry.author
    entry.flags.update({
        "is_only_fans": doc.is_only_fans,
        "pinned": doc.pinned,
        "badge": doc.badge,
    })
    entry.source.update({
        "api": "/x/polymer/web-dynamic/v1/detail",
        "sources": sources,
        "dynamic_type": doc.dyn_type,
        "major_type": doc.major_type,
    })
    entry.extra.update({
        "dynamic_type": doc.dyn_type,
        "major_type": doc.major_type,
        "is_only_fans": doc.is_only_fans,
        "blocked": False,
        "pinned": doc.pinned,
        "badge": doc.badge,
        "pub_time": fmt_local(doc.published_at),
        "author": {"mid": doc.author_mid, "name": doc.author_name, "face": doc.author_face},
        "stat": doc.stat,
        "card_note": doc.parse_note,
        "text_chars": len(dynamic_body_text(doc)),
        "body_images": layout.main_body,
        "card_images": layout.main_card,
        "forward_images": layout.orig_body + layout.orig_card,
        "images": image_records + avatar_records,
        "missing": missing,
        "sources": sources,
        "permission": "blocked" if doc.is_only_fans and not doc.blocks else "ok",
    })
    ctx.store.write_metadata(entry)
    ctx.finish_step(entry, "content", artifacts=[ctx.rel(content_path, entry)],
                    message=f"{len(markdown)} 字符")
    render_notes = _archive_render(ctx, item, entry, doc, layout, plan, avatar_path, orig_doc)

    artifacts = [ctx.rel(content_path, entry)]
    outcome = "done"
    notes: list[str] = list(missing) + render_notes
    if image_records and any(r.get("status") == "failed" for r in image_records):
        outcome = "partial"
        notes.append("部分配图下载失败：content.md 中保留原链接，重跑只补失败项")
    if not sources or "opus" not in " ".join(sources):
        if doc.is_only_fans:
            notes.append("充电专属动态：正文来源为 detail 卡片摘要，未取得 opus 段落正文")
    return ArchiveResult(outcome=outcome, artifacts=artifacts, notes=notes,
                         message=f"动态 {pid} 归档完成")


# --------------------------------------------------------------------------- #
# render：单张长 PNG（阶段 2，需求 3.2.2）
# --------------------------------------------------------------------------- #
def _render_image_item(plan, index: int, entry_dir: Path, prefix: str = "图片"):
    """把某个图片序号解析成渲染项：有本地文件就渲染，否则显示占位说明。"""
    from ..render import RenderItem

    raw = plan.path_for(index) if plan is not None else ""
    if raw and not str(raw).startswith("http"):
        candidate = Path(entry_dir) / str(raw)
        if candidate.is_file():
            return RenderItem(kind="image", image=str(candidate),
                              caption=f"{prefix}{index}")
    detail = str(raw) if raw else "无可用链接"
    return RenderItem(kind="image", caption=f"（{prefix}{index}未取得：{detail}）")


def _render_body_items(doc: DynamicDoc, plan, entry_dir: Path, start: int) -> list:
    """正文块 → 渲染项（与 content.md 的图片序号布局保持一致）。"""
    from ..render import RenderItem

    items: list = []
    cursor = start
    for block in doc.blocks:
        if block.is_image:
            for _pic in block.pics:
                items.append(_render_image_item(plan, cursor, entry_dir))
                cursor += 1
        elif block.kind == "heading":
            text = (block.text or "").strip()
            if text:
                items.append(RenderItem(kind="heading", text=text, level=block.level or 3))
        elif block.kind == "text":
            text = (block.text or "").strip()
            if text:
                items.append(RenderItem(kind="text", text=text))
        else:
            items.append(RenderItem(kind="note", text=block.note or "未识别的内容块"))
    return items


def _render_items(doc: DynamicDoc, plan, layout: ImageLayout, entry_dir: Path,
                  orig_doc: DynamicDoc | None) -> list:
    """按 content.md 的章节顺序组装渲染项。"""
    from ..render import RenderItem

    items: list = []
    if not doc.blocks:
        text = dynamic_body_text(doc).strip()
        if text:
            items.append(RenderItem(kind="text", text=text))
    items.extend(_render_body_items(doc, plan, entry_dir, 1))
    for index in range(layout.main_card_start, layout.main_card_start + layout.main_card):
        items.append(_render_image_item(plan, index, entry_dir, "卡片图"))
    card = _card_section(doc)
    if card:
        items.append(RenderItem(kind="note", text=card))
    if doc.orig is not None or doc.dyn_type == "DYNAMIC_TYPE_FORWARD":
        items.append(RenderItem(kind="heading", text="转发原文", level=2))
        if orig_doc is not None:
            items.append(RenderItem(
                kind="note",
                text=(f"原作者：{orig_doc.author_name or '未知'}"
                      f"（mid {orig_doc.author_mid or '-'}）"
                      f"；原动态 ID：{orig_doc.id_str or '-'}"
                      f"；发布时间：{fmt_local(orig_doc.published_at)}"),
            ))
            if not orig_doc.blocks:
                body = dynamic_body_text(orig_doc).strip()
                if body:
                    items.append(RenderItem(kind="text", text=body))
            items.extend(_render_body_items(orig_doc, plan, entry_dir,
                                            layout.orig_body_start))
            for index in range(layout.orig_card_start,
                               layout.orig_card_start + layout.orig_card):
                items.append(_render_image_item(plan, index, entry_dir, "转发图"))
        else:
            items.append(RenderItem(kind="note", text="（转发原文缺失：可能已被删除或当前账号无权访问）"))
    return items


def _archive_render(ctx, item, entry, doc: DynamicDoc, layout: ImageLayout, plan,
                    avatar_path: str, orig_doc: DynamicDoc | None) -> list[str]:
    """渲染单张长 PNG。返回需要写进运行摘要的说明。"""
    from ..render import RenderHeader, render_long_png

    cfg = ctx.config
    if not cfg.render_dynamic_png:
        ctx.skip_step(entry, "render", "render_disabled",
                      message="动态长图已关闭（[render].dynamic_png=false）")
        return []

    entry_dir = ctx.entry_dir(entry)
    avatar = str(entry_dir / avatar_path) if avatar_path else ""
    stat = doc.stat or {}
    stats_text = ""
    if stat:
        stats_text = (f"点赞 {stat.get('like')} / 评论 {stat.get('comment')}"
                      f" / 转发 {stat.get('forward')}")
    header = RenderHeader(
        author=doc.author_name or item.author,
        avatar=avatar,
        published=fmt_local(doc.published_at),
        title=item.title or "",
        url=item.url,
        badge=doc.badge or ("置顶" if doc.pinned else ""),
        visible="充电专属（is_only_fans）" if doc.is_only_fans else "公开",
        stats=stats_text,
        forward_note="本条为转发动态" if doc.orig is not None else "",
    )
    items = _render_items(doc, plan, layout, entry_dir, orig_doc)
    out_path = entry_dir / "render.png"
    result = render_long_png(
        header, items, out_path,
        width=cfg.render_width, max_height=cfg.render_max_height,
        font_path=cfg.render_font, logger=ctx.logger,
    )
    entry.extra["render"] = {
        "ok": result.ok,
        "path": ctx.rel(out_path, entry) if result.ok else "",
        "width": result.width,
        "height": result.height,
        "bytes": result.bytes_written,
        "images_rendered": result.images_rendered,
        "images_missing": result.images_missing,
        "clamped": result.clamped,
        "error_kind": result.error_kind,
        "message": result.message,
        "note": "单张长图，高度随内容增长，不分页（需求 3.2.2）",
    }
    ctx.store.write_metadata(entry)

    if not result.ok:
        ctx.fail_step(entry, "render", error_kind=result.error_kind or "render_error",
                      reason=result.error_kind or "render_error",
                      message=result.message[:400])
        return [f"长图渲染失败：{result.message}"]

    notes: list[str] = []
    if result.clamped:
        notes.append(f"长图被高度上限截断：{result.message}")
    if result.images_missing:
        notes.append(f"长图中有 {result.images_missing} 张配图未取得（已按占位说明渲染）")
    ctx.finish_step(
        entry, "render", artifacts=[ctx.rel(out_path, entry)],
        message=f"{result.width}×{result.height}，渲染图片 {result.images_rendered} 张"
                + ("（已截断）" if result.clamped else ""),
    )
    return notes
