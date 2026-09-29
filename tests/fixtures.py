"""离线测试替身：合成响应样本 + ``FakeApi`` + 阶段 2/3 的下载器/转写器/模型替身。

样本结构严格照抄阶段 0 记录的**真实字段路径**
（``docs/archive/stage0-evidence/raw/*.json`` 与
``docs/archive/stage0-platform-verification.md``），
使离线测试能覆盖：

- ``modules`` 的双形状（``/detail`` 是 dict，``opus/detail`` 是 list）；
- 富文本 ``summary.rich_text_nodes``、``paragraphs[]`` 的 ``para_type`` 1/2/9；
- 置顶标记（``module_tag.text == "置顶"``）、充电专属（``is_only_fans``）；
- 充电视频无权限时"``code=0`` 但只有单段 durl"的媒体流形态；
- 列表不提供发布时间（``opus/feed/space``）而必须逐条取详情；
- 阶段 2：字幕列表与字幕文件（``{from,to,content}`` 秒级浮点）、媒体下载与续传；
- 阶段 3：OpenAI 兼容接口的 Chat 替身（分块要点 → 总结 + 大纲）与 mmdc 替身。

阶段 2/3 的替身（:class:`FakeDownloader` / :class:`FakeTranscriber` /
:class:`FakeChatClient` / :func:`fake_mmdc_runner`）**绝不联网、绝不调用 ffmpeg 或
mermaid-cli**，只按协议写出结构合法的产物，用于验证编排/状态/校验逻辑。
"""

from __future__ import annotations

import json
import struct
import threading
import time
from pathlib import Path

from bili_sub_archive.bili.client import ApiResult, DownloadResult, classify
from bili_sub_archive.errors import KIND_OK
from bili_sub_archive.media import MIN_MEDIA_BYTES, MediaFile
from bili_sub_archive.paths import atomic_write_bytes

#: 1x1 PNG（图片下载替身写入的最小合法文件）
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001a5f6457c0000000049454e44ae426082"
)

#: 最小合法 MP4（``ftyp`` 魔数 + 填充到最小阈值以上），供媒体校验走通
MP4_MIN = b"\x00\x00\x00\x20ftypisom" + b"\x00" * max(MIN_MEDIA_BYTES, 8192)

#: 结构非法但足够大的"假媒体"（用于校验失败分支）
NOT_MEDIA = b"<html><body>403 Forbidden</body></html>" + b" " * 8192

AUTHOR_NAME = "测试UP主"
AUTHOR_MID = 1000


# --------------------------------------------------------------------------- #
# 响应包装
# --------------------------------------------------------------------------- #
def ok(data, path: str = "/fake") -> ApiResult:
    return ApiResult(path=path, http_status=200, code=0, message="0", data=data,
                     error_kind=KIND_OK, attempts=1)


def fail(code: int, message: str = "", http_status: int = 200, path: str = "/fake") -> ApiResult:
    return ApiResult(path=path, http_status=http_status, code=code, message=message,
                     data=None, error_kind=classify(code, http_status), attempts=1)


# --------------------------------------------------------------------------- #
# 动态 / opus 样本
# --------------------------------------------------------------------------- #
def para_text(text: str) -> dict:
    return {"para_type": 1, "text": {"nodes": [{"type": 1, "word": {"words": text}}]}}


def para_heading(text: str, level: int = 3) -> dict:
    return {"para_type": 9, "format": {"heading_type": level, "indent": 0},
            "text": {"nodes": [{"type": 1, "word": {"words": text}}]}}


def para_pic(url: str, width: int = 800, height: int = 600) -> dict:
    return {"para_type": 2, "pic": {"pics": [{"url": url, "width": width,
                                              "height": height, "size": 1234, "type": 1}]}}


def rich_text(text: str) -> dict:
    return {"rich_text_nodes": [{"orig_text": text, "text": text}]}


