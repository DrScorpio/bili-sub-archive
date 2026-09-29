"""凭据脱敏：任何落盘、打印、日志路径都必须先过 :class:`Redactor`。

沿用阶段 0 ``docs/archive/tools/stage0/credstore.py`` 的实现并补两道防线：

1. :meth:`Redactor.redact_obj` 递归脱敏任意 JSON 结构；
2. :func:`scan_for_secrets` 在写出产物后回扫，发现凭据值即报错（阶段 0 探针同款校验）。

Cookie 字段**名**（如 ``SESSDATA``）允许出现在报告里，字段**值**绝不允许。
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

#: 视为凭据的 cookie / 密钥字段名（大小写不敏感）
SECRET_KEYS = frozenset(
    {
        "sessdata", "bili_jct", "dedeuserid", "dedeuserid__ckmd5", "buvid3", "buvid4",
        "buvid_fp", "b_lsid", "bili_ticket", "sid", "_uuid", "rpdid", "b_nut",
        "bp_t_offset_", "bili_ticket_expires", "api_key", "apikey", "token",
        "authorization", "password", "secret",
    }
)

#: 值长度达到该阈值才纳入脱敏（避免把 "0"、"1" 这类短值全局替换掉）
MIN_SECRET_LEN = 6


def cookie_field_names(cookie: str) -> list[str]:
    """取出 Cookie 的字段名清单（**不含值**），用于报告"Cookie 里有哪些字段"。"""
    names: list[str] = []
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def secret_values(cookie: str, extra: list[str] | None = None) -> list[str]:
    """从 Cookie（整串 + 各字段值）与额外密钥中收集需要脱敏的值。"""
    values: list[str] = []
    cookie = str(cookie or "")
    if cookie:
        values.append(cookie)
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            _name, val = part.split("=", 1)
            if val.strip():
                values.append(val.strip())
    for item in extra or []:
        if item:
            values.append(str(item))
    return values


class Redactor:
    """把凭据值从任意文本 / 对象中抹掉。"""

    def __init__(self, secrets: list[str] | None = None):
        # 长值优先替换，避免短值先把长值切断
        self._values = sorted(
            {s for s in (secrets or []) if s and len(s) >= MIN_SECRET_LEN},
            key=len,
            reverse=True,
        )

    @classmethod
    def from_cookie(cls, cookie: str, extra: list[str] | None = None) -> "Redactor":
        return cls(secret_values(cookie, extra))

    @property
    def values(self) -> list[str]:
        return list(self._values)

    def redact(self, text: str) -> str:
        if not text:
            return text
        out = str(text)
        for val in self._values:
            if val in out:
                out = out.replace(val, REDACTED)
        return out

    def redact_obj(self, obj):
        """递归脱敏 dict / list / str。"""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {
                self.redact(k) if isinstance(k, str) else k: self.redact_obj(v)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj

    def contains_secret(self, text: str) -> str | None:
        """返回命中的凭据值（供自检使用），未命中返回 None。"""
        blob = str(text)
        for val in self._values:
            if val in blob:
                return val
        return None


# 兜底正则：任何 ``key=value`` 形式里的已知敏感键，值一律替换
_SECRET_LINE_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in sorted(SECRET_KEYS)) + r")\b(\s*[=:]\s*)([^\s;,\"']+)"
)


def redact_known_keys(text: str) -> str:
    """对尚未收集到值的场景，按字段名兜底脱敏。"""
    return _SECRET_LINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", str(text))


def scan_for_secrets(root, redactor: Redactor, suffixes: tuple[str, ...] = (".json", ".md", ".txt", ".mmd", ".srt", ".toml")):
    """扫描目录下全部文本产物，返回 ``[(path, secret), ...]``。

    阶段 0 探针的"产物中出现凭据值即报错退出"校验，阶段 1 收进正式工具，
    由 ``bili_sub_archive check --scan-output`` 与测试调用。
    """
    from pathlib import Path

    hits: list[tuple[Path, str]] = []
    base = Path(root)
    if not base.exists():
        return hits
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = redactor.contains_secret(text)
        if hit:
            hits.append((path, hit))
    return hits
