"""OpenAI 兼容 Chat Completions 客户端（阶段 3）。

计划 2.2 把"总结"的拟用方案定为 ``openai`` Python SDK 的 Chat Completions
（可配置 ``base_url`` / 模型 / 密钥）。本模块把这条链路做成**两种等价传输**：

| 传输 | 何时使用 | 说明 |
| --- | --- | --- |
| ``openai-sdk`` | 装了 ``openai`` 且 ``[summary] client = auto/sdk`` | 计划拟用方案，SDK 自带重试与超时 |
| ``urllib`` | 未装 SDK（或显式 ``client = http``） | 标准库直连 ``POST {base_url}/chat/completions``，功能等价 |

两者都满足"OpenAI 兼容接口"这一需求（需求 2「总结配置」），也都能被离线替身替换
（:data:`Transport` 可注入）。**只发送文字稿文本**：不发送 Cookie、不发送媒体文件、
不发送原视频地址（计划第 7 节风险应对）。

错误不抛异常给业务层：失败以 :class:`ChatResult` 的 ``error_kind`` 分类返回，
与 :mod:`bili_sub_archive.bili.client` 的风格一致。分类刻意与 B 站错误区分开
（``llm_*`` 前缀），避免"模型服务 401"被误当成"B 站 Cookie 失效"。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..deps import module_available
from ..errors import KIND_OK

# --------------------------------------------------------------------------- #
# 错误分类（llm_* 前缀，与平台错误分类互不混淆）
# --------------------------------------------------------------------------- #
KIND_LLM_AUTH = "llm_auth_error"          # 401 / 403：密钥无效或无权限
KIND_LLM_RATE_LIMIT = "llm_rate_limited"  # 429：限流，可稍后重试
KIND_LLM_HTTP = "llm_http_error"          # 其他 4xx / 5xx
KIND_LLM_NETWORK = "llm_network_error"    # 连接失败 / DNS
KIND_LLM_TIMEOUT = "llm_timeout"          # 超时
KIND_LLM_INVALID = "llm_response_invalid"  # 响应不是预期结构 / 内容为空

#: 值得重试的分类（与 bili 客户端同策略：退避后重试，仍失败即如实回报）
RETRYABLE_LLM_KINDS = frozenset({KIND_LLM_RATE_LIMIT, KIND_LLM_HTTP,
                                 KIND_LLM_NETWORK, KIND_LLM_TIMEOUT})

LLM_KIND_LABEL = {
    KIND_LLM_AUTH: "模型服务鉴权失败",
    KIND_LLM_RATE_LIMIT: "模型服务限流",
    KIND_LLM_HTTP: "模型服务 HTTP 错误",
    KIND_LLM_NETWORK: "模型服务连接失败",
    KIND_LLM_TIMEOUT: "模型服务超时",
    KIND_LLM_INVALID: "模型返回内容不可用",
}

#: 传输函数签名：``(url, headers, body, timeout) -> (status, headers, body)``
Transport = Callable[[str, dict, bytes, float], tuple[int, dict, bytes]]

DEFAULT_CHAT_PATH = "/chat/completions"


def llm_kind_label(kind: str) -> str:
    return LLM_KIND_LABEL.get(kind, kind)


def classify_http(status: int) -> str:
    """HTTP 状态码 → llm 错误分类。"""
    if status == 200:
        return KIND_OK
    if status in (401, 403):
        return KIND_LLM_AUTH
    if status == 429:
        return KIND_LLM_RATE_LIMIT
    return KIND_LLM_HTTP


def classify_exception(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return KIND_LLM_TIMEOUT
    if isinstance(exc, urllib.error.HTTPError):  # pragma: no cover - 传输层已捕获
        return classify_http(int(getattr(exc, "code", 0) or 0))
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return KIND_LLM_TIMEOUT
        return KIND_LLM_NETWORK
    return KIND_LLM_NETWORK


# --------------------------------------------------------------------------- #
# 结果与协议
# --------------------------------------------------------------------------- #
@dataclass
class ChatResult:
    """一次 Chat Completions 调用的结果与分类。"""

    ok: bool = False
    text: str = ""
    model: str = ""
    error_kind: str = KIND_OK
    http_status: int = 0
    code: Any = None
    message: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 0
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def brief(self) -> str:
        return (f"model={self.model} http={self.http_status} kind={self.error_kind} "
                f"msg={self.message[:160]}")


class ChatClient(Protocol):
    """总结步骤依赖的最小协议（离线测试注入替身即可，不必联网）。"""

    name: str
    model: str
    endpoint_host: str

    def complete(self, messages: list[dict[str, str]]) -> ChatResult:  # pragma: no cover
        ...


class LlmUnavailable(RuntimeError):
    """显式要求 SDK 传输但 ``openai`` 不可导入。"""


# --------------------------------------------------------------------------- #
# 传输
# --------------------------------------------------------------------------- #
def default_transport(url: str, headers: dict, body: bytes, timeout: float):
    """标准库 POST；HTTP 错误码也回传 body（供读取服务端错误说明）。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 用户配置的端点
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers or {}), exc.read()