def dynamic_feed_item(
    dyn_id: str,
    pub_ts: int,
    text: str = "",
    *,
    dyn_type: str = "DYNAMIC_TYPE_DRAW",
    major_type: str = "MAJOR_TYPE_OPUS",
    pinned: bool = False,
    is_only_fans: bool = False,
    face: str = "https://i0.hdslb.com/bfs/face/test.jpg",
    major: dict | None = None,
    orig: dict | None = None,
    author_mid: int = AUTHOR_MID,
) -> dict:
    if major is None:
        major = {
            "type": major_type,
            "opus": {
                "title": "",
                "summary": rich_text(text),
                "pics": [],
                "jump_url": f"//www.bilibili.com/opus/{dyn_id}",
            },
        }
    modules = {
        "module_tag": {"text": "置顶"} if pinned else None,
        "module_author": {
            "name": AUTHOR_NAME,
            "mid": author_mid,
            "face": face,
            "pub_ts": str(pub_ts),
            "pub_time": "2026年09月01日 12:00",
            "icon_badge": {"text": "充电专属"} if is_only_fans else None,
            "is_top": False,
        },
        "module_dynamic": {"desc": None, "major": major, "topic": None},
        "module_stat": {
            "like": {"count": 11, "status": True},
            "comment": {"count": 2, "status": True},
            "forward": {"count": 1, "status": True},
        },
    }
    return {
        "id_str": dyn_id,
        "type": dyn_type,
        "visible": True,
        "basic": {
            "is_only_fans": is_only_fans,
            "jump_url": f"//www.bilibili.com/opus/{dyn_id}",
        },
        "modules": modules,
        "orig": orig,
    }


def dynamic_feed_page(items: list[dict], *, offset: str = "", has_more: bool = False) -> dict:
    return {"items": items, "offset": offset, "has_more": has_more, "update_num": 0}


def opus_detail_item(
    item_id: str,
    pub_ts: int,
    title: str = "",
    paragraphs: list[dict] | None = None,
    *,
    item_type: int = 0,
    article_type: int = 0,
    is_only_fans: bool = False,
    blocked: bool = False,
    author_mid: int = AUTHOR_MID,
) -> dict:
    modules: list[dict] = [
        {"module_type": "MODULE_TYPE_TITLE", "module_title": {"text": title}},
        {
            "module_type": "MODULE_TYPE_AUTHOR",
            "module_author": {
                "name": AUTHOR_NAME,
                "mid": author_mid,
                "face": "https://i0.hdslb.com/bfs/face/test.jpg",
                "pub_ts": str(pub_ts),
                "pub_time": "2026年09月01日 12:00",
                "icon_badge": {"text": "充电专属"} if is_only_fans else None,
            },
        },
    ]
    if blocked:
        modules.append({"module_type": "MODULE_TYPE_BLOCKED", "module_blocked": {}})
    else:
        modules.append(
            {
                "module_type": "MODULE_TYPE_CONTENT",
                "module_content": {"paragraphs": paragraphs or []},
            }
        )
    return {
        "id_str": item_id,
        "type": item_type,
        "basic": {
            "article_type": article_type,
            "is_only_fans": is_only_fans,
            "title": f"{title} - 哔哩哔哩",
            "uid": str(author_mid),
        },
        "modules": modules,
    }


def opus_feed_item(opus_id: str, content: str, *, badge: bool = False) -> dict:
    """``opus/feed/space`` 的列表项：**没有发布时间**（阶段 0 第 3.4.1 节）。"""
    return {
        "opus_id": opus_id,
        "jump_url": f"//www.bilibili.com/opus/{opus_id}",
        "content": content,
        "pub_time": "",
        "badge": {"text": "充电专属"} if badge else None,
        "cover": {"url": "https://i0.hdslb.com/bfs/new_dyn/cover.jpg", "width": 1264, "height": 2119},
        "stat": {"view": "", "like": "5"},
    }


def opus_feed_page(items: list[dict], *, offset: str = "", has_more: bool = False) -> dict:
    return {"items": items, "offset": offset, "has_more": has_more}


# --------------------------------------------------------------------------- #
# 视频样本
# --------------------------------------------------------------------------- #
def video_vlist_entry(bvid: str, created: int, title: str, *, is_charging: bool = False) -> dict:
    return {
        "bvid": bvid,
        "aid": int(created % 100000),
        "created": created,
        "title": title,
        "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
        "description": "简介",
        "length": "10:00",
        "play": 1000,
        "comment": 10,
        "is_charging_arc": is_charging,
        "elec_arc_badge": "充电专属" if is_charging else "",
    }


