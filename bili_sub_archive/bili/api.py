"""接口白名单与条目装配（业务代码唯一可用的采集入口）。

阶段 0 第 5.1 节的白名单被实装为 :data:`WHITELIST` 表：所有请求的 path / wbi
参数都从表里取，业务层因此**无法**绕开白名单直接拼 URL（计划 2.1-5）。

统一输出契约（计划 3.1 / 阶段 0 第 5.2 节第 1 条）：
``kind`` / ``id`` / ``published_at`` / ``url`` / ``title`` / ``author`` / ``raw_ref``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..errors import KIND_PARSE
from ..models import KIND_ARTICLE, KIND_DYNAMIC, KIND_VIDEO, Item
from ..timeutil import ts_to_dt
from .client import ApiResult, DownloadResult, HttpClient
from .parse import (
    DynamicDoc,
    OpusDoc,
    PlayAccess,
    dynamic_body_text,
    parse_dynamic_item,
    parse_opus_detail,
    parse_page_list,
    parse_playurl,
)

WWW = "https://www.bilibili.com"

#: 接口白名单：``名称 -> (path, 是否需要 WBI, 最低身份)``
WHITELIST: dict[str, tuple[str, bool, str]] = {
    "nav": ("/x/web-interface/nav", False, "anon"),
    "card": ("/x/web-interface/card", False, "anon"),
    "video_list": ("/x/space/wbi/arc/search", True, "login"),
    "video_detail": ("/x/web-interface/view", False, "anon"),
    "page_list": ("/x/player/pagelist", False, "anon"),
    "subtitle_list": ("/x/player/wbi/v2", True, "login"),
    "playurl": ("/x/player/playurl", False, "login"),
    "dynamic_feed": ("/x/polymer/web-dynamic/v1/feed/space", True, "login"),
    "dynamic_detail": ("/x/polymer/web-dynamic/v1/detail", True, "login"),
    "opus_feed": ("/x/polymer/web-dynamic/v1/opus/feed/space", True, "login"),
    "opus_detail": ("/x/polymer/web-dynamic/v1/opus/detail", True, "login"),
    "article_list": ("/x/space/wbi/article", True, "login"),
    "article_view": ("/x/article/view", False, "anon"),
}

#: 明确不使用（阶段 0 第 5.1 节）：`/x/space/wbi/acc/info`（匿名 -352）、
#: `/x/player/wbi/playurl`（恒 HTTP 412）、`/x/space/article`（无条目）、空间页 SSR。

#: 动态流的 features 参数（阶段 0 探针同款，含充电专属可见性开关）
DYNAMIC_FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote"
SPACE_VIDEO_PS = 30
SPACE_ARTICLE_PS = 30


def path_of(name: str) -> str:
    return WHITELIST[name][0]


def summarize(text: str, limit: int = 40) -> str:
    """正文 → 可读摘要（目录名用，收在同一行）。"""
    raw = " ".join(str(text or "").split())
    raw = raw.replace("#", "").strip()
    if not raw:
        return ""
    return raw if len(raw) <= limit else raw[:limit].rstrip() + "…"


def space_url(mid: int, suffix: str) -> str:
    return f"https://space.bilibili.com/{int(mid)}/{suffix}"


@dataclass
class AuthorInfo:
    mid: int = 0
    name: str = ""
    face: str = ""
    fans: int = 0
    sign: str = ""


# --------------------------------------------------------------------------- #
# 业务层依赖的接口协议（测试注入替身用）
# --------------------------------------------------------------------------- #
@runtime_checkable
class BiliApi(Protocol):
    def nav(self) -> ApiResult: ...
    def card(self, mid: int) -> ApiResult: ...
    def video_list(self, mid: int, pn: int = 1, ps: int = SPACE_VIDEO_PS, *,
                   order: str = "pubdate", special_type: str = "") -> ApiResult: ...
    def video_detail(self, bvid: str) -> ApiResult: ...
    def page_list(self, bvid: str) -> ApiResult: ...
    def playurl(self, bvid: str, cid: int) -> ApiResult: ...
    def dynamic_feed(self, mid: int, offset: str = "") -> ApiResult: ...
    def dynamic_detail(self, dyn_id: str) -> ApiResult: ...
    def opus_feed(self, mid: int, page: int = 1, offset: str = "", *,
                  type: str = "all") -> ApiResult: ...
    def opus_detail(self, opus_id: str) -> ApiResult: ...
    def article_list(self, mid: int, pn: int = 1, ps: int = SPACE_ARTICLE_PS) -> ApiResult: ...
    def article_view(self, cvid: str) -> ApiResult: ...
    def download(self, url: str, dest: Path, *, referer: str = "") -> DownloadResult: ...


class BilibiliApi:
    """白名单接口封装：只暴露"取数据"的方法，错误以 :class:`ApiResult` 返回。"""

    def __init__(self, client: HttpClient, logger=None):
        self.client = client
        self.logger = logger
        self.stats: dict[str, int] = {}

    # ---------------- 传输 ----------------
    def _call(self, name: str, params: dict | None = None, *, referer: str = "",
              origin: str | None = None, retries: int | None = None) -> ApiResult:
        if name not in WHITELIST:
            raise KeyError(f"接口 {name} 不在白名单内（阶段 0 第 5.1 节）")
        path, wbi, _identity = WHITELIST[name]
        self.stats[name] = self.stats.get(name, 0) + 1
        result = self.client.get_json(path, params, wbi=wbi, referer=referer,
                                      origin=origin, retries=retries)
        if self.logger is not None:
            self.logger.debug(f"GET {name} {path} → {result.brief()}")
        return result

    def download(self, url: str, dest: Path, *, referer: str = "") -> DownloadResult:
        return self.client.download(url, dest, referer=referer)

    # ---------------- 账号与作者 ----------------
    def nav(self) -> ApiResult:
        return self.client.nav(force=True)

    def login_state(self) -> tuple[bool, int, str]:
        """``(是否登录, mid, 说明)``。Cookie 失效与未登录同为 -101，提示语合并。"""
        result = self.nav()
        if result.ok:
            data = result.data or {}
            return bool(data.get("isLogin")), int(data.get("mid") or 0), "已登录"
        if result.code == -101:
            return False, 0, "登录态无效或已过期（未登录与 Cookie 失效在接口层不可区分）"
        return False, 0, f"{result.kind_label}：{result.message}"

    def card(self, mid: int) -> ApiResult:
        return self._call("card", {"mid": int(mid)}, referer=space_url(mid, ""))

    def author_info(self, mid: int) -> tuple[AuthorInfo | None, ApiResult]:
        result = self.card(mid)
        if not result.ok:
            return None, result
        card = ((result.data or {}).get("card") or {}) if isinstance(result.data, dict) else {}
        info = AuthorInfo(
            mid=int(card.get("mid") or mid),
            name=str(card.get("name") or ""),
            face=str(card.get("face") or ""),
            fans=int(card.get("fans") or 0),
            sign=str(card.get("sign") or ""),
        )
        return info, result

    # ---------------- 视频 ----------------
    def video_list(self, mid: int, pn: int = 1, ps: int = SPACE_VIDEO_PS, *,
                   order: str = "pubdate", special_type: str = "") -> ApiResult:
        params: dict[str, Any] = {
            "mid": int(mid), "pn": int(pn), "ps": int(ps), "order": order,
            "tid": 0, "platform": "web", "web_location": 1550101,
        }
        if special_type:
            params["special_type"] = special_type
            params["order_avoided"] = "true"
        return self._call("video_list", params, referer=space_url(mid, "video"))

    def video_detail(self, bvid: str) -> ApiResult:
        return self._call("video_detail", {"bvid": bvid},
                          referer=f"{WWW}/video/{bvid}")

    def page_list(self, bvid: str) -> ApiResult:
        return self._call("page_list", {"bvid": bvid},
                          referer=f"{WWW}/video/{bvid}")

    def subtitle_list(self, bvid: str, cid: int) -> ApiResult:
        """字幕列表（阶段 2 使用）。未登录时平台返回**空数组**而非错误（阶段 0 第 3.3.3 节）。"""
        return self._call("subtitle_list", {"bvid": bvid, "cid": int(cid)},
                          referer=f"{WWW}/video/{bvid}")

    def playurl(self, bvid: str, cid: int) -> ApiResult:
        return self._call(
            "playurl",
            {"bvid": bvid, "cid": int(cid), "qn": 80, "fnval": 4048, "fnver": 0,
             "fourk": 1, "platform": "pc", "high_quality": 1},
            referer=f"{WWW}/video/{bvid}",
        )

    def pages(self, bvid: str) -> tuple[list, ApiResult]:
        result = self.page_list(bvid)
        if not result.ok:
            return [], result
        return parse_page_list(result.data), result

    def access(self, bvid: str, cid: int) -> PlayAccess:
        """充电权限判定：只看媒体流形态，不看 ``code``（阶段 0 第 3.3.4 节）。"""
        result = self.playurl(bvid, cid)
        if not result.ok:
            return PlayAccess(ok=False, error_kind=result.error_kind, message=result.message)
        return parse_playurl(result.data)

    # ---------------- 动态 ----------------
    def dynamic_feed(self, mid: int, offset: str = "") -> ApiResult:
        params = {
            "host_mid": int(mid),
            "offset": offset or "",
            "timezone_offset": -480,
            "platform": "web",
            "features": DYNAMIC_FEATURES,
            "web_location": 333.1387,
        }
        return self._call("dynamic_feed", params, referer=space_url(mid, "dynamic"))

    def dynamic_detail(self, dyn_id: str) -> ApiResult:
        params = {
            "id": str(dyn_id),
            "timezone_offset": -480,
            "features": DYNAMIC_FEATURES,
        }
        return self._call("dynamic_detail", params, referer=f"{WWW}/opus/{dyn_id}")

    # ---------------- 图文 / 专栏（opus） ----------------
    def opus_feed(self, mid: int, page: int = 1, offset: str = "", *,
                  type: str = "all") -> ApiResult:
        params = {"host_mid": int(mid), "page": int(page), "offset": offset or "", "type": type}
        return self._call("opus_feed", params, referer=space_url(mid, "article"))

    def opus_detail(self, opus_id: str) -> ApiResult:
        return self._call("opus_detail", {"id": str(opus_id)},
                          referer=f"{WWW}/opus/{opus_id}")

    def opus_doc(self, opus_id: str) -> tuple[OpusDoc | None, ApiResult]:
        result = self.opus_detail(opus_id)
        if not result.ok:
            return None, result
        doc = parse_opus_detail(result.data)
        if doc is None:
            result.error_kind = KIND_PARSE
            result.message = "opus/detail 响应结构不符合预期"
        return doc, result

    # ---------------- 专栏（旧格式 read/cv） ----------------
    def article_list(self, mid: int, pn: int = 1, ps: int = SPACE_ARTICLE_PS) -> ApiResult:
        return self._call(
            "article_list",
            {"mid": int(mid), "pn": int(pn), "ps": int(ps), "sort": "publish_time"},
            referer=space_url(mid, "article"),
        )

    def article_view(self, cvid: str) -> ApiResult:
        cid = str(cvid).lower().removeprefix("cv")
        return self._call("article_view", {"id": cid},
                          referer=f"{WWW}/read/cv{cid}")


# --------------------------------------------------------------------------- #
# 原始条目 → 统一 Item
# --------------------------------------------------------------------------- #
def video_item(raw: dict, author: str) -> Item | None:
    """``arc/search`` 的 ``vlist[]`` → Item。"""
    if not isinstance(raw, dict):
        return None
    bvid = str(raw.get("bvid") or "")
    if not bvid:
        return None
    published = ts_to_dt(raw.get("created"))
    return Item(
        kind=KIND_VIDEO,
        platform_id=bvid,
        title=str(raw.get("title") or bvid).strip(),
        url=f"{WWW}/video/{bvid}",
        author=author,
        published_at=published,
        raw_ref={
            "api": path_of("video_list"),
            "aid": raw.get("aid"),
            "bvid": bvid,
            "format": "arc_search",
        },
        extras={
            "is_charging_arc": bool(raw.get("is_charging_arc")),
            "elec_arc_badge": str(raw.get("elec_arc_badge") or ""),
            "duration": raw.get("length") or raw.get("duration"),
            "cover": str(raw.get("pic") or ""),
            "play": raw.get("play"),
            "comment": raw.get("comment"),
            "description": str(raw.get("description") or ""),
            "pinned": False,
        },
    )


def mark_pinned_videos(items: list[Item]) -> None:
    """视频列表置顶识别：首条发布时间早于第二条即为置顶（阶段 0 第 3.3.1 节）。"""
    if len(items) < 2:
        return
    first, second = items[0], items[1]
    if first.published_at and second.published_at and first.published_ts < second.published_ts:
        first.extras["pinned"] = True


def dynamic_item(raw: dict, author: str, mid: int) -> tuple[Item | None, DynamicDoc | None]:
    """``feed/space`` 的 ``items[]`` → Item + 解析后的 DynamicDoc。"""
    doc = parse_dynamic_item(raw)
    if doc is None or not doc.id_str:
        return None, doc
    body = dynamic_body_text(doc) if doc.desc or doc.major_type or doc.blocks else ""
    title = summarize(body) or f"{doc.dyn_type or '动态'}"
    return (
        Item(
            kind=KIND_DYNAMIC,
            platform_id=doc.id_str,
            title=title,
            url=f"{WWW}/opus/{doc.id_str}",
            author=author,
            published_at=doc.published_at,
            raw_ref={
                "api": path_of("dynamic_feed"),
                "dynamic_type": doc.dyn_type,
                "major_type": doc.major_type,
                "format": "dynamic",
                "mid": int(mid),
            },
            extras={
                "is_only_fans": doc.is_only_fans,
                "pinned": doc.pinned,
                "badge": doc.badge,
                "summary": title,
            },
        ),
        doc,
    )


def opus_item(raw: dict, author: str, mid: int) -> Item | None:
    """``opus/feed/space`` 的 ``items[]`` → Item。

    该列表**不提供发布时间**（``pub_time`` 为空串，阶段 0 第 3.4.1 / 3.5 节），
    因此 ``published_at`` 置空，由发现阶段逐条取详情补齐。
    """
    if not isinstance(raw, dict):
        return None
    opus_id = str(raw.get("opus_id") or "")
    if not opus_id:
        return None
    badge = raw.get("badge") if isinstance(raw.get("badge"), dict) else {}
    text = str(raw.get("content") or "")
    return Item(
        kind=KIND_ARTICLE,
        platform_id=opus_id,
        title=summarize(text) or f"opus {opus_id}",
        url=f"{WWW}/opus/{opus_id}",
        author=author,
        published_at=None,
        raw_ref={
            "api": path_of("opus_feed"),
            "format": "opus",
            "mid": int(mid),
            "list_content": text[:500],
        },
        extras={
            "badge": str(badge.get("text") or ""),
            "cover": ((raw.get("cover") or {}) or {}).get("url") if isinstance(raw.get("cover"), dict) else "",
            "unknown_publish_time": True,
        },
    )


def legacy_article_item(raw: dict, author: str, mid: int) -> Item | None:
    """``/x/space/wbi/article`` 的 ``articles[]`` → Item（旧格式 read/cv）。"""
    if not isinstance(raw, dict):
        return None
    cvid = raw.get("id")
    if cvid in (None, ""):
        return None
    author_block = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return Item(
        kind=KIND_ARTICLE,
        platform_id=f"cv{cvid}",
        title=str(raw.get("title") or f"cv{cvid}").strip(),
        url=f"{WWW}/read/cv{cvid}",
        author=str(author_block.get("name") or author),
        published_at=ts_to_dt(raw.get("publish_time") or raw.get("ctime")),
        raw_ref={
            "api": path_of("article_list"),
            "cv_id": cvid,
            "format": "legacy_cv",
            "mid": int(mid),
        },
        extras={
            "summary": str(raw.get("summary") or ""),
            "words": raw.get("words"),
            "cover": str(raw.get("banner_url") or ""),
            "is_only_fans": False,
        },
    )
