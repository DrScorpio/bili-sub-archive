"""专栏归档：``article.md`` + ``images/``。

需求 3.4 与阶段 0 第 3.5 节：平台上**两种专栏格式并存**，必须都处理：

| 格式 | 列表来源 | 详情来源 | 正文形态 |
| --- | --- | --- | --- |
| 新格式（opus 专栏） | ``opus/feed/space?type=article`` | ``opus/detail?id=`` | ``paragraphs[]`` |
| 旧格式（read/cv） | ``/x/space/wbi/article`` 的 ``articles[]`` | ``/x/article/view?id=`` | HTML（部分条目另带 ``opus.paragraphs``） |

实现要点：

- 段落类型：``1`` 正文、``2`` 图片、``9`` 小标题；
- 图片按正文顺序改写为相对路径，取不到的**保留原链接**并以空白替代（需求 3.4.1）；
- 付费/充电专栏遵循与其他内容相同的权限规则：正文被 ``MODULE_TYPE_BLOCKED``
  门控时如实记 ``denied``，不写伪造正文。
"""

from __future__ import annotations

from ..bili.client import upgrade_url
from ..bili.parse import OpusDoc, opus_blocks, parse_opus_detail
from ..errors import KIND_BLOCKED, KIND_PARSE
from ..paths import atomic_write_text, ensure_dir
from ..timeutil import dt_to_iso, fmt_local, ts_to_dt
from . import ArchiveContext, ArchiveResult, markdown_header, render_blocks
from .htmlmd import apply_image_paths, html_to_markdown
from .images import download_images, guess_ext, strip_resize


class _UrlPlan:
    """图片下载关闭时的降级 plan：正文里保留原始链接。"""

    def __init__(self, occurrences: list[dict]):
        self._urls = [str((o or {}).get("url") or "") for o in occurrences]

    def path_for(self, index: int) -> str:
        return self._urls[index - 1] if 0 < index <= len(self._urls) else ""


def _meta_pairs(item, *, title, author_name, author_mid, published_at, is_only_fans,
                fmt_label, sources) -> list[tuple[str, str]]:
    return [
        ("标题", title),
        ("作者", f"{author_name or item.author}（mid {author_mid or '-'}）"),
        ("格式", fmt_label),
        ("发布时间", fmt_local(published_at)),
        ("原链接", item.url),
        ("可见范围", "充电专属（is_only_fans=true）" if is_only_fans else "公开"),
        ("正文来源", "、".join(sources) if sources else "-"),
    ]


def _write_article(
    ctx: ArchiveContext,
    item,
    entry,
    *,
    title: str,
    blocks=None,
    html: str = "",
    occurrences: list[dict],
    cover: str = "",
    meta_pairs: list[tuple[str, str]],
    sources: list[str],
    missing: list[str],
    extra: dict,
) -> ArchiveResult:
    """公共落盘流程：images → article.md → metadata。"""
    entry_dir = ctx.entry_dir(entry)
    ensure_dir(entry_dir)
    prev_images = list(entry.extra.get("images") or [])
    prev_body = [r for r in prev_images if r.get("role") == "body"]
    prev_cover = [r for r in prev_images if r.get("role") == "cover"]

    plan = None
    image_records: list[dict] = []
    cover_records: list[dict] = []
    if not ctx.config.download_images:
        ctx.skip_step(entry, "images", "download_disabled")
        plan = _UrlPlan(occurrences)
    else:
        if occurrences:
            plan = download_images(ctx.api, occurrences, entry_dir=entry_dir,
                                   referer=item.url, previous=prev_body,
                                   logger=ctx.logger, role="body", ui=ctx.ui)
            image_records = plan.to_json()
        cover_url = upgrade_url(strip_resize(cover))
        if cover_url:
            cover_plan = download_images(
                ctx.api, [{"url": cover_url}], entry_dir=entry_dir, referer=item.url,
                previous=prev_cover, logger=ctx.logger, role="cover",
                names=[f"cover{guess_ext(cover_url)}"], ui=ctx.ui,
            )
            cover_records = cover_plan.to_json()

        failed = sum(1 for r in image_records + cover_records if r.get("status") == "failed")
        if not occurrences and not cover_url:
            ctx.skip_step(entry, "images", "no_images")
        elif failed:
            ctx.fail_step(entry, "images", error_kind="image_download",
                          message=f"{failed} 张图片下载失败（正文保留原链接，可重跑补做）",
                          reason="partial_image_failure")
        else:
            artifacts = [r["name"] for r in image_records + cover_records
                         if r.get("status") == "done" and r.get("name")]
            ctx.finish_step(entry, "images", artifacts=artifacts,
                            message=(plan.summary() if plan and hasattr(plan, "summary") else "仅封面"))

    # ---- 正文 ---- #
    for record in image_records + cover_records:
        if record.get("status") == "failed":
            missing.append(
                f"图片 {record.get('index')} 下载失败（{record.get('error_kind')}）："
                f"{record.get('url')}（正文保留原链接，重跑只补失败项）"
            )
    if plan is None:
        plan = _UrlPlan(occurrences)
    if blocks:
        body, _cursor = render_blocks(blocks, lambda index: plan.path_for(index), 1, "图片")
    elif html:
        markdown_body, srcs = html_to_markdown(html)
        mapping = {}
        for idx, _src in enumerate(srcs):
            resolved = plan.path_for(idx + 1)
            if resolved and resolved != _src:
                mapping[idx] = resolved
        body = apply_image_paths(markdown_body, mapping, srcs)
    else:
        body = "（正文为空：接口未返回可读正文）"
        missing.append("正文为空")

    out = [markdown_header(title or item.title or "专栏", meta_pairs)]
    if cover_records and cover_records[0].get("status") == "done":
        out.append(f"![封面]({cover_records[0]['name']})\n")
    if missing:
        out.append("## 未获取到的内容\n")
        out.extend(f"- {note}" for note in missing)
        out.append("")
    out.append("## 正文\n")
    out.append(body)
    article_md = "\n".join(out).rstrip() + "\n"
    article_path = entry_dir / "article.md"
    atomic_write_text(article_path, article_md)

    # ---- metadata ---- #
    entry.extra.update(extra)
    entry.extra["images"] = image_records + cover_records
    entry.extra["missing"] = missing
    entry.extra["sources"] = sources
    entry.extra["text_chars"] = len(body)
    entry.source.update({"sources": sources, "format": extra.get("format", "")})
    ctx.store.write_metadata(entry)
    ctx.finish_step(entry, "content", artifacts=[ctx.rel(article_path, entry)],
                    message=f"{len(article_md)} 字符")

    outcome = "done"
    notes: list[str] = list(missing)
    if any(r.get("status") == "failed" for r in image_records + cover_records):
        outcome = "partial"
        notes.append("部分图片下载失败：article.md 中保留原链接，重跑只补失败项")
    return ArchiveResult(outcome=outcome, artifacts=[ctx.rel(article_path, entry)],
                         notes=notes, message=f"专栏 {item.platform_id} 归档完成")