def video_list_page(entries: list[dict], count: int, *, pn: int = 1, ps: int = 30) -> dict:
    return {"list": {"vlist": entries, "tlist": {}}, "page": {"count": count, "pn": pn, "ps": ps}}


def video_detail(
    bvid: str,
    pub_ts: int,
    title: str,
    *,
    pages: list[dict] | None = None,
    is_upower_exclusive: bool = False,
    aid: int = 1,
) -> dict:
    pages = pages or [{"cid": 1001, "page": 1, "part": "P1", "duration": 600,
                       "dimension": {"width": 1920, "height": 1080}}]
    return {
        "bvid": bvid,
        "aid": aid,
        "title": title,
        "desc": "这是简介，不是文字稿",
        "pubdate": pub_ts,
        "ctime": pub_ts,
        "duration": sum(p.get("duration", 0) for p in pages),
        "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
        "owner": {"mid": AUTHOR_MID, "name": AUTHOR_NAME,
                  "face": "https://i0.hdslb.com/bfs/face/test.jpg"},
        "is_upower_exclusive": is_upower_exclusive,
        "rights": {"download": 1, "no_reprint": 1},
        "pages": pages,
        "stat": {"view": 100, "like": 5, "coin": 1, "favorite": 2},
    }


def playurl_full() -> dict:
    return {
        "accept_quality": [80, 64, 32, 16],
        "accept_description": ["1080P 高清", "720P 高清", "480P 清晰", "360P 流畅"],
        "dash": {"video": [{"id": q} for q in (80, 64, 32, 16)], "audio": [{"id": 30280}]},
        "durl": [],
    }


def playurl_preview_only() -> dict:
    """充电视频无权限：``code=0`` 但 dash 为空、只有单段试看 durl。"""
    return {"accept_quality": [16], "accept_description": ["360P 流畅"], "dash": None,
            "durl": [{"order": 1, "length": 60000, "size": 1024, "url": "https://example.invalid/preview"}]}


# --------------------------------------------------------------------------- #
# 字幕样本（阶段 0 第 3.3.3 节：data.subtitle.subtitles[] + body[].from/to/content）
# --------------------------------------------------------------------------- #
def subtitle_entry(lan: str = "ai-zh", lan_doc: str = "中文（AI 生成）",
                   url: str = "", *, is_lock: bool = False, ai_type: int = 0) -> dict:
    return {
        "id": 2078746078580752384,
        "lan": lan,
        "lan_doc": lan_doc,
        "is_lock": is_lock,
        "subtitle_url": url or f"//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/{lan}.json",
        "type": 1,
        "id_str": "2078746078580752384",
        "ai_type": ai_type,
        "ai_status": 2,
    }


def subtitle_body(rows: list[tuple[float, float, str]], lang: str = "zh") -> dict:
    """字幕文件本体：``{from, to, sid, content, ...}``，``from``/``to`` 为秒。"""
    return {
        "font_size": 0.4, "font_color": "#FFFFFF", "type": "AIsubtitle",
        "lang": lang, "version": "v1.7.0.4",
        "body": [
            {"from": start, "to": end, "sid": index + 1, "location": 2,
             "content": text, "music": 0.0}
            for index, (start, end, text) in enumerate(rows)
        ],
    }


def subtitle_list_page(entries: list[dict]) -> dict:
    return {"subtitle": {"allow_submit": False, "lan": "ai-zh", "lan_doc": "中文（AI 生成）",
                         "subtitles": entries, "subtitle_position": "bottom",
                         "font_size_type": 0}}


