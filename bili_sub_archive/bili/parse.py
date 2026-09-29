"""响应解析：把平台原始结构转成业务模型（纯函数，便于离线单测）。

阶段 0 第 3.4.2 节明确的两个坑，这里都必须兜住：

1. ``major.opus.summary`` **不是字符串**，是富文本对象
   ``{"rich_text_nodes":[{orig_text, text, ...}]}``；
2. ``/x/polymer/web-dynamic/v1/detail`` 与 ``.../opus/detail`` 的 ``modules``
   形状不同：前者是 **dict**（``module_author`` / ``module_dynamic`` / …），
   后者是 **list**（``{module_type, module_content}``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import KIND_BLOCKED, KIND_PARSE
from ..timeutil import ts_to_dt


# --------------------------------------------------------------------------- #
# 富文本 / 段落
# --------------------------------------------------------------------------- #
def rich_text(obj: Any) -> str:
    """B 站富文本对象 → 纯文本。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    nodes = obj.get("rich_text_nodes")
    if isinstance(nodes, list):
        parts = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            parts.append(str(node.get("orig_text") or node.get("text") or ""))
        return "".join(parts)
    if isinstance(obj.get("text"), str):
        return obj["text"]
    return ""


def emoji_text(node: dict) -> str:
    """表情节点 → 可读替代文本（需求 3.2.3：表情保留替代文本）。"""
    for holder in (node, node.get("word") if isinstance(node.get("word"), dict) else {}):
        if not isinstance(holder, dict):
            continue
        emoji = holder.get("emoji")
        if isinstance(emoji, dict):
            text = str(emoji.get("text") or "").strip()
            if text:
                return text
            if emoji.get("icon_url"):
                return "[表情]"
        if isinstance(emoji, str) and emoji.strip():
            return emoji.strip()
    return "[表情]"


def text_block_text(text_obj: Any) -> str:
    """``paragraphs[].text`` → 文本（兼容 nodes/word 与纯字符串两种写法）。"""
    if text_obj is None:
        return ""
    if isinstance(text_obj, str):
        return text_obj
    if not isinstance(text_obj, dict):
        return ""
    nodes = text_obj.get("nodes")
    if not isinstance(nodes, list):
        return rich_text(text_obj)
    chunks: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type not in (None, 1, "1") and node.get("emoji") is not None:
            chunks.append(emoji_text(node))
            continue
        word = node.get("word")
        if isinstance(word, dict):
            if word.get("emoji") is not None and not word.get("words"):
                chunks.append(emoji_text(node))
                continue
            chunks.append(str(word.get("words") or ""))
            continue
        if isinstance(word, str):
            chunks.append(word)
            continue
        if node.get("emoji") is not None:
            chunks.append(emoji_text(node))
        elif isinstance(node.get("text"), str):
            chunks.append(node["text"])
    return "".join(chunks)


@dataclass
class Block:
    """正文块：文本 / 小标题 / 图片（图片保留原顺序，供 Markdown 与长图排版共用）。"""

    kind: str = "text"           # text | heading | image | unknown
    text: str = ""
    pics: list[dict] = field(default_factory=list)
    note: str = ""
    level: int = 0               # heading 级别（1~6）

    @property
    def is_image(self) -> bool:
        return self.kind == "image"


def _pic_entry(raw: dict) -> dict:
    """统一图片字段（阶段 0 第 3.7 节：url/width/height/size/type）。"""
    return {
        "url": str(raw.get("url") or raw.get("src") or ""),
        "width": raw.get("width"),
        "height": raw.get("height"),
        "size": raw.get("size"),
        "type": raw.get("type"),
    }


