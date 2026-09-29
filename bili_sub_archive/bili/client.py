"""B 站接口适配层：HTTP 传输 + WBI 签名 + 限速/退避 + 错误分类。

阶段 0 第 5.1 节接口白名单与 5.2 节契约的传输层实现：

- 只用标准库 ``urllib``（不引入第三方 B 站封装库，选型见阶段 0 第 5.3 节）；
- 每次请求前限速（间隔 + 随机抖动），``-352`` / ``HTTP 412`` 时清空 wbi 密钥缓存
  并指数退避重试；重试仍失败则如实回报 ``risk_control``，**不做验证码绕过或账号轮换**；
- 传输层不抛异常给业务层：失败以 :class:`ApiResult` 的形式返回错误分类。
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import KIND_HTTP, KIND_NETWORK, KIND_OK, KIND_PARSE, KIND_RISK, classify
from ..paths import atomic_write_bytes
from .wbi import sign as wbi_sign

API_ROOT = "https://api.bilibili.com"
WWW_ROOT = "https://www.bilibili.com"

#: 传输函数签名：``(url, headers, timeout) -> (status, headers, body)``
Transport = Callable[[str, dict, float], tuple[int, dict, bytes]]


@dataclass
class ApiResult:
    """一次接口调用的结果与分类。业务层只读这个对象，不碰异常。"""

    path: str = ""
    params: dict = field(default_factory=dict)
    wbi: bool = False
    http_status: int = 0
    code: Any = None
    message: str = ""
    data: Any = None
    url: str = ""
    error_kind: str = KIND_OK
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.error_kind == KIND_OK and self.code == 0

    @property
    def kind_label(self) -> str:
        from ..errors import describe_kind

        return describe_kind(self.error_kind)

    def brief(self) -> str:
        return (f"{self.path} http={self.http_status} code={self.code} "
                f"kind={self.error_kind} msg={self.message[:120]}")


@dataclass
class DownloadResult:
    ok: bool = False
    path: Path | None = None
    bytes_written: int = 0
    content_type: str = ""
    error_kind: str = KIND_OK
    message: str = ""
    attempts: int = 0
    url: str = ""


def upgrade_url(url: str) -> str:
    """阶段 0 第 3.7 节：动态原图 URL 返回 ``http://``，统一升级为 https。"""
    raw = str(url or "").strip()
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("http://"):
        return "https://" + raw[len("http://"):]
    return raw