def _archive_opus(ctx: ArchiveContext, item, entry) -> ArchiveResult:
    pid = item.platform_id
    cache_key = ("opus", pid)
    doc: OpusDoc | None = ctx.cache_get(cache_key)
    resp = None
    if doc is None:
        resp = ctx.api.opus_detail(pid)
        if not resp.ok:
            return ctx.problem(entry, "fetch", resp, note="专栏详情接口失败，可重跑补做")
        doc = parse_opus_detail(resp.data)
        if doc is None:
            ctx.fail_step(entry, "fetch", error_kind=KIND_PARSE,
                          message="opus/detail 响应缺少 item/modules")
            return ArchiveResult(outcome="failed", error_kind=KIND_PARSE,
                                 message="专栏详情结构无法解析")
        ctx.cache_put(cache_key, doc)

    sources = ["/x/polymer/web-dynamic/v1/opus/detail"]
    missing: list[str] = []
    if doc.blocked or (doc.is_only_fans and not doc.blocks):
        ctx.finish_step(entry, "fetch", message="、".join(sources))
        ctx.skip_with_error(
            entry, "content", "content_blocked", KIND_BLOCKED,
            "正文被平台门控（MODULE_TYPE_BLOCKED）：当前账号无该充电专栏的读取权限",
        )
        ctx.skip_step(entry, "images", "upstream_blocked")
        entry.extra.update({
            "format": "opus",
            "blocked": True,
            "is_only_fans": doc.is_only_fans,
            "permission": "blocked",
            "opus_id": pid,
            "missing": ["正文被平台门控（充电专属，当前账号不可读）"],
        })
        ctx.store.write_metadata(entry)
        return ArchiveResult(outcome="denied", error_kind=KIND_BLOCKED,
                             message="专栏正文被门控（充电专属，当前账号不可读）",
                             notes=["正文被门控：未写入 article.md"])

    title = doc.title or item.title
    entry.title = title or entry.title
    entry.author = doc.author_name or entry.author
    entry.flags.update({"is_only_fans": doc.is_only_fans})
    entry.source.update({"api": "/x/polymer/web-dynamic/v1/opus/detail", "format": "opus"})
    ctx.finish_step(entry, "fetch", message="、".join(sources))

    occurrences: list[dict] = []
    for block in doc.blocks:
        if block.is_image:
            occurrences.extend(p for p in block.pics if p.get("url"))
    if not occurrences:
        missing.append("正文无图片段落")
    meta_pairs = _meta_pairs(item, title=title, author_name=doc.author_name,
                             author_mid=doc.author_mid, published_at=doc.published_at,
                             is_only_fans=doc.is_only_fans, fmt_label="opus 专栏（新格式）",
                             sources=sources)
    extra = {
        "format": "opus",
        "item_type": doc.item_type,
        "article_type": doc.article_type,
        "is_only_fans": doc.is_only_fans,
        "blocked": False,
        "pub_time": fmt_local(doc.published_at),
        "author": {"mid": doc.author_mid, "name": doc.author_name, "face": doc.author_face},
        "blocks": len(doc.blocks),
        "image_paragraphs": len(occurrences),
        "opus_id": pid,
        "permission": "ok",
    }
    return _write_article(ctx, item, entry, title=title, blocks=doc.blocks,
                          occurrences=occurrences, cover="", meta_pairs=meta_pairs,
                          sources=sources, missing=missing, extra=extra)