def opus_blocks(module_content: dict) -> list[Block]:
    """``MODULE_TYPE_CONTENT.module_content`` → 有序正文块。"""
    if not isinstance(module_content, dict):
        return []
    paragraphs = module_content.get("paragraphs")
    if not isinstance(paragraphs, list):
        # 降级形态：{content: str, pics: [...]}
        blocks: list[Block] = []
        content = module_content.get("content")
        if isinstance(content, str) and content.strip():
            blocks.append(Block(kind="text", text=content))
        pics = module_content.get("pics")
        if isinstance(pics, list) and pics:
            blocks.append(Block(kind="image", pics=[_pic_entry(p) for p in pics if isinstance(p, dict)]))
        return blocks

    blocks = []
    for para in paragraphs:
        if not isinstance(para, dict):
            continue
        pic_holder = para.get("pic") if isinstance(para.get("pic"), dict) else {}
        pics = pic_holder.get("pics") if isinstance(pic_holder.get("pics"), list) else None
        if pics is None and isinstance(para.get("pics"), list):
            pics = para["pics"]
        if pics:
            blocks.append(Block(kind="image", pics=[_pic_entry(p) for p in pics if isinstance(p, dict)]))
            continue
        text = text_block_text(para.get("text"))
        para_type = para.get("para_type")
        if para_type == 9:
            # 小标题段落：``format.heading_type`` 给级别（语义未在阶段 0 验证，
            # 取不到时按三级标题渲染，宁可保守也不丢结构）
            fmt = para.get("format") if isinstance(para.get("format"), dict) else {}
            level = fmt.get("heading_type")
            try:
                level = int(level)
            except (TypeError, ValueError):
                level = 3
            level = level if 1 <= level <= 6 else 3
            if text.strip():
                blocks.append(Block(kind="heading", text=text, level=level))
            continue
        if text.strip():
            blocks.append(Block(kind="text", text=text))
        elif para_type not in (None, 1):
            # 未知段落类型：保留说明，不用空白替代正文（需求 3.4.1）
            blocks.append(Block(kind="unknown", note=f"未识别的段落类型 para_type={para_type}"))
    return blocks


# --------------------------------------------------------------------------- #
# opus 详情（modules 是 list）
# --------------------------------------------------------------------------- #
@dataclass
class OpusDoc:
    id_str: str = ""
    item_type: Any = None
    article_type: Any = None
    is_only_fans: bool = False
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    pub_ts: int = 0
    author_name: str = ""
    author_mid: int = 0
    author_face: str = ""
    blocked: bool = False
    error_kind: str = ""

    @property
    def published_at(self):
        return ts_to_dt(self.pub_ts)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.kind == "text" and b.text.strip())

    @property
    def images(self) -> list[dict]:
        out: list[dict] = []
        for block in self.blocks:
            if block.is_image:
                out.extend(block.pics)
        return out

    @property
    def is_article(self) -> bool:
        """专栏判别位（阶段 0 第 3.5.3 节）：``item.type==1`` / ``article_type != 0``。"""
        return str(self.item_type) == "1" or str(self.article_type) not in ("", "0", "None")


def parse_opus_detail(data: Any) -> OpusDoc | None:
    """解析 ``/x/polymer/web-dynamic/v1/opus/detail`` 的 ``data``。"""
    if not isinstance(data, dict):
        return None
    item = data.get("item")
    if not isinstance(item, dict):
        return None
    doc = OpusDoc(id_str=str(item.get("id_str") or item.get("id") or ""))
    doc.item_type = item.get("type")
    basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
    doc.article_type = basic.get("article_type")
    doc.is_only_fans = bool(basic.get("is_only_fans"))
    modules = item.get("modules")
    if not isinstance(modules, list):
        return doc
    for module in modules:
        if not isinstance(module, dict):
            continue
        mtype = module.get("module_type")
        if mtype == "MODULE_TYPE_TITLE":
            title = module.get("module_title") if isinstance(module.get("module_title"), dict) else {}
            doc.title = str(title.get("text") or "")
        elif mtype == "MODULE_TYPE_AUTHOR":
            author = module.get("module_author") if isinstance(module.get("module_author"), dict) else {}
            doc.author_name = str(author.get("name") or "")
            doc.author_mid = int(author.get("mid") or 0)
            doc.author_face = str(author.get("face") or "")
            doc.pub_ts = int(author.get("pub_ts") or 0)
        elif mtype == "MODULE_TYPE_CONTENT":
            doc.blocks = opus_blocks(module.get("module_content") or {})
        elif mtype == "MODULE_TYPE_BLOCKED":
            # 充电正文被门控（阶段 0 第 3.4.3 / 3.5.4 节）
            doc.blocked = True
            doc.error_kind = KIND_BLOCKED
    return doc