# --------------------------------------------------------------------------- #
# 专栏（旧格式 read/cv）样本
# --------------------------------------------------------------------------- #
def legacy_article_entry(cvid: int, publish_time: int, title: str) -> dict:
    return {
        "id": cvid,
        "title": title,
        "summary": "摘要",
        "publish_time": publish_time,
        "ctime": publish_time,
        "words": 1200,
        "banner_url": "https://i0.hdslb.com/bfs/new_dyn/banner/cover.png",
        "image_urls": ["https://i0.hdslb.com/bfs/new_dyn/banner/inline.png"],
        "author": {"mid": AUTHOR_MID, "name": AUTHOR_NAME,
                   "face": "https://i0.hdslb.com/bfs/face/test.jpg"},
        "type": 4,
        "reprint": 1,
    }


def legacy_article_view(cvid: int, publish_time: int, title: str, *,
                        html: str = "", paragraphs: list[dict] | None = None,
                        opus_id: str = "") -> dict:
    return {
        "id": cvid,
        "title": title,
        "summary": "摘要",
        "publish_time": publish_time,
        "ctime": publish_time,
        "words": 1200,
        "content": html,
        "image_urls": ["https://i0.hdslb.com/bfs/new_dyn/banner/inline.png"],
        "origin_image_urls": ["https://i0.hdslb.com/bfs/new_dyn/banner/inline.png"],
        "author": {"mid": AUTHOR_MID, "name": AUTHOR_NAME,
                   "face": "https://i0.hdslb.com/bfs/face/test.jpg"},
        "banner_url": "",
        "opus": {
            "opus_id": opus_id,
            "content": {"paragraphs": paragraphs} if paragraphs else {},
            "article": {"cover": [{"url": "https://i0.hdslb.com/bfs/new_dyn/banner/cover.png"}]},
        },
        "dyn_id_str": opus_id,
        "type": 4,
        "reprint": 1,
    }