def chat_endpoint(base_url: str) -> str:
    """``base_url`` → ``chat/completions`` 完整地址。

    ``base_url`` 已经写到 ``/chat/completions`` 时原样使用（有些用户直接填全路径）。
    """
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith(DEFAULT_CHAT_PATH):
        return raw
    return raw + DEFAULT_CHAT_PATH


def endpoint_host(base_url: str) -> str:
    """只保留主机名（端口可有），**不落盘完整地址** —— 部分代理把令牌放在 query 里。"""
    try:
        parts = urllib.parse.urlsplit(str(base_url or "").strip())
    except ValueError:  # pragma: no cover - 极端畸形输入
        return ""
    return parts.netloc or ""


def _extract_text(payload: dict) -> tuple[str, str]:
    """从响应体取出正文与 finish_reason（容忍多种 OpenAI 兼容形状）。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    finish = str(first.get("finish_reason") or "")
    message = first.get("message")
    text = ""
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # 有些兼容实现返回 [{"type": "text", "text": "..."}] 形状
            text = "".join(str(part.get("text") or "") for part in content
                           if isinstance(part, dict))
    if not text:
        legacy = first.get("text")
        if isinstance(legacy, str):
            text = legacy
    return text.strip(), finish


def _usage(payload: dict) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    try:
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):  # pragma: no cover - 服务端给了奇怪的值
        return 0, 0


def _empty_content_hint(payload: dict, finish: str) -> str:
    """空正文的原因提示。

    实测（deepseek-flash 等推理模型）：服务端先输出 ``reasoning_content``，思考 token 也算进
    ``max_tokens``；预算被思考吃光时 ``content`` 为空、``finish_reason=length``。只说
    "模型返回内容为空"会让人以为是服务端坏了，这里把可操作的下一步写清楚。
    """
    message = ((payload.get("choices") or [{}])[0] or {}).get("message")
    reasoning = ""
    if isinstance(message, dict):
        reasoning = str(message.get("reasoning_content") or "")
    if finish == "length":
        return ("模型返回内容为空：输出被 max_tokens 截断（finish_reason=length）。"
                "推理模型会先把预算花在 reasoning_content（思考）上，请提高 "
                "[summary] max_tokens（如 8192）或改用非推理模型后 retry")
    if reasoning:
        return ("模型返回内容为空：只拿到 reasoning_content（思考过程）而没有正文；"
                "多为推理模型或服务端差异，可提高 [summary] max_tokens 后 retry")
    return "模型返回内容为空"


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str) and err:
            return err[:300]
        if payload.get("message"):
            return str(payload["message"])[:300]
    return fallback


# --------------------------------------------------------------------------- #
# 标准库实现
# --------------------------------------------------------------------------- #
class HttpChatClient:
    """标准库 ``urllib`` 直连 OpenAI 兼容端点（不依赖任何第三方库）。"""

    name = "urllib"

    def __init__(self, *, base_url: str, model: str, api_key: str = "", timeout: float = 120.0,
                 retries: int = 2, temperature: float = 0.2, max_tokens: int = 2048,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 logger=None):
        self.base_url = str(base_url or "").strip()
        self.endpoint = chat_endpoint(self.base_url)
        self.endpoint_host = endpoint_host(self.base_url)
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "")
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self._transport = transport or default_transport
        self._sleep = sleep
        self.logger = logger
        #: 统计（写进运行摘要，便于核对"调用了几次模型"）
        self.calls = 0
        self.retries_used = 0

    # -- 内部 -- #
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: list[dict[str, str]]) -> bytes:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.max_tokens > 0:
            body["max_tokens"] = self.max_tokens
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)

    # -- 协议 -- #
    def complete(self, messages: list[dict[str, str]]) -> ChatResult:
        if not self.endpoint:
            return ChatResult(error_kind=KIND_LLM_INVALID,
                              message="未配置 base_url，无法定位 chat/completions 端点")
        if not self.model:
            return ChatResult(error_kind=KIND_LLM_INVALID, message="未配置模型名")
        body = self._payload(messages)
        headers = self._headers()
        result = ChatResult(model=self.model)
        for attempt in range(1, self.retries + 1):
            result.attempts = attempt
            self.calls += 1
            try:
                status, _resp_headers, raw = self._transport(
                    self.endpoint, headers, body, self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - 传输层异常统一分类，不炸主流程
                kind = classify_exception(exc)
                result.error_kind = kind
                result.message = f"{type(exc).__name__}: {exc}"[:300]
                if attempt < self.retries:
                    self._retry_wait(attempt, kind)
                    continue
                return result

            result.http_status = int(status or 0)
            if status == 200:
                parsed = self._parse(raw, result)
                return parsed

            kind = classify_http(int(status or 0))
            payload = _decode_json(raw)
            result.error_kind = kind
            result.message = _error_message(
                payload, f"HTTP {status}：{raw[:200].decode('utf-8', 'replace') if raw else ''}"
            )
            if kind in RETRYABLE_LLM_KINDS and attempt < self.retries:
                self._retry_wait(attempt, kind)
                continue
            return result

        return result

    def _retry_wait(self, attempt: int, kind: str) -> None:
        self.retries_used += 1
        delay = min(2.0 * attempt, 8.0)
        self._log("debug", f"[LLM] {llm_kind_label(kind)}，{delay:.0f}s 后重试"
                           f"（第 {attempt + 1}/{self.retries} 次）")
        self._sleep(delay)

    def _parse(self, raw: bytes, result: ChatResult) -> ChatResult:
        payload = _decode_json(raw)
        if not isinstance(payload, dict):
            result.error_kind = KIND_LLM_INVALID
            result.message = "响应不是 JSON 对象"
            return result
        text, finish = _extract_text(payload)
        result.prompt_tokens, result.completion_tokens = _usage(payload)
        result.model = str(payload.get("model") or self.model)
        result.finish_reason = finish
        if not text:
            result.error_kind = KIND_LLM_INVALID
            result.message = _error_message(payload, _empty_content_hint(payload, finish))
            return result
        result.ok = True
        result.text = text
        result.error_kind = KIND_OK
        return result


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads((raw or b"").decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# openai SDK 实现（计划 2.2 的拟用方案）
# --------------------------------------------------------------------------- #
class SdkChatClient:
    """``openai`` Python SDK 适配：把 SDK 异常映射回统一的 llm 错误分类。"""

    name = "openai-sdk"

    def __init__(self, *, base_url: str, model: str, api_key: str = "", timeout: float = 120.0,
                 retries: int = 2, temperature: float = 0.2, max_tokens: int = 2048,
                 logger=None):
        import openai  # 惰性导入：没装 openai 也能 import bili_sub_archive

        self.model = str(model or "").strip()
        self.base_url = str(base_url or "").strip()
        self.endpoint_host = endpoint_host(self.base_url)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.logger = logger
        self.calls = 0
        self.retries_used = 0
        kwargs: dict[str, Any] = {
            "api_key": api_key or "bili_sub_archive-no-key",
            "timeout": float(timeout),
            "max_retries": max(0, int(retries) - 1),
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = openai.OpenAI(**kwargs)

    def complete(self, messages: list[dict[str, str]]) -> ChatResult:
        result = ChatResult(model=self.model)
        self.calls += 1
        try:
            resp = self._client.chat.completions.create(
                model=self.model, messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens or None,
            )
        except Exception as exc:  # noqa: BLE001 - SDK 异常种类多，按状态码/类型归类
            result.error_kind = _classify_sdk_exception(exc)
            result.http_status = int(getattr(exc, "status_code", 0) or 0)
            result.message = f"{type(exc).__name__}: {exc}"[:300]
            return result

        result.attempts = 1
        choice = (getattr(resp, "choices", None) or [None])[0]
        text = ""
        if choice is not None:
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):  # pragma: no cover - 兼容实现
                text = "".join(str(getattr(p, "text", "") or "") for p in content)
            result.finish_reason = str(getattr(choice, "finish_reason", "") or "")
        usage = getattr(resp, "usage", None)
        if usage is not None:
            result.prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            result.completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        result.model = str(getattr(resp, "model", "") or self.model)
        text = text.strip()
        if not text:
            result.error_kind = KIND_LLM_INVALID
            result.message = "模型返回内容为空"
            return result
        result.ok = True
        result.text = text
        return result


def _classify_sdk_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    status = int(getattr(exc, "status_code", 0) or 0)
    if status:
        return classify_http(status)
    if "Timeout" in name or isinstance(exc, (TimeoutError, socket.timeout)):
        return KIND_LLM_TIMEOUT
    if "Connection" in name:
        return KIND_LLM_NETWORK
    if "Authentication" in name or "PermissionDenied" in name:
        return KIND_LLM_AUTH
    if "RateLimit" in name:
        return KIND_LLM_RATE_LIMIT
    return KIND_LLM_NETWORK


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
#: ``[summary] client`` 的合法取值
CLIENT_MODES = ("auto", "sdk", "http")


def build_chat_client(config, api_key: str = "", *, transport: Transport | None = None,
                      logger=None) -> ChatClient:
    """按配置装配客户端。

    - 注入 ``transport`` 时一律走标准库实现（离线测试用）；
    - ``client = auto``：装了 ``openai`` 用 SDK，否则标准库直连；
    - ``client = sdk``：强制 SDK，缺失即 :class:`LlmUnavailable`；
    - ``client = http``：强制标准库直连。
    """
    mode = str(getattr(config, "summary_client", "auto") or "auto").strip().lower()
    kwargs = {
        "base_url": getattr(config, "summary_base_url", ""),
        "model": getattr(config, "summary_model", ""),
        "api_key": api_key,
        "timeout": float(getattr(config, "summary_timeout_seconds", 120.0)),
        "retries": int(getattr(config, "summary_retries", 2)),
        "temperature": float(getattr(config, "summary_temperature", 0.2)),
        "max_tokens": int(getattr(config, "summary_max_tokens", 2048)),
        "logger": logger,
    }
    if transport is not None or mode == "http":
        return HttpChatClient(transport=transport, **kwargs)
    if mode == "sdk" and not module_available("openai"):
        raise LlmUnavailable(
            "配置要求使用 openai SDK（[summary] client = \"sdk\"），但当前环境未安装："
            "pip install \"bili-sub-archive[summary]\"；或把 client 改为 \"http\" 用标准库直连"
        )
    if module_available("openai"):
        try:
            return SdkChatClient(**kwargs)
        except Exception as exc:  # noqa: BLE001 - SDK 初始化失败时降级而不是崩掉归档
            if mode == "sdk":
                raise LlmUnavailable(f"openai SDK 初始化失败：{exc}") from exc
            if logger is not None:
                logger.warning(f"[LLM] openai SDK 初始化失败（{exc}），改用标准库直连")
    return HttpChatClient(**kwargs)


__all__ = [
    "CLIENT_MODES",
    "ChatClient",
    "ChatResult",
    "HttpChatClient",
    "KIND_LLM_AUTH",
    "KIND_LLM_HTTP",
    "KIND_LLM_INVALID",
    "KIND_LLM_NETWORK",
    "KIND_LLM_RATE_LIMIT",
    "KIND_LLM_TIMEOUT",
    "LLM_KIND_LABEL",
    "LlmUnavailable",
    "RETRYABLE_LLM_KINDS",
    "SdkChatClient",
    "Transport",
    "build_chat_client",
    "chat_endpoint",
    "classify_exception",
    "classify_http",
    "default_transport",
    "endpoint_host",
    "llm_kind_label",
]