# --------------------------------------------------------------------------- #
# 动态（modules 是 dict；feed 列表项与 detail 项同形）
# --------------------------------------------------------------------------- #
@dataclass
class DynamicDoc:
    id_str: str = ""
    dyn_type: str = ""
    is_only_fans: bool = False
    pinned: bool = False
    pub_ts: int = 0
    author_name: str = ""
    author_mid: int = 0
    author_face: str = ""
    badge: str = ""
    desc: str = ""
    major_type: str = ""
    major: dict = field(default_factory=dict)
    orig: dict | None = None
    blocks: list[Block] = field(default_factory=list)
    stat: dict = field(default_factory=dict)
    error_kind: str = ""
    parse_note: str = ""

    @property
    def published_at(self):
        return ts_to_dt(self.pub_ts)

    @property
    def visible(self) -> bool:
        return self.pub_ts > 0


def major_text(major: Any) -> str:
    """按 ``major.type`` 取卡片文本（阶段 0 第 3.4.2 节的字段路径表）。"""
    if not isinstance(major, dict):
        return ""
    mtype = str(major.get("type") or "")
    parts: list[str] = []
    if mtype == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
        title = str(opus.get("title") or "").strip()
        if title:
            parts.append(f"**{title}**")
        summary = rich_text(opus.get("summary"))
        if summary:
            parts.append(summary)
    elif mtype == "MAJOR_TYPE_DRAW":
        draw = major.get("draw") if isinstance(major.get("draw"), dict) else {}
        parts.append(rich_text(draw.get("desc")))
    elif mtype == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
        title = str(archive.get("title") or "").strip()
        if title:
            parts.append(f"**{title}**")
        parts.append(rich_text(archive.get("desc")))
    elif mtype == "MAJOR_TYPE_ARTICLE":
        article = major.get("article") if isinstance(major.get("article"), dict) else {}
        title = str(article.get("title") or "").strip()
        if title:
            parts.append(f"**{title}**")
        parts.append(rich_text(article.get("desc")))
    elif mtype == "MAJOR_TYPE_COMMON":
        common = major.get("common") if isinstance(major.get("common"), dict) else {}
        title = str(common.get("title") or "").strip()
        if title:
            parts.append(f"**{title}**")
        parts.append(rich_text(common.get("desc")))
    elif mtype == "MAJOR_TYPE_LIVE_RCMD":
        live = major.get("live_rcmd") if isinstance(major.get("live_rcmd"), dict) else {}
        content = live.get("content")
        parts.append(rich_text(content) if content else "（直播推荐卡）")
    elif mtype in ("MAJOR_TYPE_PGC",):
        pgc = major.get("pgc") if isinstance(major.get("pgc"), dict) else {}
        parts.append(str(pgc.get("title") or ""))
    return "\n\n".join(p for p in parts if p and p.strip())


def major_images(major: Any) -> list[dict]:
    """卡片携带的图片（封面 / 图文配图）。"""
    if not isinstance(major, dict):
        return []
    mtype = str(major.get("type") or "")
    out: list[dict] = []
    if mtype == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
        for pic in opus.get("pics") or []:
            if isinstance(pic, dict):
                out.append(_pic_entry(pic))
    elif mtype == "MAJOR_TYPE_DRAW":
        draw = major.get("draw") if isinstance(major.get("draw"), dict) else {}
        for pic in draw.get("items") or []:
            if isinstance(pic, dict):
                out.append(_pic_entry(pic))
    elif mtype == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
        cover = archive.get("cover")
        if isinstance(cover, str) and cover:
            out.append(_pic_entry({"url": cover}))
        elif isinstance(cover, dict):
            out.append(_pic_entry(cover))
    elif mtype == "MAJOR_TYPE_ARTICLE":
        article = major.get("article") if isinstance(major.get("article"), dict) else {}
        for cover in article.get("covers") or []:
            if isinstance(cover, dict):
                out.append(_pic_entry(cover))
            elif isinstance(cover, str):
                out.append(_pic_entry({"url": cover}))
    return [p for p in out if p.get("url")]