# --------------------------------------------------------------------------- #
# FakeApi
# --------------------------------------------------------------------------- #
class FakeApi:
    """按 :class:`bili_sub_archive.bili.api.BiliApi` 协议返回合成响应，绝不联网。"""

    def __init__(
        self,
        *,
        dynamics: list[dict] | None = None,
        dynamic_details: dict[str, dict] | None = None,
        opus_details: dict[str, dict] | None = None,
        opus_all: list[dict] | None = None,
        opus_articles: list[dict] | None = None,
        videos: list[dict] | None = None,
        video_count: int | None = None,
        video_details: dict[str, dict] | None = None,
        video_pages: dict[str, list[dict]] | None = None,
        playurls: dict[str, dict] | None = None,
        legacy_articles: list[dict] | None = None,
        legacy_views: dict[str, dict] | None = None,
        subtitles: dict[str, list[dict]] | None = None,
        subtitle_bodies: dict[str, dict] | None = None,
        dynamic_page_size: int = 3,
        opus_page_size: int = 2,
        failures: dict[str, int] | None = None,
    ):
        self.dynamics = dynamics or []
        self.dynamic_details = dynamic_details or {}
        self.opus_details = opus_details or {}
        self.opus_all = opus_all if opus_all is not None else []
        self.opus_articles = opus_articles or []
        self.videos = videos or []
        self.video_count = video_count if video_count is not None else len(self.videos)
        self.video_details = video_details or {}
        self.video_pages = video_pages or {}
        self.playurls = playurls or {}
        self.legacy_articles = legacy_articles or []
        self.legacy_views = legacy_views or {}
        #: ``{"<cid>": [subtitle_entry, ...]}`` —— 该分 P 的平台字幕列表
        self.subtitles = subtitles or {}
        #: ``{"<subtitle_url>": subtitle_body}`` —— 字幕文件本体的下载内容
        self.subtitle_bodies = subtitle_bodies or {}
        self.dynamic_page_size = dynamic_page_size
        self.opus_page_size = opus_page_size
        #: ``{"dynamic_detail:<id>": -352}`` → 第一次调用返回指定错误码
        self.failures = dict(failures or {})
        self.calls: dict[str, int] = {}
        self.downloads: list[str] = []
        self.client = _FakeClient()
        self.author_name = AUTHOR_NAME
        self.author_mid = AUTHOR_MID

    # -- 计数 / 失败注入 -- #
    def _tick(self, key: str) -> ApiResult | None:
        self.calls[key] = self.calls.get(key, 0) + 1
        code = self.failures.pop(key, None)
        if code is not None:
            if code == -352:
                return fail(-352, "风控校验失败")
            return fail(code, "注入失败")
        return None

    # -- 账户 -- #
    def nav(self) -> ApiResult:
        return ok({"isLogin": True, "mid": 2000, "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
        }})

    def login_state(self):
        return True, 2000, "已登录"

    def card(self, mid: int) -> ApiResult:
        return ok({"card": {"mid": mid, "name": AUTHOR_NAME,
                            "face": "https://i0.hdslb.com/bfs/face/test.jpg",
                            "fans": 123, "sign": "签名"}})

    def author_info(self, mid: int):
        from bili_sub_archive.bili.api import AuthorInfo

        return AuthorInfo(mid=mid, name=AUTHOR_NAME,
                          face="https://i0.hdslb.com/bfs/face/test.jpg", fans=123, sign="签名"), ok({})

    # -- 视频 -- #
    def video_list(self, mid: int, pn: int = 1, ps: int = 30, *, order: str = "pubdate",
                   special_type: str = "") -> ApiResult:
        if special_type:
            return ok(video_list_page([], 0))
        start = (pn - 1) * ps
        chunk = self.videos[start:start + ps]
        return ok(video_list_page(chunk, self.video_count, pn=pn, ps=ps))

    def video_detail(self, bvid: str) -> ApiResult:
        injected = self._tick(f"video_detail:{bvid}")
        if injected:
            return injected
        data = self.video_details.get(bvid)
        if data is None:
            return fail(62002, "稿件不可见")
        return ok(data)

    def page_list(self, bvid: str) -> ApiResult:
        pages = self.video_pages.get(bvid)
        if pages is None:
            data = self.video_details.get(bvid) or {}
            pages = data.get("pages") or []
        return ok({"pages": pages})

    def playurl(self, bvid: str, cid: int) -> ApiResult:
        return ok(self.playurls.get(bvid) or playurl_full())

    def access(self, bvid: str, cid: int):
        from bili_sub_archive.bili.parse import parse_playurl

        result = self.playurl(bvid, cid)
        if not result.ok:
            from bili_sub_archive.bili.parse import PlayAccess

            return PlayAccess(ok=False, error_kind=result.error_kind, message=result.message)
        return parse_playurl(result.data)

    def subtitle_list(self, bvid: str, cid: int) -> ApiResult:
        entries = self.subtitles.get(str(cid)) or self.subtitles.get(bvid) or []
        return ok(subtitle_list_page(entries))

    # -- 动态 -- #
    def dynamic_feed(self, mid: int, offset: str = "") -> ApiResult:
        start = int(offset) if offset.isdigit() else 0
        chunk = self.dynamics[start:start + self.dynamic_page_size]
        next_offset = start + self.dynamic_page_size
        has_more = next_offset < len(self.dynamics)
        return ok(dynamic_feed_page(chunk, offset=str(next_offset) if has_more else "", has_more=has_more))

    def dynamic_detail(self, dyn_id: str) -> ApiResult:
        injected = self._tick(f"dynamic_detail:{dyn_id}")
        if injected:
            return injected
        item = self.dynamic_details.get(dyn_id)
        if item is None:
            return fail(-404, "啥都木有")
        return ok({"item": item})

    # -- opus -- #
    def opus_feed(self, mid: int, page: int = 1, offset: str = "", *, type: str = "all") -> ApiResult:
        pool = self.opus_articles if type == "article" else self.opus_all
        start = (page - 1) * self.opus_page_size
        chunk = pool[start:start + self.opus_page_size]
        next_page = page + 1
        has_more = next_page * self.opus_page_size < len(pool)
        return ok(opus_feed_page(chunk, offset=f"cursor{next_page}" if has_more else "",
                                 has_more=has_more))

    def opus_detail(self, opus_id: str) -> ApiResult:
        injected = self._tick(f"opus_detail:{opus_id}")
        if injected:
            return injected
        item = self.opus_details.get(opus_id)
        if item is None:
            return fail(-404, "啥都木有")
        return ok({"item": item})

    # -- 旧格式专栏 -- #
    def article_list(self, mid: int, pn: int = 1, ps: int = 30) -> ApiResult:
        start = (pn - 1) * ps
        chunk = self.legacy_articles[start:start + ps]
        return ok({"articles": chunk, "pn": pn, "ps": ps, "count": len(self.legacy_articles)})

    def article_view(self, cvid: str) -> ApiResult:
        key = f"cv{str(cvid).lower().removeprefix('cv')}"
        injected = self._tick(f"article_view:{key}")
        if injected:
            return injected
        data = self.legacy_views.get(key)
        if data is None:
            return fail(-404, "啥都木有")
        return ok(data)

    # -- 下载 -- #
    def download(self, url: str, dest: Path, *, referer: str = "") -> DownloadResult:
        from bili_sub_archive.paths import atomic_write_bytes

        self.calls["download"] = self.calls.get("download", 0) + 1
        self.downloads.append(url)
        if "broken" in str(url):
            return DownloadResult(ok=False, error_kind="network_error",
                                  message="注入的下载失败", attempts=1, url=str(url))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if str(url) in self.subtitle_bodies:
            blob = json.dumps(self.subtitle_bodies[str(url)], ensure_ascii=False).encode("utf-8")
            atomic_write_bytes(Path(dest), blob)
            return DownloadResult(ok=True, path=Path(dest), bytes_written=len(blob),
                                  content_type="application/json", attempts=1, url=str(url))
        atomic_write_bytes(Path(dest), PNG_1PX)
        return DownloadResult(ok=True, path=Path(dest), bytes_written=len(PNG_1PX),
                              content_type="image/png", attempts=1, url=str(url))


