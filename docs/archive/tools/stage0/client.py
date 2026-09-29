"""B 站 API 客户端（阶段 0 验证专用）。

特性：UA/Referer/Cookie、请求限速 + 抖动、WBI 签名、风控退避重试、错误分类。
不写入任何凭据到磁盘。
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from wbi import sign as wbi_sign

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

API = "https://api.bilibili.com"
WWW = "https://www.bilibili.com"


class BiliError(RuntimeError):
    def __init__(self, code: int, message: str, url: str = "", http_status: int = 0):
        super().__init__(f"code={code} msg={message} {url}".strip())
        self.code = code
        self.message = message
        self.url = url
        self.http_status = http_status


class CookieError(BiliError):
    """登录态失效（-101）或未登录。"""


class RiskControlError(BiliError):
    """风控拦截（-352 / HTTP 412 / 429）。"""


class BiliClient:
    def __init__(self, cookie: str = "", interval: float = 0.7, timeout: float = 20,
                 retries: int = 3):
        self.cookie = cookie
        self.interval = max(0.0, interval)
        self.timeout = timeout
        self.retries = max(1, retries)
        self._lock = threading.Lock()
        self._last = 0.0
        self._img_key: str | None = None
        self._sub_key: str | None = None
        self._nav: dict | None = None
        self._nav_ts = 0.0

    # ---------------- 基础请求 ----------------
    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last + self.interval + random.random() * self.interval * 0.5 - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _request(self, url: str, referer: str, origin: str | None = None,
                 extra_headers: dict | None = None) -> urllib.request.Request:
        hdrs = {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if origin:
            hdrs["Origin"] = origin
        if extra_headers:
            hdrs.update(extra_headers)
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        return urllib.request.Request(url, headers=hdrs)

    def open(self, url: str, referer: str = WWW + "/", origin: str | None = None,
             extra_headers: dict | None = None, timeout: float | None = None):
        self._throttle()
        return urllib.request.urlopen(
            self._request(url, referer, origin, extra_headers),
            timeout=timeout or self.timeout,
        )

    # ---------------- JSON API ----------------
    def get_json(
        self,
        path: str,
        params: dict | None = None,
        *,
        referer: str = WWW + "/",
        origin: str | None = None,
        wbi: bool = False,
        retries: int | None = None,
        raise_for_error: bool = False,
    ) -> dict:
        """返回 ``{"http_status", "code", "message", "data", "elapsed_ms", "url"}``。

        默认 **不抛错**（阶段 0 要记录错误码而不是中断），由调用方读 ``code``。
        """
        if retries is None:
            retries = self.retries
        last_err: Exception | None = None
        for attempt in range(retries):
            url = self._build_url(path, params, wbi)
            started = time.monotonic()
            try:
                resp = self.open(url, referer, origin)
                raw = resp.read()
                elapsed = int((time.monotonic() - started) * 1000)
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    return {
                        "http_status": resp.status, "code": None, "message": "非 JSON 响应",
                        "data": None, "elapsed_ms": elapsed, "url": url,
                        "raw_head": raw[:300].decode("utf-8", "replace"),
                    }
                data["http_status"] = resp.status
                data["elapsed_ms"] = elapsed
                data["url"] = url
                code = data.get("code")
                if raise_for_error and code not in (0, None):
                    raise _classify(code, data.get("message", ""), url, resp.status)
                if code in (-352, -412, -403) and attempt < retries - 1:
                    self._img_key = self._sub_key = None
                    time.sleep(2 + attempt * 2 + random.random())
                    continue
                return data
            except urllib.error.HTTPError as e:
                elapsed = int((time.monotonic() - started) * 1000)
                last_err = e
                if e.code in (412, 429, 503) and attempt < retries - 1:
                    self._img_key = self._sub_key = None
                    time.sleep(2 + attempt * 2 + random.random())
                    continue
                return {
                    "http_status": e.code, "code": None,
                    "message": f"HTTP {e.code}", "data": None,
                    "elapsed_ms": elapsed, "url": url,
                }
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(1 + attempt * 2)
                    continue
        return {
            "http_status": 0, "code": None, "message": f"网络失败: {last_err}",
            "data": None, "elapsed_ms": 0, "url": path,
        }

    def _build_url(self, path: str, params: dict | None, wbi: bool) -> str:
        url = API + path
        if params:
            p = dict(params)
            if wbi:
                img_key, sub_key = self.ensure_wbi_keys()
                p = wbi_sign(p, img_key, sub_key)
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: str(v) for k, v in p.items()}
            )
        return url

    # ---------------- WBI / 登录态 ----------------
    def ensure_wbi_keys(self) -> tuple[str, str]:
        if self._img_key and self._sub_key:
            return self._img_key, self._sub_key
        nav = self.nav(force=True)
        wbi_img = (nav.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url") or ""
        sub_url = wbi_img.get("sub_url") or ""
        self._img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        self._sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        if not self._img_key or not self._sub_key:
            raise BiliError(-1, "无法从 nav 接口获取 wbi 密钥")
        return self._img_key, self._sub_key

    def nav(self, force: bool = False) -> dict:
        """``/x/web-interface/nav``：登录态与 wbi 密钥来源。未登录返回 code=-101 但仍带密钥。"""
        if self._nav is not None and not force and time.time() - self._nav_ts < 600:
            return self._nav
        data = self.get_json("/x/web-interface/nav", referer=WWW + "/")
        self._nav = data
        self._nav_ts = time.time()
        return data


def _classify(code: int, message: str, url: str, http_status: int = 0) -> BiliError:
    msg = str(message)
    if code == -101:
        return CookieError(code, msg, url, http_status)
    if code == -352 or "风控" in msg or "risk" in msg.lower():
        return RiskControlError(code, msg, url, http_status)
    return BiliError(code, msg, url, http_status)


# B 站错误码语义（用于阶段 0 的"可区分性"结论）
ERROR_MEANINGS: dict[int, str] = {
    0: "成功",
    -101: "账号未登录 / Cookie 失效（SESSDATA 缺失或过期）",
    -400: "请求参数错误",
    -403: "访问权限不足（无权限）",
    -404: "啥都木有（资源不存在 / 已删除）",
    -412: "请求被拦截（风控）",
    -352: "风控校验失败（-352，需 buvid3/wbi 或降低频率）",
    -799: "请求过于频繁，请稍后再试（限流）",
    -509: "请求过于频繁（限流）",
    -62062: "稿件审核中或不可见",
    62002: "稿件不可见（仅作者可见 / 已删除）",
    62004: "稿件审核中",
}


def describe_code(code: Any) -> str:
    if code is None:
        return "无 JSON code（HTTP 层错误）"
    return ERROR_MEANINGS.get(code, "未收录错误码")