def major_note(major: Any) -> str:
    """无法完整渲染的部分在元数据里标记（需求 3.2.3）。"""
    if not isinstance(major, dict):
        return ""
    mtype = str(major.get("type") or "")
    if mtype == "MAJOR_TYPE_LIVE_RCMD":
        return "直播推荐卡：仅保留卡片文本与链接，不采集直播内容（需求 §7）"
    if mtype == "MAJOR_TYPE_COMMON":
        common = major.get("common") if isinstance(major.get("common"), dict) else {}
        if str(common.get("jump_url") or "").startswith("https://member.bilibili.com"):
            return "充电专属问答卡：正文由平台侧承载，仅保留标题/描述与跳转链接"
    if mtype == "MAJOR_TYPE_ARCHIVE":
        return "视频卡片：视频本体按 BV 号独立归档，动态这里只保留卡片信息"
    if mtype in ("MAJOR_TYPE_MEDIALIST", "MAJOR_TYPE_COURSES", "MAJOR_TYPE_MUSIC",
                 "MAJOR_TYPE_SUBSCRIPTION", "MAJOR_TYPE_UGC_SEASON"):
        return f"{mtype}：平台卡片类型，阶段 1 仅保留可获取的文本/链接与类型说明"
    return ""


def parse_dynamic_item(item: Any) -> DynamicDoc | None:
    """解析动态列表项或 ``/detail`` 的 ``item``（``modules`` 为 dict）。"""
    if not isinstance(item, dict):
        return None
    doc = DynamicDoc(id_str=str(item.get("id_str") or item.get("id") or ""))
    doc.dyn_type = str(item.get("type") or "")
    basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
    doc.is_only_fans = bool(basic.get("is_only_fans"))
    modules = item.get("modules")
    if isinstance(modules, list):
        # opus/detail 形状（list）→ 复用 opus 解析，正文以 blocks 呈现
        opus_doc = parse_opus_detail({"item": item})
        if opus_doc is not None:
            doc.pub_ts = opus_doc.pub_ts
            doc.author_name = opus_doc.author_name
            doc.author_mid = opus_doc.author_mid
            doc.author_face = opus_doc.author_face
            doc.blocks = opus_doc.blocks
            doc.error_kind = opus_doc.error_kind
            doc.major_type = "MAJOR_TYPE_OPUS"
        return doc
    if not isinstance(modules, dict):
        doc.parse_note = "modules 缺失或形状未知"
        return doc

    author = modules.get("module_author") if isinstance(modules.get("module_author"), dict) else {}
    doc.author_name = str(author.get("name") or "")
    doc.author_mid = int(author.get("mid") or 0)
    doc.author_face = str(author.get("face") or "")
    doc.pub_ts = int(author.get("pub_ts") or 0)
    badge = author.get("icon_badge") if isinstance(author.get("icon_badge"), dict) else {}
    doc.badge = str(badge.get("text") or "")
    tag = modules.get("module_tag")
    if isinstance(tag, dict):
        doc.pinned = str(tag.get("text") or "").strip() == "置顶"
    elif isinstance(tag, list):
        doc.pinned = any(
            isinstance(t, dict) and str(t.get("text") or "").strip() == "置顶" for t in tag
        )
    if author.get("is_top") is True:
        doc.pinned = True

    dynamic = modules.get("module_dynamic") if isinstance(modules.get("module_dynamic"), dict) else {}
    doc.desc = rich_text(dynamic.get("desc"))
    major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}
    doc.major = major
    doc.major_type = str(major.get("type") or "")
    doc.parse_note = major_note(major)
    stat = modules.get("module_stat")
    if isinstance(stat, dict):
        doc.stat = {
            "like": (stat.get("like") or {}).get("count") if isinstance(stat.get("like"), dict) else None,
            "comment": (stat.get("comment") or {}).get("count") if isinstance(stat.get("comment"), dict) else None,
            "forward": (stat.get("forward") or {}).get("count") if isinstance(stat.get("forward"), dict) else None,
        }
    orig = item.get("orig")
    if isinstance(orig, dict) and orig:
        doc.orig = orig
    if str(item.get("type") or "") == "DYNAMIC_TYPE_FORWARD":
        orig_doc = parse_dynamic_item(doc.orig) if doc.orig else None
        if orig_doc is None:
            doc.parse_note = "转发动态：转发原文缺失（可能已被删除或权限不足）"
    return doc


def dynamic_body_text(doc: DynamicDoc) -> str:
    """动态正文（优先 opus 段落，其次 desc + 卡片文本）。"""
    parts: list[str] = []
    if doc.blocks:
        parts.append("\n\n".join(b.text for b in doc.blocks if b.kind == "text" and b.text.strip()))
    if doc.desc.strip():
        parts.append(doc.desc.strip())
    card = major_text(doc.major)
    if card.strip() and card.strip() not in parts:
        parts.append(card.strip())
    return "\n\n".join(p for p in parts if p.strip())