class _FakeClient:
    """模拟 :class:`bili_sub_archive.bili.client.HttpClient` 的统计字段。"""

    def __init__(self) -> None:
        self.requests = 0
        self.retries_used = 0
        self.bytes_downloaded = 0
        self.risk_events = 0
        self.consecutive_risk = 0
        self.cookie = ""


# --------------------------------------------------------------------------- #
# 阶段 2 替身：媒体下载器 / 本地转写器
# --------------------------------------------------------------------------- #
class FakeDownloader:
    """按 :class:`bili_sub_archive.media.Downloader` 协议写出最小合法 MP4，绝不联网。

    - ``fail_pages``：这些分 P 返回失败（验证 partial / 失败补做）；
    - ``broken_pages``：写出结构非法的产物（验证"产物校验失败"分支）；
    - 续传：调用方预先放一个 ``videos/P01.mp4.part`` 残片即可（模拟上次中断）。
    """

    name = "fake-downloader"

    def __init__(self, *, fail_pages: tuple[int, ...] = (), broken_pages: tuple[int, ...] = (),
                 available: bool = True,
                 error_kind: str = "network_error", delay: float = 0.02) -> None:
        self.fail_pages = set(fail_pages)
        self.broken_pages = set(broken_pages)
        self.available = available
        self.error_kind = error_kind
        self.delay = float(delay)
        self.calls: list[int] = []
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def probe(self) -> tuple[bool, str]:
        return (True, "替身下载器可用") if self.available else (False, "替身下载器被标记为不可用")

    def download(self, request) -> MediaFile:
        with self._lock:
            self.calls.append(request.page)
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            return self._do_download(request)
        finally:
            with self._lock:
                self._active -= 1

    def _do_download(self, request) -> MediaFile:
        if self.delay:
            time.sleep(self.delay)
        record = MediaFile(page=request.page, cid=request.cid, part=request.part,
                           url=request.page_url)
        if not self.available:
            record.status = "failed"
            record.error_kind = "dependency_missing"
            record.message = "替身下载器被标记为不可用"
            return record
        if request.page in self.fail_pages:
            record.status = "failed"
            record.error_kind = self.error_kind
            record.message = "注入的下载失败"
            return record

        videos = request.videos_dir
        videos.mkdir(parents=True, exist_ok=True)
        # 与 yt-dlp 一致：续传标记必须在下载前判断（完成后 .part 会被删掉）
        record.resumed = any(videos.glob(f"{request.stem}*.part"))
        target = videos / f"{request.stem}.mp4"
        blob = NOT_MEDIA if request.page in self.broken_pages else MP4_MIN
        atomic_write_bytes(target, blob)
        record.name = f"videos/{target.name}"
        record.size = len(blob)
        record.duration = 600.0
        record.width = 1920
        record.height = 1080
        record.status = "done"
        record.message = "替身下载完成" + ("（续传）" if record.resumed else "")
        return record


