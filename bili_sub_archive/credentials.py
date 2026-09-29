"""凭据加载（Cookie / 目标 UID / LLM 密钥）。

优先级（先到先用）与阶段 0 一致，另加 LLM 密钥：

1. 环境变量 ``BSA_COOKIE`` / ``SUBVIDEO_COOKIE``（旧名，兼容）/ ``BILI_COOKIE``，
   ``BSA_UID`` / ``SUBVIDEO_UID`` / ``BILI_UID``，
   ``BSA_LLM_API_KEY`` / ``SUBVIDEO_LLM_API_KEY`` / ``OPENAI_API_KEY``
2. ``config.local.toml`` / ``config.toml``（未入库的本地配置优先）
3. ``require.txt``（用户手工放置的"键名一行、值一行"文本）

硬性约束：凭据值不得写入任何产物、日志或异常输出；本模块只回传值，
落盘路径一律先过 :class:`bili_sub_archive.redact.Redactor`。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .redact import cookie_field_names

#: 按序取第一个非空值。``SUBVIDEO_*`` 是改名前（旧项目名 SubVideo）的前缀，
#: 保留一个版本作兜底，避免用户已有的 shell 配置立刻失效。
_ENV_COOKIE = ("BSA_COOKIE", "SUBVIDEO_COOKIE", "BILI_COOKIE")
_ENV_UID = ("BSA_UID", "SUBVIDEO_UID", "BILI_UID")
_ENV_LLM_KEY = ("BSA_LLM_API_KEY", "SUBVIDEO_LLM_API_KEY", "OPENAI_API_KEY")

_TXT_KEYS = {"cookie": "cookie", "mid": "uid", "uid": "uid", "sessdata": "sessdata"}
_VALUE_RE = re.compile(r"^\s*([^=;\s]+)\s*=\s*(.*?)\s*$")


@dataclass
class Credentials:
    """一次运行所需的账号信息。``cookie`` 绝不外泄。"""

    cookie: str = ""
    uid: int = 0
    source: str = "none"
    llm_api_key: str = ""
    llm_key_source: str = "none"
    cookie_field_names: list[str] = field(default_factory=list)

    @property
    def has_cookie(self) -> bool:
        return bool(str(self.cookie).strip())

    @property
    def has_llm_key(self) -> bool:
        return bool(str(self.llm_api_key).strip())

    def secrets(self) -> list[str]:
        return [self.cookie, self.llm_api_key]


def load_require_txt(path: Path) -> tuple[str, int]:
    """解析 ``require.txt`` 这类"键名一行、值一行"的文本。"""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    cookie = ""
    uid = 0
    i = 0
    while i < len(lines):
        key = lines[i].strip().rstrip(":").lower()
        if key in _TXT_KEYS:
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


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_credentials(
    root: Path | None = None,
    uid_override: int = 0,
    toml_sections: list[dict] | None = None,
) -> Credentials:
    """按优先级装配凭据。

    ``toml_sections`` 由 :mod:`bili_sub_archive.config` 传入（已按"local 覆盖 base"合并
    后的 ``[account]`` / ``[summary]`` 段），避免本模块重复解析 TOML。
    """
    root = Path(root or Path.cwd())
    sections = [s or {} for s in (toml_sections or [])]

    cookie = ""
    source = "none"
    for env_name in _ENV_COOKIE:
        value = os.environ.get(env_name)
        if value and value.strip():
            cookie = value.strip()
            source = f"env:{env_name}"
            break

    llm_key = ""
    llm_source = "none"
    for env_name in _ENV_LLM_KEY:
        value = os.environ.get(env_name)
        if value and value.strip():
            llm_key = value.strip()
            llm_source = f"env:{env_name}"
            break

    uid = 0
    for env_name in _ENV_UID:
        value = os.environ.get(env_name)
        if value and str(value).strip().isdigit():
            uid = int(str(value).strip())
            break

    for section in sections:
        if not cookie:
            candidate = str(section.get("cookie") or "").strip()
            if candidate:
                cookie = candidate
                source = source if source != "none" else "config"
        if not uid:
            uid = _as_int(section.get("uid") or section.get("mid") or 0)
        if not llm_key:
            candidate = str(
                section.get("api_key") or section.get("llm_api_key") or ""
            ).strip()
            if candidate:
                llm_key = candidate
                llm_source = llm_source if llm_source != "none" else "config"

    if not cookie:
        txt_path = root / "require.txt"
        if txt_path.exists():
            cookie, x_uid = load_require_txt(txt_path)
            if cookie:
                source = "require.txt"
            uid = uid or x_uid

    if uid_override:
        uid = int(uid_override)

    return Credentials(
        cookie=cookie,
        uid=uid,
        source=source,
        llm_api_key=llm_key,
        llm_key_source=llm_source,
        cookie_field_names=cookie_field_names(cookie),
    )
