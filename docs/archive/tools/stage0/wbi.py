"""WBI 签名（B 站 web 端接口风控签名）。

参考: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md

算法与 E:\\bilivideo\\bilisum\\wbi.py（2026-08 实测可用）一致，此处独立实现以保持
SubVideo 不依赖旧工程。
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import urlencode

# mixinKeyEncTab: 从 img_key + sub_key 拼接串中按此表重排后取前 32 位
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

# urlencode 前需从参数值中过滤的特殊字符
_FILTER_CHARS = "!'()*"


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """由 wbi_img 的 img_key/sub_key 计算 mixin_key（32 位）。"""
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _filter_value(value: object) -> str:
    return "".join(ch for ch in str(value) if ch not in _FILTER_CHARS)


def sign(params: dict, img_key: str, sub_key: str, wts: int | None = None) -> dict:
    """对参数字典做 WBI 签名，返回含 wts 与 w_rid 的新字典。"""
    mixin_key = get_mixin_key(img_key, sub_key)
    data: dict[str, str] = {k: _filter_value(v) for k, v in params.items()}
    data["wts"] = str(wts if wts is not None else int(time.time()))
    data = dict(sorted(data.items()))
    query = urlencode(data)
    data["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return data