class FakeTranscriber:
    """按 :class:`bili_sub_archive.transcript.asr.Transcriber` 协议产出固定片段，不跑模型。"""

    name = "fake-whisper"

    def __init__(self, *, text: str = "替身转写文本", available: bool = True,
                 fail: bool = False, empty: bool = False) -> None:
        self.text = text
        self.available = available
        self.fail = fail
        self.empty = empty
        self.calls: list[int] = []

    def probe(self) -> tuple[bool, str]:
        return (True, "替身转写器可用") if self.available else (
            False, "未安装 faster-whisper：pip install \"bili-sub-archive[asr]\"")

    def transcribe(self, audio, *, page: int, logger=None):
        from bili_sub_archive.transcript.asr import AsrOutcome
        from bili_sub_archive.transcript.srt import Segment

        self.calls.append(int(page))
        if not self.available:
            return AsrOutcome(error_kind="dependency_missing", message="替身转写器不可用")
        if self.fail:
            return AsrOutcome(error_kind="http_error", message="注入的转写失败")
        if self.empty:
            return AsrOutcome(error_kind="no_speech", message="转写未产出任何文字片段")
        return AsrOutcome(
            ok=True, model=self.name, language="zh", duration=12.0,
            segments=[
                Segment(start=0.0, end=2.5, text=f"{self.text}（P{int(page)} 第 1 段）",
                        page=int(page), source="asr"),
                Segment(start=2.5, end=6.0, text=f"{self.text}（P{int(page)} 第 2 段）",
                        page=int(page), source="asr"),
            ],
        )


def fake_ffmpeg_runner(created: list | None = None, *, duration: float = 600.0,
                       width: int = 1920, height: int = 1080, has_audio: bool = True):
    """假的媒体命令执行器：同时满足 ffmpeg 与 ffprobe 两条调用路径。

    - ffmpeg（提音频）：把 ``argv[-1]`` 指向的目标 WAV 写出来并返回成功；
    - ffprobe（校验）：在 stdout 返回一份合法的 ``-print_format json`` 结果。

    两者用同一个 runner 是因为 ``extract_audio`` 只看退出码与目标文件，
    ``_probe_with_ffprobe`` 只看 stdout —— 互不干扰，省掉一层注入。
    """

    def runner(argv: list[str]) -> tuple[int, str, str]:
        target = Path(argv[-1])
        if target.suffix.lower() in (".json", "") or "-print_format" in argv:
            streams = [{"codec_type": "video", "codec_name": "h264",
                        "width": width, "height": height}]
            if has_audio:
                streams.append({"codec_type": "audio", "codec_name": "aac"})
            payload = {"streams": streams,
                       "format": {"format_name": "mov,mp4,m4a", "duration": str(duration)}}
            return 0, json.dumps(payload), ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF" + b"\x00" * 4096)
        if created is not None:
            created.append(list(argv))
        return 0, "", ""

    return runner


# --------------------------------------------------------------------------- #
# 阶段 3 替身：Chat 客户端 / mmdc 渲染器
# --------------------------------------------------------------------------- #
#: 分块要点 prompt 的识别标记（与 summarize.prompt.DEFAULT_MAP 保持一致）
MAP_MARKER = "请只针对**这一部分**"


def fake_png(width: int = 1600, height: int = 900, *, padding: int = 256) -> bytes:
    """合成"结构合法"的 PNG 头（签名 + IHDR），供渲染替身写盘与尺寸校验使用。"""
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
            + b"\x00" * 4 + b"\x00" * padding)


