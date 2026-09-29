"""凭据加载与脱敏。

凭据来源优先级（先到先用）：
1. 环境变量 ``SUBVIDEO_COOKIE`` / ``BILI_COOKIE``，``SUBVIDEO_UID`` / ``BILI_UID``
2. ``config.local.toml``（未入库，建议加入 .gitignore）::

       [bilibili]
       uid = 123456
       cookie = "SESSDATA=...; bili_jct=..."

3. ``require.txt``（用户手工放置的键值对文本，见 ``load_require_txt``）

本模块的硬性约束：**任何日志、证据文件、报告都不得出现 Cookie 原文**。
所有写盘/打印路径必须先过 :func:`Redactor.redact`。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REDACTED = "[REDACTED]"

# 视为凭据的 cookie 字段名（其余字段如 CURRENT_QUALITY 不是秘密，但一并脱敏更安全）
_SECRET_KEYS = {
    "sessdata", "bili_jct", "dedeuserid", "dedeuserid__ckmd5", "buvid3", "buvid4",
    "buvid_fp", "b_lsid", "bili_ticket", "sid", "_uuid", "rpdid", "b_nut",
    "bp_t_offset_", "bili_ticket_expires",
}

# require.txt 里可识别的键名（大小写不敏感）
_TXT_KEYS = {"cookie": "cookie", "mid": "uid", "uid": "uid", "sessdata": "sessdata"}

_VALUE_RE = re.compile(r"^\s*([^=;\s]+)\s*=\s*(.*?)\s*$")


@dataclass
class Credentials:
    """一次验证所需的账号信息。``cookie`` 绝不外泄。"""

    cookie: str = ""
    uid: int = 0
    source: str = "none"
    # 非秘密的 cookie 字段名清单，用于报告"Cookie 里有哪些字段"
    cookie_field_names: list[str] = field(default_factory=list)

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie.strip())


def _cookie_field_names(cookie: str) -> list[str]:
    names = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part:
            continue
        name = part.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def load_require_txt(path: Path) -> tuple[str, int]:
    """解析 ``require.txt`` 这类"键名一行、值一行"的文本。

    实际样例::

        cookie
        buvid4=...; SESSDATA=...; bili_jct=...
        <空行>
        mid
        1039025435
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    cookie = ""
    uid = 0
    i = 0
    while i < len(lines):
        key = lines[i].strip().rstrip(":").lower()
        if key in _TXT_KEYS:
            # 值可能在同行（key=value）或下一非空行
            same_line = ""
            if "=" in lines[i] and not lines[i].strip().lower().startswith(key + "="):
                same_line = lines[i].split("=", 1)[1].strip()
            if not same_line:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                same_line = lines[j].strip() if j < len(lines) else ""
                i = j
            target = _TXT_KEYS[key]
            if target == "cookie" and "=" in same_line:
                cookie = same_line
            elif target == "uid" and same_line.isdigit():
                uid = int(same_line)
            i += 1
            continue
        # 兼容 key=value 形式
        m = _VALUE_RE.match(lines[i])
        if m and m.group(1).strip().lower() in _TXT_KEYS:
            target = _TXT_KEYS[m.group(1).strip().lower()]
            val = m.group(2)
            if target == "cookie":
                cookie = val
            elif target == "uid" and val.isdigit():
                uid = int(val)
        i += 1
    return cookie, uid


def _load_toml(path: Path) -> tuple[str, int]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return "", 0
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return "", 0
    section = data.get("bilibili") or data.get("account") or data
    cookie = str(section.get("cookie") or "")
    uid_raw = section.get("uid") or section.get("mid") or 0
    try:
        uid = int(uid_raw)
    except (TypeError, ValueError):
        uid = 0
    return cookie, uid


def load_credentials(root: Path | None = None, uid_override: int = 0) -> Credentials:
    """按优先级装配凭据。缺失 Cookie 时返回空 Cookie（公开范围验证仍可进行）。"""
    root = root or Path.cwd()

    cookie = os.environ.get("SUBVIDEO_COOKIE") or os.environ.get("BILI_COOKIE") or ""
    uid_raw = os.environ.get("SUBVIDEO_UID") or os.environ.get("BILI_UID") or "0"
    source = "env" if cookie else "none"
    try:
        uid = int(uid_raw)
    except ValueError:
        uid = 0

    toml_path = root / "config.local.toml"
    if not cookie and toml_path.exists():
        cookie, t_uid = _load_toml(toml_path)
        if cookie:
            source = "config.local.toml"
        uid = uid or t_uid

    txt_path = root / "require.txt"
    if not cookie and txt_path.exists():
        cookie, x_uid = load_require_txt(txt_path)
        if cookie:
            source = "require.txt"
        uid = uid or x_uid

    if uid_override:
        uid = uid_override

    return Credentials(
        cookie=cookie,
        uid=uid,
        source=source,
        cookie_field_names=_cookie_field_names(cookie),
    )


class Redactor:
    """把凭据值从任意文本/对象中抹掉。"""

    def __init__(self, secrets: list[str]):
        # 长值优先替换，避免短值先把长值切断
        self._values = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    @classmethod
    def from_cookie(cls, cookie: str) -> "Redactor":
        values: list[str] = []
        if cookie:
            values.append(cookie)
            for part in cookie.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, val = part.split("=", 1)
                if val.strip():
                    values.append(val.strip())
        return cls(values)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for val in self._values:
            if val in text:
                text = text.replace(val, REDACTED)
        return text

    def redact_obj(self, obj):
        """递归脱敏 dict/list/str。"""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj
