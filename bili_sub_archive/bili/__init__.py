"""B 站采集适配层（自研薄封装）。

对外只暴露 :class:`BilibiliApi`；业务代码不得直接拼 URL（计划 2.1-5）。
接口白名单见 ``docs/DEVELOPMENT.md`` 第 3.2 节。
"""

from __future__ import annotations

from .api import BilibiliApi, BiliApi  # noqa: F401
from .client import ApiResult, DownloadResult, HttpClient  # noqa: F401

__all__ = ["BilibiliApi", "BiliApi", "ApiResult", "DownloadResult", "HttpClient"]