class FakeChatClient:
    """按 :class:`bili_sub_archive.summarize.llm.ChatClient` 协议产出确定性内容，绝不联网。

    - ``map`` 阶段（识别 :data:`MAP_MARKER`）：回 2 条要点，**并把该块末尾原文带进要点**，
      这样测试可以断言"最后一块的尾部确实进了合并输入"（不丢尾段的端到端证据）；
    - ``full``/``reduce`` 阶段：回一份带 ``## 摘要`` 与 ``## 大纲`` 的受控文档；
    - ``fail`` / ``fail_on_call`` / ``empty`` / ``truncate``：注入各类失败与截断；
    - ``calls``：逐次记录 messages，供断言"调了几次、发了什么"。
    """

    name = "fake-chat"

    def __init__(self, *, model: str = "fake-model-1", endpoint_host: str = "fake.local",
                 summary_text: str = "替身模型生成的摘要正文。", fail: bool = False,
                 fail_on_call: int | None = None, error_kind: str = "llm_http_error",
                 http_status: int = 500, empty: bool = False, truncate: bool = False,
                 outline: bool = True) -> None:
        self.model = model
        self.endpoint_host = endpoint_host
        self.summary_text = summary_text
        self.fail = fail
        self.fail_on_call = fail_on_call
        self.error_kind = error_kind
        self.http_status = http_status
        self.empty = empty
        self.truncate = truncate
        self.outline = outline
        self.calls: list[list[dict]] = []

    # -- 协议 -- #
    def complete(self, messages):
        from bili_sub_archive.summarize.llm import ChatResult

        self.calls.append(list(messages))
        index = len(self.calls)
        if self.fail or (self.fail_on_call and index == self.fail_on_call):
            return ChatResult(ok=False, model=self.model, error_kind=self.error_kind,
                              http_status=self.http_status,
                              message="注入的模型调用失败", attempts=1)
        if self.empty:
            return ChatResult(ok=False, model=self.model, error_kind="llm_response_invalid",
                              http_status=200, message="模型返回内容为空", attempts=1)
        user = str(messages[-1].get("content") or "") if messages else ""
        text = self._respond(user)
        return ChatResult(ok=True, text=text, model=self.model, attempts=1,
                          prompt_tokens=max(1, len(user) // 2),
                          completion_tokens=max(1, len(text) // 2),
                          finish_reason="length" if self.truncate else "stop")

    # -- 内容 -- #
    def _respond(self, user: str) -> str:
        if MAP_MARKER in user:
            tail = " ".join(self._transcript_of(user).split())[-80:]
            return f"- 本段关键信息（替身）\n- 本段结尾原文：{tail}"
        body = f"## 摘要\n\n{self.summary_text}\n\n"
        if not self.outline:
            return f"## 摘要\n\n{self.summary_text}\n"
        return body + ("## 大纲\n"
                       "- 主题：替身视频主题\n"
                       "  - 第一部分要点\n"
                       "    - 细节一\n"
                       "  - 第二部分要点\n")

    @staticmethod
    def _transcript_of(user: str) -> str:
        """取 prompt 里 ``----- 文字稿开始/结束 -----`` 之间的正文（没有则整段）。"""
        begin, end = "----- 文字稿开始 -----", "----- 文字稿结束 -----"
        if begin in user and end in user:
            return user.split(begin, 1)[1].split(end, 1)[0]
        return user

    def prompts(self) -> list[str]:
        """每次调用最后一条 user 消息的文本（断言用）。"""
        return [str(call[-1].get("content") or "") for call in self.calls]


def fake_mmdc_runner(created: list | None = None, *, ok: bool = True, width: int = 1600,
                     height: int = 900, stderr: str = "", padding: int = 256,
                     write_png: bool = True):
    """假的 mermaid-cli 执行器：``(argv) -> (returncode, stdout, stderr)``。

    ``write_png=False`` 用于验证"mmdc 报成功但没产物"；``ok=False`` 用于验证退出码失败。
    """

    def runner(argv: list[str]) -> tuple[int, str, str]:
        if created is not None:
            created.append(list(argv))
        if not ok:
            return 1, "", stderr or "mmdc: 注入的渲染失败"
        if write_png:
            target = Path(argv[argv.index("-o") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, fake_png(width, height, padding=padding))
        return 0, "generated", ""

    return runner