def dynamic_images(doc: DynamicDoc) -> list[dict]:
    """动态配图：opus 段落图片优先，其次卡片封面/配图。"""
    seen: set[str] = set()
    out: list[dict] = []
    for block in doc.blocks:
        if not block.is_image:
            continue
        for pic in block.pics:
            url = pic.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                out.append(pic)
    for pic in major_images(doc.major):
        url = pic.get("url") or ""
        if url and url not in seen:
            seen.add(url)
            out.append(pic)
    return out


# --------------------------------------------------------------------------- #
# 视频
# --------------------------------------------------------------------------- #
@dataclass
class VideoDoc:
    bvid: str = ""
    aid: int = 0
    title: str = ""
    desc: str = ""
    pub_ts: int = 0
    duration: int = 0
    owner_name: str = ""
    owner_mid: int = 0
    owner_face: str = ""
    cover: str = ""
    is_upower_exclusive: bool = False
    rights: dict = field(default_factory=dict)
    pages: list[dict] = field(default_factory=list)
    stat: dict = field(default_factory=dict)
    error_kind: str = ""

    @property
    def published_at(self):
        return ts_to_dt(self.pub_ts)


@dataclass
class PageInfo:
    cid: int = 0
    page: int = 0
    part: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0


@dataclass
class PlayAccess:
    """播放地址形态 —— 判定充电权限的**唯一**可靠依据（阶段 0 第 3.3.4 节）。"""

    ok: bool = False
    dash_video_streams: int = 0
    dash_audio_streams: int = 0
    durl_segments: int = 0
    accept_quality: list = field(default_factory=list)
    accept_description: list = field(default_factory=list)
    error_kind: str = ""
    message: str = ""

    @property
    def mode(self) -> str:
        if self.dash_video_streams > 0:
            return "full"
        if self.durl_segments > 0 or self.ok:
            return "preview_only"
        return "unknown"


def parse_page_list(data: Any) -> list[PageInfo]:
    out: list[PageInfo] = []
    if isinstance(data, dict):
        raw_pages = data.get("pages") or []
    elif isinstance(data, list):
        raw_pages = data
    else:
        raw_pages = []
    for raw in raw_pages:
        if not isinstance(raw, dict):
            continue
        dimension = raw.get("dimension") if isinstance(raw.get("dimension"), dict) else {}
        out.append(
            PageInfo(
                cid=int(raw.get("cid") or 0),
                page=int(raw.get("page") or 0),
                part=str(raw.get("part") or ""),
                duration=int(raw.get("duration") or 0),
                width=int(dimension.get("width") or 0),
                height=int(dimension.get("height") or 0),
            )
        )
    return out


def parse_video_detail(data: Any) -> VideoDoc | None:
    if not isinstance(data, dict):
        return None
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    doc = VideoDoc(
        bvid=str(data.get("bvid") or ""),
        aid=int(data.get("aid") or 0),
        title=str(data.get("title") or ""),
        desc=str(data.get("desc") or ""),
        pub_ts=int(data.get("pubdate") or data.get("ctime") or 0),
        duration=int(data.get("duration") or 0),
        owner_name=str(owner.get("name") or ""),
        owner_mid=int(owner.get("mid") or 0),
        owner_face=str(owner.get("face") or ""),
        cover=str(data.get("pic") or ""),
        is_upower_exclusive=bool(data.get("is_upower_exclusive")),
        rights=data.get("rights") if isinstance(data.get("rights"), dict) else {},
        pages=[
            {
                "cid": p.get("cid"),
                "page": p.get("page"),
                "part": p.get("part"),
                "duration": p.get("duration"),
            }
            for p in (data.get("pages") or [])
            if isinstance(p, dict)
        ],
        stat=data.get("stat") if isinstance(data.get("stat"), dict) else {},
    )
    return doc


def parse_playurl(data: Any) -> PlayAccess:
    if not isinstance(data, dict):
        return PlayAccess(ok=False, error_kind=KIND_PARSE, message="playurl data 缺失")
    dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
    return PlayAccess(
        ok=True,
        dash_video_streams=len(dash.get("video") or []),
        dash_audio_streams=len(dash.get("audio") or []),
        durl_segments=len(data.get("durl") or []),
        accept_quality=list(data.get("accept_quality") or []),
        accept_description=list(data.get("accept_description") or []),
    )