def default_transport(url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 白名单接口
        body = resp.read()
        return resp.status, dict(resp.headers), body


class HttpClient:
    """限速 + WBI + 退避的薄 HTTP 客户端。"""

    def __init__(
        self,
        cookie: str = "",
        *,
        interval: float = 1.2,
        timeout: float = 20.0,
        retries: int = 3,
        user_agent: str = "",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger=None,
    ):
        from ..config import DEFAULT_USER_AGENT

        self.cookie = str(cookie or "")
        self.interval = max(0.0, float(interval))
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._transport = transport or default_transport
        self._sleep = sleep
        self.logger = logger
        self._lock = threading.Lock()
        self._last = 0.0
        self._img_key: str | None = None
        self._sub_key: str | None = None
        self._nav_cache: dict | None = None
        self._nav_ts = 0.0
        # 统计
        self.requests = 0
        self.retries_used = 0
        self.bytes_downloaded = 0
        self.risk_events = 0
        self.consecutive_risk = 0

    # ---------------- 基础 ---------------- #
    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last + self.interval + random.random() * self.interval * 0.5 - now
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()

    def _headers(self, referer: str, origin: str | None = None,
                 extra: dict | None = None) -> dict:
        hdrs = {
            "User-Agent": self.user_agent,
            "Referer": referer or (WWW_ROOT + "/"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if origin:
            hdrs["Origin"] = origin
        if extra:
            hdrs.update(extra)
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        return hdrs

    def get_json(
        self,
        path: str,
        params: dict | None = None,
        *,
        wbi: bool = False,
        referer: str = "",
        origin: str | None = None,
        retries: int | None = None,
        track_risk: bool = True,
    ) -> ApiResult:
        """请求 JSON 接口，返回 :class:`ApiResult`（永不抛网络异常）。

        ``track_risk=False`` 用于内部辅助请求（如取 wbi 密钥的 ``nav``）：
        它们的成功**不应**把"连续风控计数"清零（清空密钥后重取 nav 会成功，
        但业务请求仍处于风控状态）。
        """
        attempts_allowed = max(1, int(retries if retries is not None else self.retries))
        last = ApiResult(path=path, params=dict(params or {}), wbi=wbi)
        for attempt in range(1, attempts_allowed + 1):
            try:
                url = self._build_url(path, params, wbi)
            except Exception as exc:  # wbi 密钥取不到等 → 分类返回，不抛给业务层
                last.error_kind = KIND_PARSE
                last.message = f"构建请求失败：{type(exc).__name__}: {exc}"
                return last
            last.url = url
            last.attempts = attempt
            self._throttle()
            self.requests += 1
            try:
                status, _headers, body = self._transport(url, self._headers(referer, origin), self.timeout)
            except urllib.error.HTTPError as exc:  # pragma: no cover - 依赖真实网络
                status = exc.code
                body = b""
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last.error_kind = KIND_NETWORK
                last.message = f"网络失败：{type(exc).__name__}: {exc}"
                if attempt < attempts_allowed:
                    self.retries_used += 1
                    self._sleep(1.0 + attempt * 2)
                    continue
                return last

            last.http_status = int(status)

            if status in (412, 429, 503):
                last.error_kind = classify(None, status)
                last.message = f"HTTP {status}"
                if last.error_kind == KIND_RISK:
                    self.risk_events += 1
                    self.consecutive_risk += 1
                    self._img_key = self._sub_key = None
                if attempt < attempts_allowed:
                    self.retries_used += 1
                    self._sleep(2 + attempt * 2 + random.random())
                    continue
                return last

            if status >= 400:
                last.error_kind = KIND_HTTP
                last.message = f"HTTP {status}"
                if attempt < attempts_allowed and status >= 500:
                    self.retries_used += 1
                    self._sleep(1 + attempt * 2)
                    continue
                return last

            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                last.error_kind = KIND_PARSE
                last.message = "非 JSON 响应"
                last.data = {"raw_head": body[:300].decode("utf-8", "replace")}
                return last

            if not isinstance(payload, dict):
                last.error_kind = KIND_PARSE
                last.message = f"响应不是对象：{type(payload).__name__}"
                return last

            last.code = payload.get("code")
            last.message = str(payload.get("message") or "")
            last.error_kind = classify(last.code, status)
            last.data = payload.get("data")
            if last.error_kind == KIND_RISK:
                # 契约 5：清空 wbi 密钥缓存后指数退避重试
                if track_risk:
                    self.risk_events += 1
                    self.consecutive_risk += 1
                self._img_key = self._sub_key = None
                if attempt < attempts_allowed:
                    self.retries_used += 1
                    self._sleep(2 + attempt * 2 + random.random())
                    continue
            if last.error_kind == KIND_OK and track_risk:
                self.consecutive_risk = 0
            return last
        return last

    def download(
        self,
        url: str,
        dest: Path,
        *,
        referer: str = "",
        retries: int | None = None,
    ) -> DownloadResult:
        """下载图片等静态资源：临时文件 + 原子替换，失败不留半截文件。"""
        target_url = upgrade_url(url)
        result = DownloadResult(url=target_url)
        if not target_url:
            result.error_kind = KIND_PARSE
            result.message = "空 URL"
            return result
        attempts_allowed = max(1, int(retries if retries is not None else self.retries))
        for attempt in range(1, attempts_allowed + 1):
            result.attempts = attempt
            self._throttle()
            self.requests += 1
            try:
                # 图片 CDN 不校验 Cookie（阶段 0 第 3.7 节），但带上 Referer 更稳
                status, headers, body = self._transport(
                    target_url, self._headers(referer or WWW_ROOT + "/", None,
                                              {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}),
                    self.timeout,
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                result.error_kind = KIND_NETWORK
                result.message = f"{type(exc).__name__}: {exc}"
                if attempt < attempts_allowed:
                    self.retries_used += 1
                    self._sleep(1 + attempt)
                    continue
                return result
            if status >= 400 or not body:
                result.error_kind = classify(None, status) if status >= 400 else KIND_NETWORK
                result.message = f"HTTP {status}" if status >= 400 else "空响应体"
                if attempt < attempts_allowed and status in (412, 429, 500, 502, 503, 504):
                    self.retries_used += 1
                    self._sleep(1 + attempt)
                    continue
                return result
            atomic_write_bytes(Path(dest), body)
            result.ok = True
            result.path = Path(dest)
            result.bytes_written = len(body)
            result.content_type = str(headers.get("Content-Type") or "").split(";")[0].strip()
            self.bytes_downloaded += len(body)
            return result
        return result

    # ---------------- WBI ---------------- #
    def _build_url(self, path: str, params: dict | None, wbi: bool) -> str:
        url = API_ROOT + path
        if params:
            payload = dict(params)
            if wbi:
                img_key, sub_key = self.ensure_wbi_keys()
                payload = wbi_sign(payload, img_key, sub_key)
            query = urllib.parse.urlencode({k: str(v) for k, v in payload.items()})
            url += ("&" if "?" in url else "?") + query
        return url

    def ensure_wbi_keys(self) -> tuple[str, str]:
        if self._img_key and self._sub_key:
            return self._img_key, self._sub_key
        nav = self.nav(force=True)
        data = nav.data if isinstance(nav.data, dict) else {}
        wbi_img = data.get("wbi_img") if isinstance(data.get("wbi_img"), dict) else {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        self._img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        self._sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        if not self._img_key or not self._sub_key:
            raise RuntimeError("无法从 /x/web-interface/nav 获取 wbi 密钥")
        return self._img_key, self._sub_key

    def nav(self, force: bool = False) -> ApiResult:
        """``/x/web-interface/nav``：登录态与 wbi 密钥来源（未登录也下发密钥）。"""
        if self._nav_cache is not None and not force and time.time() - self._nav_ts < 600:
            return self._nav_cache
        result = self.get_json("/x/web-interface/nav", referer=WWW_ROOT + "/",
                               track_risk=False)
        if result.ok or result.code == -101:
            self._nav_cache = result
            self._nav_ts = time.time()
        return result

