"""统一错误分类、异常类型与退出码。

契约（阶段 0 第 5.2 节）：
- 三类状态如实区分：``未发现``（列表里就没有）、``invisible``（62002，删除/无权限
  在接口层不可区分，不得写成"权限不足"）、``抓取失败``（网络 / 风控 / HTTP）。
- 遇到风控（-352 / HTTP 412）不绕过，退避重试后仍失败则停止并提示人工处理。
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# 错误分类（ApiResult.error_kind）
# --------------------------------------------------------------------------- #
KIND_OK = "ok"
KIND_AUTH = "auth_invalid"          # -101 未登录 / Cookie 失效
KIND_RISK = "risk_control"          # -352 / HTTP 412
KIND_RATE_LIMIT = "rate_limited"    # -799 / -509 / HTTP 429
KIND_INVISIBLE = "invisible"        # 62002 稿件不可见（删除 / 无权限不可区分）
KIND_DENIED = "denied"              # -403 明确权限不足
KIND_NOT_FOUND = "not_found"        # -404 资源不存在
KIND_PARAM = "bad_request"          # -400 参数错误
KIND_HTTP = "http_error"            # 其他 HTTP 错误
KIND_NETWORK = "network_error"      # 连接 / 超时
KIND_PARSE = "parse_error"          # 响应结构不符合预期
KIND_BLOCKED = "content_blocked"    # MODULE_TYPE_BLOCKED（充电正文被门控）

_KIND_LABEL = {
    KIND_OK: "成功",
    KIND_AUTH: "登录态无效",
    KIND_RISK: "风控拦截",
    KIND_RATE_LIMIT: "限流",
    KIND_INVISIBLE: "稿件不可见（62002）",
    KIND_DENIED: "权限不足",
    KIND_NOT_FOUND: "资源不存在",
    KIND_PARAM: "请求参数错误",
    KIND_HTTP: "HTTP 错误",
    KIND_NETWORK: "网络失败",
    KIND_PARSE: "响应解析失败",
    KIND_BLOCKED: "正文被门控（充电专属，未授权）",
}

#: 需要退避并停止后续大批量请求的分类（契约 5）
FATAL_KINDS = frozenset({KIND_AUTH, KIND_RISK})


def classify(code: Any, http_status: int = 200) -> str:
    """把 ``code`` + HTTP 状态映射为错误分类。"""
    if http_status == 412:
        return KIND_RISK
    if http_status == 429:
        return KIND_RATE_LIMIT
    if http_status and http_status >= 500:
        return KIND_HTTP
    if http_status and http_status >= 400:
        return KIND_HTTP
    if code is None:
        return KIND_NETWORK if http_status == 0 else KIND_HTTP
    if code == 0:
        return KIND_OK
    if code == -101:
        return KIND_AUTH
    if code == -352:
        return KIND_RISK
    if code in (-799, -509):
        return KIND_RATE_LIMIT
    if code == 62002:
        return KIND_INVISIBLE
    if code == -403:
        return KIND_DENIED
    if code == -404:
        return KIND_NOT_FOUND
    if code == -400:
        return KIND_PARAM
    return KIND_HTTP


def describe_kind(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind)


# --------------------------------------------------------------------------- #
# 异常与退出码
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_PARTIAL = 4
EXIT_RISK_STOP = 5


class BiliSubArchiveError(RuntimeError):
    """所有可预期错误的基类。``exit_code`` 决定进程退出码。"""

    exit_code = EXIT_UNEXPECTED


class ConfigError(BiliSubArchiveError):
    """配置 / 命令行参数错误。"""

    exit_code = EXIT_CONFIG


class CredentialError(BiliSubArchiveError):
    """缺少凭据或登录态无效（Cookie 失效与未登录在接口层不可区分）。"""

    exit_code = EXIT_AUTH


class RiskControlStop(BiliSubArchiveError):
    """风控拦截且退避重试无效 —— 停止并提示人工处理，不做绕过。"""

    exit_code = EXIT_RISK_STOP


class OutputError(BiliSubArchiveError):
    """输出目录 / 并发写锁 / 落盘失败。"""

    exit_code = EXIT_CONFIG


def exit_code_for_kind(kind: str) -> int:
    """错误分类 → 进程退出码。

    目标 UID 不存在 / 参数非法属于**配置问题**（2），登录态问题才是 3；
    这样脚本调用方可以区分"改参数"和"换 Cookie"。
    """
    if kind in (KIND_NOT_FOUND, KIND_PARAM, KIND_DENIED, KIND_INVISIBLE):
        return EXIT_CONFIG
    return EXIT_AUTH

