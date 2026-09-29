"""阶段 0：B 站平台可行性验证探针。

核心方法是**身份矩阵**：同一接口分别用三种身份请求，从而量化"登录 Cookie 到底
扩大了多少可发现 / 可读取范围"：

    anon       完全不带 Cookie
    anon_buvid 只带 buvid3/buvid4（真实匿名 Web 会话的最小指纹）
    auth       完整登录 Cookie

用法（在 E:\\SubVideo 下）::

    python tools/stage0/probe.py                 # 全量
    python tools/stage0/probe.py --mode public   # 强制匿名，只测公开范围
    python tools/stage0/probe.py --uid 123456
    python tools/stage0/probe.py --only video_list_public,playurl

产物：
    stage0/evidence/raw/<probe_id>.json   逐探针脱敏原始响应（裁剪）
    stage0/evidence/summary.json          结论化记录（含身份矩阵与对照）
    stage0/evidence/RESULTS.md            自动生成的验证结果表

凭据只从环境变量 / config.local.toml / require.txt 读取，**绝不写入任何产物**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import BiliClient, describe_code  # noqa: E402
from credstore import Credentials, Redactor, load_credentials  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "stage0" / "evidence"
RAW = EVIDENCE / "raw"

IDENT_LABEL = {"anon": "匿名", "anon_buvid": "匿名+buvid", "auth": "授权"}
IDENT_ORDER = ("auth", "anon_buvid", "anon")

# --------------------------------------------------------------------------- #
# 证据裁剪：保留结构、字段名与样本，控制体积
# --------------------------------------------------------------------------- #
_MAX_STR = 240
_MAX_LIST = 2
_MAX_DEPTH = 6


def trim(obj: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "..."
    if isinstance(obj, str):
        return obj if len(obj) <= _MAX_STR else obj[:_MAX_STR] + f"...(+{len(obj) - _MAX_STR})"
    if isinstance(obj, dict):
        return {k: trim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        kept = [trim(v, depth + 1) for v in obj[:_MAX_LIST]]
        if len(obj) > _MAX_LIST:
            kept.append(f"...(共 {len(obj)} 项)")
        return kept
    return obj


def keys_of(items: list) -> list[str]:
    if not items or not isinstance(items[0], dict):
        return []
    return sorted(items[0].keys())


def _ints(items: list, field: str) -> list[int]:
    vals = []
    for it in items:
        if isinstance(it, dict) and it.get(field) is not None:
            try:
                vals.append(int(it[field]))
            except (TypeError, ValueError):
                pass
    return vals


def check_order(items: list, field: str) -> list[str]:
    """检查排序，并识别"置顶项"（首条比第二条更早 = 置顶）。"""
    vals = _ints(items, field)
    if len(vals) < 2:
        return [f"可比较样本不足（{len(vals)} 条），无法判定 {field} 排序"]
    tail_ok = all(vals[i] >= vals[i + 1] for i in range(1, len(vals) - 1))
    head_ok = vals[0] >= vals[1]
    out = []
    if head_ok and tail_ok:
        out.append(f"整体按 {field} 严格降序（含首条）")
    elif tail_ok and not head_ok:
        out.append(f"**首条为置顶项**：{field}={vals[0]} 早于第二条 {vals[1]}；"
                   f"第 2 条起按 {field} 降序")
    else:
        out.append(f"**不满足**按 {field} 降序，实测前 8 项 = {vals[:8]}")
    out.append(f"{field} 范围 = [{min(vals)}, {max(vals)}]")
    return out


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def _extract_items(data: Any) -> list | None:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("vlist", "items", "archives", "list", "articles", "pics", "medias"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("vlist"), list):
            return v["vlist"]
    return None


def items_of(resp: dict) -> list:
    return _extract_items(resp.get("data")) or []


def opus_text(module_content: dict) -> tuple[str, int]:
    """从 opus 的 MODULE_TYPE_CONTENT 里取正文与图片数（兼容两种结构）。"""
    if not module_content:
        return "", 0
    if module_content.get("paragraphs"):
        chunks, pics = [], 0
        for p in module_content["paragraphs"]:
            if p.get("para_type") == 2:
                pics += 1
            txt = p.get("text") or {}
            for node in txt.get("nodes") or []:
                chunks.append(((node.get("word") or {}).get("words")) or "")
        return "".join(chunks), pics
    return (module_content.get("content") or "",
            len(module_content.get("pics") or []))


def dynamic_text(item: dict) -> tuple[str, int, int]:
    """汇总动态正文长度、图片数、发布时间。兼容两种 modules 形状：

    - ``/x/polymer/web-dynamic/v1/detail``      → modules 是 dict（module_author/module_dynamic/...）
    - ``/x/polymer/web-dynamic/v1/opus/detail`` → modules 是 list（module_type/module_content）
    """
    text_len = pics = pub_ts = 0
    mods = item.get("modules")
    if isinstance(mods, list):
        for m in mods:
            mt = m.get("module_type")
            if mt == "MODULE_TYPE_CONTENT":
                t, p = opus_text(m.get("module_content") or {})
                text_len += len(t)
                pics += p
            elif mt == "MODULE_TYPE_AUTHOR":
                pub_ts = int((m.get("module_author") or {}).get("pub_ts") or 0)
    elif isinstance(mods, dict):
        author = mods.get("module_author") or {}
        pub_ts = int(author.get("pub_ts") or 0)
        md = mods.get("module_dynamic") or {}
        text_len += len(rich_text(md.get("desc")))
        _mt, m_text, m_pics, _extra = major_shape(md.get("major") or {})
        text_len += m_text
        pics += m_pics
    return text_len, pics, pub_ts


def rich_text(obj: Any) -> str:
    """B 站富文本对象 → 纯文本。

    ``major.opus.summary`` 之类字段不是字符串，而是
    ``{"rich_text_nodes": [{"orig_text": ..., "text": ...}, ...]}``；
    按字符串直接取长度会得到字典键数（错的）。
    """
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    nodes = obj.get("rich_text_nodes")
    if isinstance(nodes, list):
        return "".join(n.get("orig_text") or n.get("text") or "" for n in nodes)
    return obj.get("text") or ""


def major_shape(major: dict) -> tuple[str, int, int, dict]:
    """解析 module_dynamic.major：返回 (major_type, 文本长度, 图片数, 关键字段)。"""
    mt = major.get("type") or ""
    extra: dict[str, Any] = {}
    text_len = pics = 0
    if mt == "MAJOR_TYPE_OPUS":
        op = major.get("opus") or {}
        summary = op.get("summary")
        text_len = len(rich_text(summary)) + len(op.get("title") or "")
        pics = len(op.get("pics") or [])
        extra = {"title": op.get("title"), "jump_url": op.get("jump_url"),
                 "summary_是富文本对象": isinstance(summary, dict),
                 "summary_nodes": len((summary or {}).get("rich_text_nodes") or [])
                 if isinstance(summary, dict) else 0,
                 "pic_keys": sorted((op.get("pics") or [{}])[0].keys()) if op.get("pics") else []}
    elif mt == "MAJOR_TYPE_DRAW":
        dr = major.get("draw") or {}
        pics = len(dr.get("items") or [])
        extra = {"pic_keys": sorted((dr.get("items") or [{}])[0].keys()) if dr.get("items") else []}
    elif mt == "MAJOR_TYPE_ARCHIVE":
        ar = major.get("archive") or {}
        text_len = len(ar.get("title") or "") + len(rich_text(ar.get("desc")))
        extra = {"bvid": ar.get("bvid"), "title": ar.get("title"),
                 "cover": bool(ar.get("cover")), "duration_text": ar.get("duration_text")}
    elif mt == "MAJOR_TYPE_ARTICLE":
        art = major.get("article") or {}
        text_len = len(art.get("title") or "") + len(rich_text(art.get("desc")))
        pics = len(art.get("covers") or [])
        extra = {"id": art.get("id"), "title": art.get("title"),
                 "jump_url": art.get("jump_url")}
    elif mt == "MAJOR_TYPE_COMMON":
        cm = major.get("common") or {}
        text_len = len(cm.get("title") or "") + len(rich_text(cm.get("desc")))
        extra = {"title": cm.get("title"), "jump_url": cm.get("jump_url")}
    elif mt == "MAJOR_TYPE_LIVE_RCMD":
        extra = {"说明": "直播推荐卡（本项目不纳入）"}
    return mt, text_len, pics, extra


# --------------------------------------------------------------------------- #
# 探针上下文
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, creds: Credentials, redactor: Redactor, mode: str):
        self.creds = creds
        self.red = redactor
        self.mode = mode
        self.uid = creds.uid
        self.clients: dict[str, BiliClient] = {
            "anon": BiliClient(cookie="", interval=0.7, retries=2),
            "anon_buvid": BiliClient(cookie="", interval=0.7, retries=2),
            "auth": BiliClient(cookie=creds.cookie, interval=0.9, retries=3),
        }
        self.records: list[dict] = []
        self.store: dict[str, Any] = {}

    def available(self, identity: str) -> bool:
        return identity != "auth" or self.creds.has_cookie

    def call(
        self,
        probe_id: str,
        group: str,
        title: str,
        path: str,
        params: dict | None = None,
        *,
        wbi: bool = False,
        identity: str = "auth",
        referer: str = "https://www.bilibili.com/",
        note: str = "",
        analyse: Callable[[dict], list[str]] | None = None,
    ) -> dict:
        if not self.available(identity):
            rec = {"probe_id": probe_id, "group": group, "title": title,
                   "identity": identity, "skipped": True,
                   "reason": "无 Cookie，跳过授权探针"}
            self.records.append(rec)
            print(f"  [skip] {probe_id}（无 Cookie）")
            return rec

        client = self.clients[identity]
        print(f"  [run ] {probe_id} [{IDENT_LABEL[identity]}] {path}")
        resp = client.get_json(path, params, wbi=wbi, referer=referer)
        items = _extract_items(resp.get("data"))
        # 全量响应只留在内存中供后续探针链式使用；落盘的是裁剪 + 脱敏版本
        self.store[f"{probe_id}:resp"] = resp
        if items is not None:
            self.store[f"{probe_id}:items"] = items

        obs = [f"HTTP {resp.get('http_status')} / code={resp.get('code')}"
               f"（{describe_code(resp.get('code'))}）"]
        if items is not None:
            obs.append(f"本页条目数 = {len(items)}")
        if analyse:
            try:
                obs.extend(analyse(resp) or [])
            except Exception as exc:
                obs.append(f"[分析异常] {type(exc).__name__}: {exc}")

        rec = {
            "probe_id": probe_id, "group": group, "title": title,
            "identity": identity, "wbi": wbi, "path": path,
            "params": self.red.redact_obj(params or {}), "note": note,
            "http_status": resp.get("http_status"), "code": resp.get("code"),
            "message": self.red.redact(str(resp.get("message", ""))),
            "code_meaning": describe_code(resp.get("code")),
            "elapsed_ms": resp.get("elapsed_ms"),
            "item_count": len(items) if items is not None else None,
            "item_keys": keys_of(items) if items else [],
            "observations": obs,
        }
        self.records.append(rec)
        (RAW / f"{probe_id}.json").write_text(
            json.dumps(self.red.redact_obj(trim(resp)), ensure_ascii=False, indent=2),
            encoding="utf-8")
        return rec

    def matrix(self, base_id: str, group: str, title: str, path: str,
               params: dict | None = None, *, wbi: bool = False,
               identities: tuple[str, ...] = IDENT_ORDER,
               referer: str = "https://www.bilibili.com/", note: str = "",
               analyse: Callable[[dict], list[str]] | None = None) -> None:
        for ident in identities:
            self.call(f"{base_id}__{ident}", group, f"{title}（{IDENT_LABEL[ident]}）",
                      path, params, wbi=wbi, identity=ident, referer=referer,
                      note=note, analyse=analyse)

    # -- 链式取用（内存中的全量数据） -------------------------------------- #
    def resp(self, probe_id: str) -> dict:
        return self.store.get(f"{probe_id}:resp") or {}

    def items(self, probe_id: str) -> list:
        return self.store.get(f"{probe_id}:items") or []

    def remember(self, key: str, value: Any) -> None:
        self.store[key] = value

    def recall(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)


# --------------------------------------------------------------------------- #
# 探针
# --------------------------------------------------------------------------- #
def run_probes(ctx: Ctx, only: set[str] | None) -> None:
    def wanted(pid: str) -> bool:
        # --only 按前缀匹配，便于只跑某一组（如 --only video_list_public,playurl）
        return not only or any(pid.startswith(o) for o in only)

    uid = ctx.uid
    space_video_ref = f"https://space.bilibili.com/{uid}/video"

    # ================= A. 登录态与匿名指纹 ================= #
    print("\n[A] 登录态与匿名指纹")
    if wanted("nav"):
        ctx.matrix(
            "nav", "A 登录态", "登录态检查 / wbi 密钥来源", "/x/web-interface/nav",
            identities=("auth", "anon"),
            note="启动时登录态检查的实现依据；未登录仍下发 wbi 密钥",
            analyse=lambda r: [
                f"isLogin = {(r.get('data') or {}).get('isLogin')}",
                f"uname = {(r.get('data') or {}).get('uname')}",
                f"mid = {(r.get('data') or {}).get('mid')}",
                f"wbi_img 是否下发 = {bool((r.get('data') or {}).get('wbi_img'))}",
            ])
    if wanted("buvid_spi"):
        rec = ctx.call("buvid_spi", "A 登录态", "匿名指纹获取（buvid3/buvid4）",
                       "/x/frontend/finger/spi", identity="anon",
                       note="匿名会话的最小指纹；拿到后用于构造 anon_buvid 身份对照")
        d = ctx.resp("buvid_spi").get("data") or {}
        if d.get("b_3"):
            ctx.clients["anon_buvid"].cookie = f"buvid3={d['b_3']}; buvid4={d.get('b_4', '')}"
            rec["observations"].append("已用于构造 anon_buvid 客户端")
    if wanted("nav_invalid_cookie"):
        bad = BiliClient(cookie="SESSDATA=deadbeef; bili_jct=deadbeef", interval=0.7, retries=1)
        resp = bad.get_json("/x/web-interface/nav", referer="https://www.bilibili.com/")
        ctx.records.append({
            "probe_id": "nav_invalid_cookie", "group": "A 登录态",
            "title": "伪造/失效 Cookie 访问 nav", "identity": "auth",
            "path": "/x/web-interface/nav", "params": {}, "wbi": False,
            "note": "验证「Cookie 失效」可与「未发现」「无权限」区分（对照 nav__auth）",
            "http_status": resp.get("http_status"), "code": resp.get("code"),
            "message": ctx.red.redact(str(resp.get("message", ""))),
            "code_meaning": describe_code(resp.get("code")),
            "elapsed_ms": resp.get("elapsed_ms"), "item_count": None, "item_keys": [],
            "observations": [
                f"HTTP {resp.get('http_status')} / code={resp.get('code')}"
                f"（{describe_code(resp.get('code'))}）",
                f"isLogin = {(resp.get('data') or {}).get('isLogin')}"],
        })
        (RAW / "nav_invalid_cookie.json").write_text(
            json.dumps(ctx.red.redact_obj(trim(resp)), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("  [run ] nav_invalid_cookie [伪造 Cookie] /x/web-interface/nav")

    # ================= B. 作者信息 ================= #
    print("\n[B] 作者信息")
    if wanted("author_card"):
        ctx.call("author_card", "B 作者", "UP 主名片（UID→名称/头像/粉丝数）",
                 "/x/web-interface/card", {"mid": uid}, identity="anon",
                 referer=f"https://space.bilibili.com/{uid}",
                 note="用于输出目录命名 <UID>_<UP名称>，且不需登录",
                 analyse=lambda r: [
                     f"name = {((r.get('data') or {}).get('card') or {}).get('name')}",
                     f"fans = {((r.get('data') or {}).get('card') or {}).get('fans')}",
                     f"face 可用 = {bool(((r.get('data') or {}).get('card') or {}).get('face'))}"])
    if wanted("author_acc_info"):
        ctx.matrix("author_acc_info", "B 作者", "UP 主详细信息（wbi 版）",
                   "/x/space/wbi/acc/info", {"mid": uid}, wbi=True,
                   identities=("auth", "anon_buvid"),
                   referer=f"https://space.bilibili.com/{uid}",
                   note="记录风控表现，决定适配层是否使用该接口")

    # ================= C. 视频列表 ================= #
    print("\n[C] 视频列表")
    if wanted("video_list_public"):
        for pn in (1, 2):
            def a_list(r, pn=pn):
                d = r.get("data") or {}
                page = d.get("page") or {}
                vl = items_of(r)
                out = [f"page.count = {page.get('count')}（公开投稿总数）",
                       f"page.pn = {page.get('pn')} / ps = {page.get('ps')}"]
                if vl:
                    out += check_order(vl, "created")
                    out.append("首条 created = %s（%s）"
                               % (vl[0].get("created"), _fmt_ts(vl[0].get("created"))))
                    out.append("字段：" + ", ".join(sorted(vl[0].keys())))
                    out.append(f"其中 is_charging_arc=True 的条目数 = "
                               f"{len([v for v in vl if v.get('is_charging_arc')])}")
                return out
            ctx.matrix(f"video_list_public_p{pn}", "C 视频",
                       f"公开投稿列表 第 {pn} 页（order=pubdate, WBI）",
                       "/x/space/wbi/arc/search",
                       {"mid": uid, "ps": 30, "pn": pn, "order": "pubdate",
                        "tid": 0, "platform": "web", "web_location": 1550101},
                       wbi=True, referer=space_video_ref,
                       note="order=pubdate 按发布时间倒序；置顶项由首条判定",
                       analyse=a_list)
        ctx.remember("public_videos", ctx.items("video_list_public_p1__auth"))
        ctx.remember("public_total",
                     ((ctx.resp("video_list_public_p1__auth").get("data") or {}).get("page") or {}).get("count"))
        ctx.remember("public_anon_count", len(ctx.items("video_list_public_p1__anon_buvid")))

    if wanted("video_list_charging"):
        def a_charge(r):
            d = r.get("data") or {}
            page = d.get("page") or {}
            vl = items_of(r)
            out = [f"page.count = {page.get('count')}（充电专属视频总数）"]
            if vl:
                out += check_order(vl, "created")
                out.append("字段：" + ", ".join(sorted(vl[0].keys())))
                out.append("首条 is_charging_arc = %s / elec_arc_badge = %s"
                           % (vl[0].get("is_charging_arc"), vl[0].get("elec_arc_badge")))
            return out
        ctx.matrix("video_list_charging", "C 视频",
                   "充电专属视频列表（special_type=charging）",
                   "/x/space/wbi/arc/search",
                   {"mid": uid, "pn": 1, "ps": 30, "special_type": "charging",
                    "order": "pubdate", "order_avoided": "true", "platform": "web"},
                   wbi=True, referer=space_video_ref,
                   note="关键结论：充电专属条目能否从列表被发现（而非只能靠已知链接）",
                   analyse=a_charge)
        ctx.remember("charging_videos", ctx.items("video_list_charging__auth"))

    # ================= C2. 视频详情 / 分P / 字幕 / 播放地址 ================= #
    print("\n[C2] 视频详情与可下载性")
    pub = ctx.recall("public_videos") or []
    charge = ctx.recall("charging_videos") or []
    # 选一个"非充电"的公开视频做对照，避免把充电门控误判为登录门控
    plain = [v for v in pub if not v.get("is_charging_arc")]
    probe_bvid = (plain[0].get("bvid") if plain else "") or (pub[0].get("bvid") if pub else "") or ""
    charge_bvid = (charge[0].get("bvid") if charge else "") or ""
    ctx.remember("probe_bvid", probe_bvid)
    ctx.remember("charge_bvid", charge_bvid)
    print(f"  [info] 对照公开视频 = {probe_bvid} / 充电样本 = {charge_bvid}")

    if probe_bvid and wanted("video_detail"):
        ctx.call("video_detail", "C2 视频详情", "视频详情（含分P 列表）",
                 "/x/web-interface/view", {"bvid": probe_bvid}, identity="auth",
                 referer=f"https://www.bilibili.com/video/{probe_bvid}",
                 note="免登录；pages[] 是分 P 归档的依据",
                 analyse=lambda r: [
                     f"title = {(r.get('data') or {}).get('title')}",
                     f"分P 数 = {len((r.get('data') or {}).get('pages') or [])}",
                     f"duration = {(r.get('data') or {}).get('duration')}s",
                     f"is_upower_exclusive = {(r.get('data') or {}).get('is_upower_exclusive')}",
                     f"字段：{', '.join(sorted((r.get('data') or {}).keys()))}"])
    if probe_bvid and wanted("video_pagelist"):
        ctx.call("video_pagelist", "C2 视频详情", "分P 列表（独立接口）",
                 "/x/player/pagelist", {"bvid": probe_bvid}, identity="auth",
                 referer=f"https://www.bilibili.com/video/{probe_bvid}",
                 note="对照 view.pages，确认 cid 来源")

    pages = ((ctx.resp("video_detail").get("data") or {}).get("pages") or [])
    cid = str(pages[0].get("cid")) if pages else ""

    if cid and wanted("playurl"):
        def a_play(r):
            d = r.get("data") or {}
            dash = d.get("dash") or {}
            return [f"accept_quality = {d.get('accept_quality')}",
                    f"accept_description = {d.get('accept_description')}",
                    f"dash 视频流 = {len(dash.get('video') or [])} / "
                    f"音频流 = {len(dash.get('audio') or [])}",
                    f"durl 分段 = {len(d.get('durl') or [])}",
                    f"直出可下载 URL = {bool(dash or d.get('durl'))}"]
        # 关键：/x/player/wbi/playurl 在本环境恒为 HTTP 412，非 wbi 的才可用
        ctx.call("playurl_wbi_variant", "C2 视频详情",
                 "播放地址 —— wbi 变体（实测不可用）",
                 "/x/player/wbi/playurl",
                 {"bvid": probe_bvid, "cid": cid, "qn": 80, "fnval": 4048, "fnver": 0,
                  "fourk": 1, "platform": "pc", "high_quality": 1},
                 wbi=True, identity="auth",
                 referer=f"https://www.bilibili.com/video/{probe_bvid}",
                 note="记录该变体被风控拦截，适配层必须改用非 wbi 变体", analyse=a_play)
        ctx.matrix("playurl", "C2 视频详情",
                   "播放地址 —— 非 wbi 变体 platform=pc",
                   "/x/player/playurl",
                   {"bvid": probe_bvid, "cid": cid, "qn": 80, "fnval": 4048, "fnver": 0,
                    "fourk": 1, "platform": "pc", "high_quality": 1},
                   identities=("auth", "anon_buvid"),
                   referer=f"https://www.bilibili.com/video/{probe_bvid}",
                   note="决定 yt-dlp 之外是否还需要自研下载；阶段 2 决策", analyse=a_play)

    if charge_bvid:
        if wanted("charging_video_detail"):
            ctx.matrix("charging_video_detail", "C2 视频详情", "充电专属视频详情",
                       "/x/web-interface/view", {"bvid": charge_bvid},
                       identities=("auth", "anon_buvid"),
                       referer=f"https://www.bilibili.com/video/{charge_bvid}",
                       note="关键对照：无权限时能否得到可区分的错误码，而非伪装成功",
                       analyse=lambda r: [
                           f"title = {(r.get('data') or {}).get('title')}",
                           f"is_upower_exclusive = {(r.get('data') or {}).get('is_upower_exclusive')}"])
        # 必须在 matrix 之后再取 pages（cid 来自授权那一次响应）
        pages_c = ((ctx.resp("charging_video_detail__auth").get("data") or {}).get("pages") or [])
        ccid = str(pages_c[0].get("cid")) if pages_c else ""
        if ccid and wanted("charging_playurl"):
            ctx.matrix("charging_playurl", "C2 视频详情",
                       "充电专属视频播放地址（非 wbi, platform=pc）",
                       "/x/player/playurl",
                       {"bvid": charge_bvid, "cid": ccid, "qn": 80, "fnval": 4048,
                        "fnver": 0, "fourk": 1, "platform": "pc", "high_quality": 1},
                       identities=("auth", "anon_buvid"),
                       referer=f"https://www.bilibili.com/video/{charge_bvid}",
                       note="**决定性证据**：授权拿到 dash 多路流 = 有充电权限；"
                            "匿名只有单段试看 = 无权限",
                       analyse=lambda r: [
                           f"dash 视频流 = {len(((r.get('data') or {}).get('dash') or {}).get('video') or [])}",
                           f"durl 分段 = {len((r.get('data') or {}).get('durl') or [])}"])

    # ---- 字幕门控（登录 vs 匿名；充电 vs 非充电 双重对照） ---- #
    if wanted("subtitle"):
        def a_sub(r):
            subs = ((r.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            return [f"字幕条目数 = {len(subs)}",
                    f"字幕语言 = {[s.get('lan') for s in subs]}",
                    f"subtitle_url 直出 = {bool(subs and subs[0].get('subtitle_url'))}"]
        if cid:
            ctx.matrix("subtitle_plain", "C2 字幕", "字幕列表 —— 非充电公开视频",
                       "/x/player/wbi/v2", {"bvid": probe_bvid, "cid": cid}, wbi=True,
                       identities=("auth", "anon_buvid"),
                       referer=f"https://www.bilibili.com/video/{probe_bvid}",
                       note="对照项：非充电视频的匿名取字幕结果", analyse=a_sub)
        pages_c = ((ctx.resp("charging_video_detail__auth").get("data") or {}).get("pages") or [])
        ccid = str(pages_c[0].get("cid")) if pages_c else ""
        if ccid:
            ctx.matrix("subtitle_charging", "C2 字幕", "字幕列表 —— 充电专属视频",
                       "/x/player/wbi/v2", {"bvid": charge_bvid, "cid": ccid}, wbi=True,
                       identities=("auth", "anon_buvid"),
                       referer=f"https://www.bilibili.com/video/{charge_bvid}",
                       note="对照项：充电视频的匿名取字幕结果", analyse=a_sub)
            # 字幕文件本体是否匿名可下载
            subs = ((ctx.resp("subtitle_charging__auth").get("data") or {})
                    .get("subtitle") or {}).get("subtitles") or []
            if subs and wanted("subtitle_file"):
                url = subs[0].get("subtitle_url") or ""
                if url.startswith("//"):
                    url = "https:" + url
                try:
                    body = ctx.clients["anon"].open(
                        url, referer=f"https://www.bilibili.com/video/{charge_bvid}").read()
                    j = json.loads(body.decode("utf-8"))
                    cues = j.get("body") or []
                    ctx.records.append({
                        "probe_id": "subtitle_file", "group": "C2 字幕",
                        "title": "字幕文件本体（CDN 直取，不带 Cookie）",
                        "identity": "anon", "path": url.split("?")[0], "params": {},
                        "wbi": False, "note": "验证拿到 subtitle_url 后能否直接下载",
                        "http_status": 200, "code": 0, "message": "OK",
                        "code_meaning": "成功", "elapsed_ms": None,
                        "item_count": len(cues), "item_keys": sorted((cues[0] or {}).keys()) if cues else [],
                        "observations": [f"字幕片段数 = {len(cues)}",
                                         f"首条 = {json.dumps(cues[0], ensure_ascii=False)[:160] if cues else '无'}"],
                    })
                    (RAW / "subtitle_file.json").write_text(
                        json.dumps(ctx.red.redact_obj(trim(j)), ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    print("  [run ] subtitle_file [匿名] CDN 字幕 JSON")
                except Exception as exc:
                    ctx.records.append({
                        "probe_id": "subtitle_file", "group": "C2 字幕",
                        "title": "字幕文件本体（CDN 直取，不带 Cookie）",
                        "identity": "anon", "path": url.split("?")[0], "params": {},
                        "wbi": False, "note": "验证拿到 subtitle_url 后能否直接下载",
                        "http_status": 0, "code": None,
                        "message": f"{type(exc).__name__}: {exc}", "code_meaning": "失败",
                        "elapsed_ms": None, "item_count": None, "item_keys": [],
                        "observations": ["字幕文件匿名下载失败"],
                    })

    # ---- 字幕覆盖率：决定"到底有多少视频需要跑本地 ASR" ---- #
    if wanted("subtitle_coverage"):
        scan = pub[:10]
        rows = []
        for v in scan:
            bv = v.get("bvid")
            vr = ctx.clients["auth"].get_json("/x/web-interface/view", {"bvid": bv},
                                              referer=f"https://www.bilibili.com/video/{bv}")
            pages_v = (vr.get("data") or {}).get("pages") or []
            if not pages_v:
                rows.append({"bvid": bv, "error": f"view code={vr.get('code')}"})
                continue
            pr = ctx.clients["auth"].get_json("/x/player/wbi/v2",
                                              {"bvid": bv, "cid": pages_v[0]["cid"]}, wbi=True,
                                              referer=f"https://www.bilibili.com/video/{bv}")
            subs = ((pr.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            rows.append({"bvid": bv, "charging": bool(v.get("is_charging_arc")),
                         "pages": len(pages_v),
                         "langs": [s.get("lan") for s in subs],
                         "has_zh": any(str(s.get("lan", "")).startswith("ai-zh") or
                                       s.get("lan") == "zh-CN" for s in subs)})
        with_zh = len([r for r in rows if r.get("has_zh")])
        ctx.records.append({
            "probe_id": "subtitle_coverage", "group": "C2 字幕",
            "title": f"字幕覆盖率 —— 公开列表前 {len(rows)} 条逐个检查",
            "identity": "auth", "path": "/x/player/wbi/v2", "params": {}, "wbi": True,
            "note": "决定本地 ASR 的必要性：有中文字幕的视频不需要跑 ASR",
            "http_status": 200, "code": 0, "message": "OK", "code_meaning": "成功",
            "elapsed_ms": None, "item_count": len(rows),
            "item_keys": sorted(rows[0].keys()) if rows else [],
            "observations": [f"{with_zh}/{len(rows)} 条有中文字幕"
                             f"（占 {round(100 * with_zh / max(1, len(rows)))}%）"] +
                            [f"{r.get('bvid')} 充电={r.get('charging')} P={r.get('pages')} "
                             f"字幕={r.get('langs')}" for r in rows],
            "rows": rows,
        })
        (RAW / "subtitle_coverage.json").write_text(
            json.dumps(ctx.red.redact_obj(rows), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  [run ] subtitle_coverage [授权] {with_zh}/{len(rows)} 有中文字幕")

    # ---- 多 P 样本：需求 3.3.1 要求"多 P 一个条目一个文件夹" ---- #
    if wanted("multipart_sample"):
        cands: list[str] = []
        rk = ctx.clients["auth"].get_json("/x/web-interface/ranking/v2",
                                          {"rid": 0, "type": "all"},
                                          referer="https://www.bilibili.com/v/popular/rank/all")
        cands += [v.get("bvid") for v in ((rk.get("data") or {}).get("list") or [])][:16]
        sr = ctx.clients["auth"].get_json("/x/web-interface/wbi/search/type",
                                          {"search_type": "video", "keyword": "教程 合集",
                                           "page": 1}, wbi=True,
                                          referer="https://search.bilibili.com/")
        cands += [r.get("bvid") for r in ((sr.get("data") or {}).get("result") or [])]

        found_bv, found_data = "", {}
        for bv in [c for c in cands if c and str(c).startswith("BV")][:22]:
            vr = ctx.clients["auth"].get_json("/x/web-interface/view", {"bvid": bv},
                                              referer=f"https://www.bilibili.com/video/{bv}")
            d = vr.get("data") or {}
            if int(d.get("videos") or 0) > 1:
                found_bv, found_data = bv, d
                break
        pages_m = found_data.get("pages") or []
        ctx.records.append({
            "probe_id": "multipart_sample", "group": "C2 视频详情",
            "title": "多 P 视频样本（分 P 归档依据）",
            "identity": "auth", "path": "/x/web-interface/view",
            "params": {"bvid": found_bv or "(未找到)"}, "wbi": False,
            "note": "需求 3.3.1：多 P 作为一个条目、一个文件夹，各 P 媒体分开放",
            "http_status": 200, "code": 0 if found_bv else None, "message": "OK",
            "code_meaning": "成功" if found_bv else "未在候选集中找到多 P 视频",
            "elapsed_ms": None, "item_count": len(pages_m),
            "item_keys": sorted(pages_m[0].keys()) if pages_m else [],
            "observations": ([f"找到多 P 样本 {found_bv}：videos={found_data.get('videos')}，"
                              f"pages={len(pages_m)}"]
                             + [f"P{p.get('page')}: cid={p.get('cid')} part={p.get('part')!r} "
                                f"duration={p.get('duration')}s" for p in pages_m[:5]])
                            if found_bv else ["未在 22 个候选 BV 中找到多 P 视频"],
            "pages": [{"page": p.get("page"), "cid": p.get("cid"), "part": p.get("part"),
                       "duration": p.get("duration")} for p in pages_m],
        })
        (RAW / "multipart_sample.json").write_text(
            json.dumps(ctx.red.redact_obj({"bvid": found_bv, "videos": found_data.get("videos"),
                                           "pages": pages_m}), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  [run ] multipart_sample [授权] {found_bv or '未找到'} ({len(pages_m)} P)")

    # ---- 媒体可达性：动态原图 / 专栏图片 ---- #
    if wanted("media_reach"):
        # 1) 动态原图：翻图文列表找带图片的条目
        pic_url, pic_meta = "", {}
        offset = ""
        for _ in range(4):
            rr = ctx.clients["auth"].get_json(
                "/x/polymer/web-dynamic/v1/opus/feed/space",
                {"host_mid": uid, "page": 1, "offset": offset, "type": "all"}, wbi=True,
                referer=f"https://space.bilibili.com/{uid}/article")
            dd = rr.get("data") or {}
            for it in dd.get("items") or []:
                oid = it.get("opus_id")
                od = ctx.clients["auth"].get_json(
                    "/x/polymer/web-dynamic/v1/opus/detail", {"id": oid}, wbi=True,
                    referer=f"https://www.bilibili.com/opus/{oid}")
                for m in ((od.get("data") or {}).get("item") or {}).get("modules") or []:
                    if m.get("module_type") != "MODULE_TYPE_CONTENT":
                        continue
                    for p in (m.get("module_content") or {}).get("paragraphs") or []:
                        if p.get("para_type") == 2 and (p.get("pic") or {}).get("pics"):
                            first = p["pic"]["pics"][0]
                            pic_url = first.get("url") or ""
                            pic_meta = {k: first.get(k) for k in
                                        ("url", "width", "height", "size", "type")}
                            break
                    if pic_url:
                        break
                if pic_url:
                    break
            if pic_url or not dd.get("has_more"):
                break
            offset = dd.get("offset") or ""

        obs = [f"动态原图 URL 是否找到 = {bool(pic_url)}"]
        if pic_meta:
            obs.append(f"图片元数据字段 = {sorted(pic_meta.keys())}")
            obs.append(f"尺寸 = {pic_meta.get('width')}x{pic_meta.get('height')} "
                       f"type = {pic_meta.get('type')}")
            obs.append(f"URL 形如 = {str(pic_meta.get('url'))[:110]}")
        if pic_url:
            try:
                r = ctx.clients["anon"].open(pic_url, referer=f"https://space.bilibili.com/{uid}")
                blob = r.read()
                obs.append(f"匿名下载成功：{len(blob)} 字节，Content-Type="
                           f"{r.headers.get('Content-Type')}")
            except Exception as exc:
                obs.append(f"匿名下载失败：{type(exc).__name__}: {exc}")
        ctx.records.append({
            "probe_id": "dynamic_image_reach", "group": "D 动态",
            "title": "动态原图可达性（图文段落 para_type=2）",
            "identity": "auth", "path": "/x/polymer/web-dynamic/v1/opus/detail",
            "params": {}, "wbi": True,
            "note": "需求 3.2.1：保存配图原文件并保留原顺序",
            "http_status": 200, "code": 0, "message": "OK", "code_meaning": "成功",
            "elapsed_ms": None, "item_count": 1 if pic_url else 0,
            "item_keys": sorted(pic_meta.keys()), "observations": obs,
        })
        (RAW / "dynamic_image_reach.json").write_text(
            json.dumps(ctx.red.redact_obj(pic_meta), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  [run ] dynamic_image_reach [授权] found={bool(pic_url)}")

    # ================= D. 动态 ================= #
    print("\n[D] 动态")
    dyn_params = {"host_mid": uid, "offset": "", "timezone_offset": -480, "platform": "web",
                  "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                  "web_location": 333.1387}

    def a_dyn(r):
        d = r.get("data") or {}
        items = d.get("items") or []
        out = [f"has_more = {d.get('has_more')}，offset {'存在' if d.get('offset') else '缺失'}",
               f"条目数 = {len(items)}"]
        types: dict[str, int] = {}
        for it in items:
            t = it.get("type") or "?"
            types[t] = types.get(t, 0) + 1
        if types:
            out.append(f"条目类型分布 = {types}")
        if items:
            out.append("条目字段：" + ", ".join(sorted(items[0].keys())))
            out.append(f"modules 数 = {len(items[0].get('modules') or [])}")
        return out

    if wanted("dynamic_feed_space"):
        ctx.matrix("dynamic_feed_space", "D 动态", "空间动态流（offset 游标分页）",
                   "/x/polymer/web-dynamic/v1/feed/space", dyn_params, wbi=True,
                   referer=f"https://space.bilibili.com/{uid}/dynamic",
                   note="动态列表主接口", analyse=a_dyn)
    if wanted("dynamic_opus_feed"):
        def a_opus(r):
            d = r.get("data") or {}
            items = d.get("items") or []
            out = [f"has_more = {d.get('has_more')}，offset {'存在' if d.get('offset') else '缺失'}",
                   f"条目数 = {len(items)}",
                   f"带 badge（充电专属）条目数 = {len([it for it in items if it.get('badge')])}"]
            if items:
                out.append("条目字段：" + ", ".join(sorted(items[0].keys())))
                out.append("时间类字段 = "
                           + str([k for k in items[0] if "time" in k.lower() or "ts" in k.lower()]))
                out.append(f"首条 pub_time = {items[0].get('pub_time')!r}"
                           f"（空 = 列表不提供发布时间）")
                out.append(f"首条正文长度 = {len(items[0].get('content') or '')}")
            return out
        ctx.matrix("dynamic_opus_feed", "D 动态", "空间图文（opus）列表",
                   "/x/polymer/web-dynamic/v1/opus/feed/space",
                   {"host_mid": uid, "page": 1, "offset": "", "type": "all"},
                   wbi=True, identities=("auth", "anon_buvid"),
                   referer=f"https://space.bilibili.com/{uid}/article",
                   note="旧工程实测可用；关注 badge 与发布时间可得性", analyse=a_opus)

    dyn_items = ctx.items("dynamic_feed_space__auth")
    dyn_id = str(dyn_items[0].get("id_str") or dyn_items[0].get("id") or "") if dyn_items else ""
    if dyn_id and wanted("dynamic_detail"):
        ctx.matrix("dynamic_detail", "D 动态", "动态详情（原图与完整正文）",
                   "/x/polymer/web-dynamic/v1/detail",
                   {"id": dyn_id, "timezone_offset": -480,
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote"},
                   wbi=True, identities=("auth", "anon_buvid"),
                   referer=f"https://www.bilibili.com/opus/{dyn_id}",
                   note="详情用于取原图列表与完整正文",
                   analyse=lambda r: [
                       f"type = {(((r.get('data') or {}).get('item') or {}).get('type'))}",
                       f"modules 形状 = "
                       f"{type((((r.get('data') or {}).get('item') or {}).get('modules'))).__name__}"
                       f"（detail 接口是 dict，opus/detail 接口是 list）",
                       f"正文长度 = {dynamic_text(((r.get('data') or {}).get('item') or {}))[0]}",
                       f"图片数 = {dynamic_text(((r.get('data') or {}).get('item') or {}))[1]}",
                       f"pub_ts = {dynamic_text(((r.get('data') or {}).get('item') or {}))[2]}"
                       f"（{_fmt_ts(dynamic_text(((r.get('data') or {}).get('item') or {}))[2])}）"])
        ctx.remember("dynamic_detail_id", dyn_id)

    # 逐类型的字段路径证据：每种动态类型取一条，记录 major 结构与图片/文本路径
    if wanted("dynamic_shapes"):
        seen: dict[str, str] = {}
        offset = ""
        pages_scanned = 0
        for _ in range(6):
            rr = ctx.clients["auth"].get_json(
                "/x/polymer/web-dynamic/v1/feed/space",
                {**dyn_params, "offset": offset}, wbi=True,
                referer=f"https://space.bilibili.com/{uid}/dynamic")
            dd = rr.get("data") or {}
            pages_scanned += 1
            for it in dd.get("items") or []:
                t = it.get("type")
                if t and t not in seen:
                    seen[t] = str(it.get("id_str") or it.get("id") or "")
            if not dd.get("has_more"):
                break
            offset = dd.get("offset") or ""

        shape_recs = []
        for dtype, did in seen.items():
            rr = ctx.clients["auth"].get_json(
                "/x/polymer/web-dynamic/v1/detail",
                {"id": did, "timezone_offset": -480,
                 "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote"},
                wbi=True, referer=f"https://www.bilibili.com/opus/{did}")
            item = (rr.get("data") or {}).get("item") or {}
            mods = item.get("modules") or {}
            md = (mods.get("module_dynamic") or {}) if isinstance(mods, dict) else {}
            mt, m_text, m_pics, extra = major_shape(md.get("major") or {})
            author = (mods.get("module_author") or {}) if isinstance(mods, dict) else {}
            shape_recs.append({
                "type": dtype, "major_type": mt, "id": did,
                "desc_len": len(rich_text(md.get("desc"))),
                "major_text_len": m_text, "major_pics": m_pics,
                "module_keys": sorted(mods.keys()) if isinstance(mods, dict) else "list",
                "pub_ts": author.get("pub_ts"), "pub_time": author.get("pub_time"),
                "extra": extra,
            })
        ctx.records.append({
            "probe_id": "dynamic_shapes", "group": "D 动态",
            "title": "各动态类型的字段路径（逐类型取样）",
            "identity": "auth", "path": "/x/polymer/web-dynamic/v1/detail",
            "params": {}, "wbi": True,
            "note": "阶段 1 解析动态正文/图片/转发来源的字段依据",
            "http_status": 200, "code": 0, "message": "OK", "code_meaning": "成功",
            "elapsed_ms": None, "item_count": len(shape_recs),
            "item_keys": sorted(shape_recs[0].keys()) if shape_recs else [],
            "observations": [f"扫描动态流 {pages_scanned} 页，覆盖 {len(seen)} 种类型"] +
                            [f"{s['type']}: major={s['major_type']} "
                             f"desc_len={s['desc_len']} major_text={s['major_text_len']} "
                             f"pics={s['major_pics']} pub_time={s['pub_time']} {s['extra']}"
                             for s in shape_recs],
            "shapes": shape_recs,
        })
        (RAW / "dynamic_shapes.json").write_text(
            json.dumps(ctx.red.redact_obj(shape_recs), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  [run ] dynamic_shapes [授权] {len(shape_recs)} 种类型")

    opus_items = ctx.items("dynamic_opus_feed__auth")
    opus_id = str(opus_items[0].get("opus_id") or "") if opus_items else ""
    if opus_id and wanted("opus_detail"):
        def a_odetail(r):
            item = (r.get("data") or {}).get("item") or {}
            tl, pics, pub_ts = dynamic_text(item)
            return [f"modules = {[m.get('module_type') for m in (item.get('modules') or [])]}",
                    f"正文长度 = {tl} / 图片数 = {pics}",
                    f"module_author.pub_ts = {pub_ts}（{_fmt_ts(pub_ts)}）",
                    f"is_only_fans = {(item.get('basic') or {}).get('is_only_fans')}"]
        # 注意：参数名是 id，不是 opus_id（opus_id 会返回 -400）
        ctx.call("opus_detail_bad_param", "D 动态", "opus 详情 —— 错误参数名对照",
                 "/x/polymer/web-dynamic/v1/opus/detail", {"opus_id": opus_id}, wbi=True,
                 referer=f"https://www.bilibili.com/opus/{opus_id}",
                 note="记录 opus_id 参数名无效，适配层必须用 id")
        ctx.matrix("opus_detail", "D 动态", "opus 详情（正文/图片/发布时间）",
                   "/x/polymer/web-dynamic/v1/opus/detail", {"id": opus_id}, wbi=True,
                   identities=("auth", "anon_buvid"),
                   referer=f"https://www.bilibili.com/opus/{opus_id}",
                   note="**内容门控证据**：充电专属动态的正文对匿名是否为空",
                   analyse=a_odetail)
        ctx.remember("opus_id", opus_id)

    # ================= E. 专栏 ================= #
    print("\n[E] 专栏")
    if wanted("article_list"):
        def a_art(r):
            d = r.get("data") or {}
            arts = d.get("articles")
            out = [f"count = {d.get('count')}（接口报告的专栏总数）",
                   f"data 字段 = {sorted(d.keys())}"]
            if arts is None:
                out.append("响应中**没有 articles 字段** —— 条目列表不由本接口提供；"
                           "专栏条目须改用 opus feed `type=article`（见 article_opus_feed）")
            else:
                out.append(f"本页条目数 = {len(arts)}")
                if arts:
                    out.append("字段：" + ", ".join(sorted(arts[0].keys())))
                    out += check_order(arts, "publish_time")
            return out
        ctx.matrix("article_list", "E 专栏", "专栏计数接口（wbi）—— 只给 count",
                   "/x/space/wbi/article",
                   {"mid": uid, "pn": 1, "ps": 30, "sort": "publish_time"},
                   wbi=True, referer=f"https://space.bilibili.com/{uid}/article",
                   note="该接口只返回 {pn,ps,count}，不返回条目；条目见 article_opus_feed",
                   analyse=a_art)
    if wanted("article_list_legacy"):
        ctx.call("article_list_legacy", "E 专栏", "专栏计数（旧接口对照）",
                 "/x/space/article",
                 {"mid": uid, "pn": 1, "ps": 30, "sort": "publish_time"},
                 identity="auth", referer=f"https://space.bilibili.com/{uid}/article",
                 note="旧接口同样只给 count，已不提供条目")

    # ---- 专栏条目列表：opus feed 的 type=article（本次验证的关键修正） ---- #
    if wanted("article_opus_feed"):
        def a_ofeed(r):
            d = r.get("data") or {}
            items = d.get("items") or []
            out = [f"has_more = {d.get('has_more')}",
                   f"本页条目数 = {len(items)}",
                   f"带 badge 条目数 = {len([i for i in items if i.get('badge')])}"]
            if items:
                out.append("条目字段：" + ", ".join(sorted(items[0].keys())))
                out.append(f"jump_url 形如 = {items[0].get('jump_url')}")
            return out
        ctx.matrix("article_opus_feed", "E 专栏",
                   "**专栏条目列表**（opus feed, type=article）",
                   "/x/polymer/web-dynamic/v1/opus/feed/space",
                   {"host_mid": uid, "page": 1, "offset": "", "type": "article"},
                   wbi=True, identities=("auth", "anon_buvid"),
                   referer=f"https://space.bilibili.com/{uid}/article",
                   note="关键修正：type=article 生效且与 type=all 不重叠，这才是专栏列表",
                   analyse=a_ofeed)

        # 全量翻页，与 count 对照
        ids: list[str] = []
        offset, page, has_more = "", 0, True
        while has_more and page < 12:
            rr = ctx.clients["auth"].get_json(
                "/x/polymer/web-dynamic/v1/opus/feed/space",
                {"host_mid": uid, "page": page + 1, "offset": offset, "type": "article"},
                wbi=True, referer=f"https://space.bilibili.com/{uid}/article")
            dd = rr.get("data") or {}
            page_items = dd.get("items") or []
            ids += [str(i.get("opus_id")) for i in page_items]
            has_more = bool(dd.get("has_more")) and bool(page_items)
            offset = dd.get("offset") or ""
            page += 1
        reported = ((ctx.resp("article_list__auth").get("data") or {}).get("count"))
        ctx.remember("article_opus_ids", ids)
        ctx.records.append({
            "probe_id": "article_opus_feed_total", "group": "E 专栏",
            "title": "专栏条目全量翻页 vs 计数接口",
            "identity": "auth", "path": "/x/polymer/web-dynamic/v1/opus/feed/space",
            "params": {"type": "article"}, "wbi": True,
            "note": "把 type=article 翻到底，与 /x/space/wbi/article 的 count 对照",
            "http_status": 200, "code": 0, "message": "OK", "code_meaning": "成功",
            "elapsed_ms": None, "item_count": len(ids), "item_keys": [],
            "observations": [f"type=article 翻 {page} 页共 {len(ids)} 条（去重 {len(set(ids))}）",
                             f"计数接口 count = {reported}",
                             f"两者是否一致 = {reported == len(ids)}",
                             f"首条（最新）= {ids[0] if ids else '-'}"],
        })
        (RAW / "article_opus_feed_total.json").write_text(
            json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [run ] article_opus_feed_total [授权] {len(ids)} 条（count={reported}）")

        # type=all 与 type=article 是否重叠（证明两者是不同列表）
        all_ids: list[str] = []
        offset, page = "", 0
        while page < 3:
            rr = ctx.clients["auth"].get_json(
                "/x/polymer/web-dynamic/v1/opus/feed/space",
                {"host_mid": uid, "page": page + 1, "offset": offset, "type": "all"},
                wbi=True, referer=f"https://space.bilibili.com/{uid}/article")
            dd = rr.get("data") or {}
            all_ids += [str(i.get("opus_id")) for i in (dd.get("items") or [])]
            if not dd.get("has_more"):
                break
            offset = dd.get("offset") or ""
            page += 1
        overlap = len(set(all_ids) & set(ids))
        ctx.records.append({
            "probe_id": "article_vs_all_disjoint", "group": "E 专栏",
            "title": "type=all 与 type=article 是否同一列表",
            "identity": "auth", "path": "/x/polymer/web-dynamic/v1/opus/feed/space",
            "params": {"type": "all vs article"}, "wbi": True,
            "note": "若交集为 0，说明 type 参数生效、专栏是独立列表",
            "http_status": 200, "code": 0, "message": "OK", "code_meaning": "成功",
            "elapsed_ms": None, "item_count": overlap, "item_keys": [],
            "observations": [f"type=all 取样 {len(all_ids)} 条，与专栏列表交集 = {overlap}",
                             f"type=all 首条 = {all_ids[0] if all_ids else '-'}",
                             f"type=article 首条 = {ids[0] if ids else '-'}",
                             "交集为 0 → type 参数生效，两者是不同列表"
                             if overlap == 0 else "存在交集 → 需重新判断"],
        })
        print(f"  [run ] article_vs_all_disjoint [授权] 交集={overlap}")

    # ---- 专栏详情 + 风控失败率实测 ---- #
    if wanted("article_opus_detail"):
        art_ids = ctx.recall("article_opus_ids") or []
        if art_ids:
            def a_odetail(r):
                item = (r.get("data") or {}).get("item") or {}
                basic = item.get("basic") or {}
                title = paras = pics = text_len = pub_ts = 0
                title = ""
                for m in item.get("modules") or []:
                    mt = m.get("module_type")
                    if mt == "MODULE_TYPE_TITLE":
                        title = (m.get("module_title") or {}).get("text") or ""
                    elif mt == "MODULE_TYPE_AUTHOR":
                        try:
                            pub_ts = int((m.get("module_author") or {}).get("pub_ts") or 0)
                        except (TypeError, ValueError):
                            pub_ts = 0
                    elif mt == "MODULE_TYPE_CONTENT":
                        ps = (m.get("module_content") or {}).get("paragraphs") or []
                        paras = len(ps)
                        pics = len([p for p in ps if p.get("para_type") == 2])
                        text_len = sum(
                            len((n.get("word") or {}).get("words") or "")
                            for p in ps for n in ((p.get("text") or {}).get("nodes") or []))
                return [f"title = {title!r}",
                        f"item.type = {item.get('type')} / basic.article_type = "
                        f"{basic.get('article_type')}（专栏与动态图文的判别位）",
                        f"is_only_fans = {basic.get('is_only_fans')}",
                        f"paragraphs = {paras} / 图片段落 = {pics} / 正文长度 = {text_len}",
                        f"pub_ts = {pub_ts}（{_fmt_ts(pub_ts)}）"]
            ctx.matrix("article_opus_detail", "E 专栏",
                       "专栏正文详情（opus detail, id=）",
                       "/x/polymer/web-dynamic/v1/opus/detail",
                       {"id": art_ids[0]}, wbi=True, identities=("auth", "anon_buvid"),
                       referer=f"https://www.bilibili.com/opus/{art_ids[0]}",
                       note="专栏正文即 opus 的 paragraphs；图片在 para_type=2",
                       analyse=a_odetail)

            # 全量清单：逐条取详情（-352 时重试一次），统计风控失败率并落盘清单
            inventory, retried_ok = [], 0
            for oid in art_ids:
                rr = ctx.clients["auth"].get_json(
                    "/x/polymer/web-dynamic/v1/opus/detail", {"id": oid}, wbi=True,
                    referer=f"https://www.bilibili.com/opus/{oid}")
                if rr.get("code") != 0:
                    # -352 是间歇性的：稍后重试一次（契约 10）
                    time.sleep(2.0)
                    rr = ctx.clients["auth"].get_json(
                        "/x/polymer/web-dynamic/v1/opus/detail", {"id": oid}, wbi=True,
                        referer=f"https://www.bilibili.com/opus/{oid}")
                    if rr.get("code") == 0:
                        retried_ok += 1
                item = (rr.get("data") or {}).get("item") or {}
                basic = item.get("basic") or {}
                title, paras, pics, text_len, pub_ts = "", 0, 0, 0, 0
                for m in item.get("modules") or []:
                    mt = m.get("module_type")
                    if mt == "MODULE_TYPE_TITLE":
                        title = (m.get("module_title") or {}).get("text") or ""
                    elif mt == "MODULE_TYPE_AUTHOR":
                        try:
                            pub_ts = int((m.get("module_author") or {}).get("pub_ts") or 0)
                        except (TypeError, ValueError):
                            pub_ts = 0
                    elif mt == "MODULE_TYPE_CONTENT":
                        ps = (m.get("module_content") or {}).get("paragraphs") or []
                        paras = len(ps)
                        pics = len([p for p in ps if p.get("para_type") == 2])
                        text_len = sum(
                            len((n.get("word") or {}).get("words") or "")
                            for p in ps for n in ((p.get("text") or {}).get("nodes") or []))
                inventory.append({
                    "opus_id": oid, "code": rr.get("code"),
                    "type": item.get("type"), "article_type": basic.get("article_type"),
                    "is_only_fans": basic.get("is_only_fans"), "title": title,
                    "paragraphs": paras, "image_paragraphs": pics,
                    "text_len": text_len, "pub_ts": pub_ts,
                })
            ok_items = [r for r in inventory if r["code"] == 0]
            charged = [r for r in ok_items if r["is_only_fans"]]
            lens = sorted((r["text_len"] for r in ok_items), reverse=True)
            paras = sorted((r["paragraphs"] for r in ok_items), reverse=True)
            ctx.records.append({
                "probe_id": "article_opus_inventory", "group": "E 专栏",
                "title": "专栏全量清单（44 篇逐条取详情）",
                "identity": "auth", "path": "/x/polymer/web-dynamic/v1/opus/detail",
                "params": {"id": "全部专栏 opus_id"}, "wbi": True,
                "note": "阶段 3 分块策略与阶段 1 时间筛选的规模依据；"
                        "同时实测 -352 重试是否有效",
                "http_status": 200, "code": None,
                "message": f"成功 {len(ok_items)}/{len(inventory)}",
                "code_meaning": "—", "elapsed_ms": None,
                "item_count": len(ok_items),
                "item_keys": sorted(inventory[0].keys()) if inventory else [],
                "observations": [
                    f"成功 {len(ok_items)}/{len(inventory)} 条（首次失败后重试成功 {retried_ok} 条）",
                    f"失败 code 分布 = "
                    f"{ {r['code'] for r in inventory if r['code'] != 0} }",
                    f"充电专属 = {len(charged)}/{len(ok_items)}",
                    f"正文长度 最大 = {lens[0] if lens else 0} / 中位 = "
                    f"{lens[len(lens) // 2] if lens else 0}",
                    f"段落数 最大 = {paras[0] if paras else 0} / 中位 = "
                    f"{paras[len(paras) // 2] if paras else 0}",
                    f"有图片段落的篇数 = {len([r for r in ok_items if r['image_paragraphs']])}",
                ],
            })
            (RAW / "article_opus_inventory.json").write_text(
                json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [run ] article_opus_inventory [授权] "
                  f"{len(ok_items)}/{len(inventory)} 成功，重试救回 {retried_ok} 条")

    # 专栏公开样本闭环：找一个确实有专栏的作者
    if wanted("article_sample"):
        sr = ctx.clients["auth"].get_json(
            "/x/web-interface/wbi/search/type",
            {"search_type": "article", "keyword": "市场分析", "page": 1}, wbi=True,
            referer="https://search.bilibili.com/")
        results = (sr.get("data") or {}).get("result") or []
        sample_mid = str(results[0].get("mid")) if results else ""
        ctx.records.append({
            "probe_id": "article_sample_search", "group": "E 专栏",
            "title": "用搜索接口定位一个确实有专栏的作者（专栏闭环取样）",
            "identity": "auth", "path": "/x/web-interface/wbi/search/type",
            "params": {"search_type": "article", "keyword": "市场分析", "page": 1},
            "wbi": True, "note": "验证专栏列表接口对别的 UP 是否正常，以区分"
                                 "「接口全局失效」与「本样本专栏不可得」",
            "http_status": sr.get("http_status"), "code": sr.get("code"),
            "message": ctx.red.redact(str(sr.get("message", ""))),
            "code_meaning": describe_code(sr.get("code")),
            "elapsed_ms": sr.get("elapsed_ms"),
            "item_count": len(results), "item_keys": sorted(results[0].keys()) if results else [],
            "observations": [f"命中条目 = {len(results)}",
                             f"取样作者 mid = {sample_mid}",
                             f"样例 cv 号 = {results[0].get('id') if results else '无'}"],
        })
        (RAW / "article_sample_search.json").write_text(
            json.dumps(ctx.red.redact_obj(trim(sr)), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("  [run ] article_sample_search [授权] /x/web-interface/wbi/search/type")
        if sample_mid:
            ctx.call("article_list_sample", "E 专栏",
                     f"专栏计数/列表 —— 对照作者 mid={sample_mid}（旧格式 cv 专栏）",
                     "/x/space/wbi/article",
                     {"mid": sample_mid, "pn": 1, "ps": 30, "sort": "publish_time"},
                     wbi=True, identity="auth",
                     referer=f"https://space.bilibili.com/{sample_mid}/article",
                     note="**对照组**：该 UP 的专栏是旧格式（read/cv），本接口会返回 articles[]",
                     analyse=lambda r: [
                         f"data 字段 = {sorted((r.get('data') or {}).keys())}",
                         f"count = {(r.get('data') or {}).get('count')}",
                         f"articles = {len((r.get('data') or {}).get('articles') or [])}"])
            # 同一 UP 的 opus 专栏列表：若为空，说明两种格式是两个独立来源
            ctx.call("article_opus_feed_sample", "E 专栏",
                     f"opus 专栏列表 —— 同一对照作者 mid={sample_mid}",
                     "/x/polymer/web-dynamic/v1/opus/feed/space",
                     {"host_mid": sample_mid, "page": 1, "offset": "", "type": "article"},
                     wbi=True, identity="auth",
                     referer=f"https://space.bilibili.com/{sample_mid}/article",
                     note="与 article_list_sample 对照：判断两种专栏格式是否各自独立计数",
                     analyse=lambda r: [
                         f"has_more = {(r.get('data') or {}).get('has_more')}",
                         f"条目数 = {len((r.get('data') or {}).get('items') or [])}",
                         f"首条 jump_url = {(((r.get('data') or {}).get('items') or [{}])[0]).get('jump_url')}"])
            arts = ((ctx.resp("article_list_sample").get("data") or {}).get("articles") or [])
            if arts and wanted("article_detail"):
                cvid = str(arts[0].get("id"))
                ctx.call("article_detail", "E 专栏", "旧格式专栏正文（含内嵌图片）",
                         "/x/article/view", {"id": cvid}, identity="auth",
                         referer=f"https://www.bilibili.com/read/cv{cvid}",
                         note="正文为 HTML，需解析为 Markdown 并改写图片为相对路径",
                         analyse=lambda r: [
                             f"title = {(r.get('data') or {}).get('title')}",
                             f"正文 HTML 长度 = {len((r.get('data') or {}).get('content') or '')}",
                             f"<img> 数 = {len(re.findall('<img', (r.get('data') or {}).get('content') or ''))}",
                             f"<p> 数 = {len(re.findall('<p', (r.get('data') or {}).get('content') or ''))}",
                             f"image_urls = {len((r.get('data') or {}).get('image_urls') or [])}"])

    # ---- 专栏图片可达性（必须在 article_detail 之后） ---- #
    if wanted("article_image_reach"):
        art_resp = ctx.resp("article_detail").get("data") or {}
        imgs = art_resp.get("image_urls") or []
        aobs = [f"image_urls 数 = {len(imgs)}"]
        if imgs:
            aobs.append(f"URL 形如 = {str(imgs[0])[:110]}")
            try:
                r = ctx.clients["anon"].open(imgs[0], referer="https://www.bilibili.com/")
                blob = r.read()
                aobs.append(f"匿名下载成功：{len(blob)} 字节，Content-Type="
                            f"{r.headers.get('Content-Type')}")
            except Exception as exc:
                aobs.append(f"匿名下载失败：{type(exc).__name__}: {exc}")
        ctx.records.append({
            "probe_id": "article_image_reach", "group": "E 专栏",
            "title": "专栏图片可达性（image_urls 原图）",
            "identity": "auth", "path": "/x/article/view", "params": {}, "wbi": False,
            "note": "需求 3.4.1：保存正文中可获取的图片；无法取得的保留原链接与错误状态",
            "http_status": 200, "code": 0, "message": "OK", "code_meaning": "成功",
            "elapsed_ms": None, "item_count": len(imgs), "item_keys": [],
            "observations": aobs,
        })
        print(f"  [run ] article_image_reach [授权] {len(imgs)} 张")

    # ================= F. 错误码可区分性 ================= #
    print("\n[F] 错误码可区分性")
    errs = [
        ("err_deleted_video", "格式合法但不存在的稿件", "/x/web-interface/view",
         {"bvid": "BV1Wk4y1B7Qs"}, "**62002 同时覆盖「已删除」与「无权访问」，二者不可区分**"),
        ("err_bad_bvid", "格式非法的 BV 号", "/x/web-interface/view",
         {"bvid": "BV0000000000"}, "参数错误应与权限错误区分"),
        ("err_nonexistent_aid", "不存在的 aid", "/x/web-interface/view",
         {"aid": 99999999999}, "aid 路径可得到 -404"),
        ("err_bad_article", "格式非法的专栏 id", "/x/article/view", {"id": "abc"},
         "参数错误"),
        ("err_nonexistent_article", "不存在的专栏 id", "/x/article/view",
         {"id": 99999999999}, "专栏不存在 → -404"),
    ]
    for pid, title, path, params, note in errs:
        if wanted(pid):
            ctx.call(pid, "F 错误码", title, path, params, identity="auth", note=note)


# --------------------------------------------------------------------------- #
# 汇总与报告
# --------------------------------------------------------------------------- #
def build_summary(ctx: Ctx) -> dict:
    by_group: dict[str, list] = {}
    for rec in ctx.records:
        by_group.setdefault(rec["group"], []).append(rec)

    comparison: dict[str, Any] = {}
    pub = ctx.recall("public_videos") or []
    charge = ctx.recall("charging_videos") or []
    pub_ids = {v.get("bvid") for v in pub if isinstance(v, dict)}
    charge_ids = {v.get("bvid") for v in charge if isinstance(v, dict)}
    if pub or charge:
        comparison["video_public_total"] = ctx.recall("public_total")
        comparison["video_public_page1_count_auth"] = len(pub)
        comparison["video_public_page1_charging_items"] = len(
            [v for v in pub if v.get("is_charging_arc")])
        comparison["video_public_page1_count_anon_buvid"] = ctx.recall("public_anon_count")
        comparison["video_charging_total"] = (
            (ctx.resp("video_list_charging__auth").get("data") or {}).get("page") or {}).get("count")
        comparison["video_charging_page1_count_auth"] = len(charge)
        comparison["overlap_bvid_page1"] = len(pub_ids & charge_ids)
        comparison["charging_only_in_charging_list"] = len(charge_ids - pub_ids)
        comparison["public_only_in_public_list"] = len(pub_ids - charge_ids)

    # 动态 opus：授权与匿名的 id 集合是否一致
    a_ids = [i.get("opus_id") for i in ctx.items("dynamic_opus_feed__auth")]
    b_ids = [i.get("opus_id") for i in ctx.items("dynamic_opus_feed__anon_buvid")]
    if a_ids or b_ids:
        comparison["opus_feed_auth_ids"] = len(a_ids)
        comparison["opus_feed_anon_ids"] = len(b_ids)
        comparison["opus_feed_intersection"] = len(set(a_ids) & set(b_ids))
        comparison["opus_feed_badged_auth"] = len(
            [i for i in ctx.items("dynamic_opus_feed__auth") if i.get("badge")])
        comparison["opus_feed_badged_anon"] = len(
            [i for i in ctx.items("dynamic_opus_feed__anon_buvid") if i.get("badge")])

    ident_matrix: dict[str, dict] = {}
    for rec in ctx.records:
        pid = rec.get("probe_id", "")
        if "__" not in pid:
            continue
        base, ident = pid.rsplit("__", 1)
        ident_matrix.setdefault(base, {})[ident] = {
            "http": rec.get("http_status"), "code": rec.get("code"),
            "items": rec.get("item_count"),
        }

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": ctx.mode, "uid": ctx.uid,
        "cookie_present": ctx.creds.has_cookie,
        "cookie_source": ctx.creds.source,
        "cookie_field_names": ctx.creds.cookie_field_names,
        "probe_count": len(ctx.records),
        "identity_matrix": ident_matrix,
        "by_group": by_group,
        "comparison": comparison,
    }


def render_results_md(summary: dict) -> str:
    lines = [
        "# 阶段 0 平台验证 · 自动生成结果", "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 目标 UID：{summary['uid']}",
        f"- 是否携带 Cookie：{'是' if summary['cookie_present'] else '否'}"
        f"（来源：{summary['cookie_source']}）",
        f"- Cookie 字段（仅字段名）：{', '.join(summary['cookie_field_names']) or '无'}",
        f"- 探针数：{summary['probe_count']}", "",
        "> 本文件由 `tools/stage0/probe.py` 自动生成；结论性判断见 "
        "`docs/stage0-platform-verification.md`。", "",
        "## 身份对照矩阵（同一接口 × 三种身份）", "",
        "| 接口 | 授权 | 匿名+buvid | 匿名 |", "| --- | --- | --- | --- |",
    ]

    def cell(entry: dict | None) -> str:
        if not entry:
            return "—"
        return f"HTTP {entry['http']} / code {entry['code']} / {entry['items']} 条"

    for base, idents in summary["identity_matrix"].items():
        lines.append(f"| `{base}` | {cell(idents.get('auth'))} | "
                     f"{cell(idents.get('anon_buvid'))} | {cell(idents.get('anon'))} |")
    lines.append("")

    for group, recs in summary["by_group"].items():
        lines += [f"## {group}", "",
                  "| 探针 | 接口 | 身份 | HTTP | code | 条目 | 观察 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in recs:
            if r.get("skipped"):
                lines.append(f"| `{r['probe_id']}` | — | — | — | — | — | 跳过：{r['reason']} |")
                continue
            obs = "<br>".join(str(o).replace("|", "\\|") for o in r.get("observations", []))
            lines.append(
                f"| `{r['probe_id']}` | `{r['path']}` | {IDENT_LABEL.get(r.get('identity'), '?')} "
                f"| {r.get('http_status')} | {r.get('code')} | {r.get('item_count')} | {obs} |")
        lines.append("")
    if summary.get("comparison"):
        lines += ["## 关键对照数据", "", "```json",
                  json.dumps(summary["comparison"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段 0 平台可行性验证探针")
    ap.add_argument("--uid", type=int, default=0, help="目标 UP 主 UID（覆盖配置文件）")
    ap.add_argument("--mode", choices=["public", "auth", "both"], default="both")
    ap.add_argument("--only", default="", help="逗号分隔的探针前缀，只跑这些")
    ap.add_argument("--root", default=str(ROOT), help="凭据文件所在目录")
    args = ap.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)

    creds = load_credentials(Path(args.root), uid_override=args.uid)
    redactor = Redactor.from_cookie(creds.cookie)

    print("=" * 72)
    print("阶段 0 平台可行性验证")
    print(f"  UID    : {creds.uid or '(未提供)'}")
    print(f"  Cookie : {'已加载' if creds.has_cookie else '未提供（仅公开范围）'}"
          f"  来源={creds.source}  字段数={len(creds.cookie_field_names)}")
    print(f"  模式   : {args.mode}")
    print("=" * 72)

    if not creds.uid:
        print("错误：未提供 UID。请用 --uid 指定，或在 require.txt / config.local.toml 中配置。")
        return 2

    if args.mode == "public":
        creds.cookie = ""
        creds.cookie_field_names = []

    ctx = Ctx(creds, redactor, args.mode)
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    started = time.time()
    run_probes(ctx, only)
    summary = build_summary(ctx)

    (EVIDENCE / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVIDENCE / "RESULTS.md").write_text(render_results_md(summary), encoding="utf-8")

    leaked = []
    for f in EVIDENCE.rglob("*.json"):
        body = f.read_text(encoding="utf-8")
        if creds.cookie:
            for part in creds.cookie.split(";"):
                if "=" in part:
                    val = part.split("=", 1)[1].strip()
                    if len(val) > 12 and val in body:
                        leaked.append(f"{f.name}:{part.split('=', 1)[0].strip()}")
    if leaked:
        print("!! 检测到疑似凭据泄漏，请检查：" + ", ".join(leaked))
        return 3

    print("\n" + "=" * 72)
    print(f"完成：{len(ctx.records)} 个探针，用时 {time.time() - started:.1f}s")
    print(f"  证据目录 : {EVIDENCE}")
    print(f"  结果表   : {EVIDENCE / 'RESULTS.md'}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