def _archive_legacy(ctx: ArchiveContext, item, entry) -> ArchiveResult:
    cvid = str(item.platform_id).lower().removeprefix("cv")
    cache_key = ("cv", cvid)
    data = ctx.cache_get(cache_key)
    resp = None
    if data is None:
        resp = ctx.api.article_view(cvid)
        if not resp.ok:
            return ctx.problem(entry, "fetch", resp, note="专栏详情接口失败，可重跑补做")
        data = resp.data if isinstance(resp.data, dict) else None
        if not data:
            ctx.fail_step(entry, "fetch", error_kind=KIND_PARSE, message="article/view 响应为空")
            return ArchiveResult(outcome="failed", error_kind=KIND_PARSE,
                                 message="旧格式专栏详情为空")
        ctx.cache_put(cache_key, data)

    sources = ["/x/article/view"]
    missing: list[str] = []
    title = str(data.get("title") or item.title)
    author_block = data.get("author") if isinstance(data.get("author"), dict) else {}
    published = ts_to_dt(data.get("publish_time") or data.get("ctime")) or item.published_at

    blocks = None
    html = ""
    opus_block = data.get("opus") if isinstance(data.get("opus"), dict) else {}
    content_block = opus_block.get("content") if isinstance(opus_block.get("content"), dict) else {}
    paragraphs = content_block.get("paragraphs")
    if isinstance(paragraphs, list) and paragraphs:
        blocks = opus_blocks(content_block)
        sources.append("/x/article/view#opus.paragraphs")
    html = str(data.get("content") or "")
    if not blocks and not html:
        missing.append("正文为空（接口未返回 content）")

    # 图片：正文内联顺序优先，数量匹配时用 image_urls 的原图替换
    image_urls = [str(u) for u in (data.get("image_urls") or data.get("origin_image_urls") or []) if u]
    occurrences: list[dict] = []
    if blocks:
        for block in blocks:
            if block.is_image:
                occurrences.extend(p for p in block.pics if p.get("url"))
    if not occurrences and html:
        _md, srcs = html_to_markdown(html)
        for idx, src in enumerate(srcs):
            original = image_urls[idx] if idx < len(image_urls) else src
            occurrences.append({"url": original, "fallback": src})

    cover = ""
    covers = opus_block.get("article") if isinstance(opus_block.get("article"), dict) else {}
    cover_list = covers.get("cover") if isinstance(covers.get("cover"), list) else []
    if cover_list and isinstance(cover_list[0], dict):
        cover = str(cover_list[0].get("url") or "")
    if not cover:
        cover = str(data.get("banner_url") or "")

    entry.title = title or entry.title
    entry.author = str(author_block.get("name") or entry.author)
    entry.source.update({"api": "/x/article/view", "format": "legacy_cv"})
    if published and not item.published_at:
        # 列表未给时间时以详情为准；已有则保留（index/metadata 统一用 ISO 带时区）
        entry.published_at = dt_to_iso(published)
    ctx.finish_step(entry, "fetch", message="、".join(sources))

    meta_pairs = _meta_pairs(item, title=title, author_name=str(author_block.get("name") or ""),
                             author_mid=int(author_block.get("mid") or 0), published_at=published,
                             is_only_fans=False, fmt_label="read/cv 专栏（旧格式）", sources=sources)
    extra = {
        "format": "legacy_cv",
        "cv_id": cvid,
        "opus_id": str(opus_block.get("opus_id") or ""),
        "dynamic_id": str(data.get("dyn_id_str") or ""),
        "words": data.get("words"),
        "is_only_fans": False,
        "pub_time": fmt_local(published),
        "author": {
            "mid": int(author_block.get("mid") or 0),
            "name": str(author_block.get("name") or ""),
            "face": str(author_block.get("face") or ""),
        },
        "blocks": len(blocks) if blocks else 0,
        "image_paragraphs": len(occurrences),
        "html_fallback": bool(html and not blocks),
        "permission": "ok",
    }
    return _write_article(ctx, item, entry, title=title, blocks=blocks, html=html,
                          occurrences=occurrences, cover=cover, meta_pairs=meta_pairs,
                          sources=sources, missing=missing, extra=extra)


def archive_article(ctx: ArchiveContext, item, entry) -> ArchiveResult:
    """专栏归档入口：按 ``raw_ref.format`` 分派新旧两种格式。"""
    fmt = str(item.raw_ref.get("format") or "opus")
    if fmt == "legacy_cv":
        return _archive_legacy(ctx, item, entry)
    return _archive_opus(ctx, item, entry)
