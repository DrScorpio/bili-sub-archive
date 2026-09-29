"""配图下载：动态原图 / 头像 / 专栏图片。

阶段 0 第 3.7 节的实现注意点：

1. 动态原图 URL 是 ``http://``，专栏图片是 ``https://`` → 统一升级为 https；
2. 图片 CDN 不校验 Cookie，可匿名下载，失败只可能是网络/风控/URL 过期，
   错误分类**不归因于权限**。

下载失败不留空文件：产物保留原链接，状态记 ``failed``，可重跑补做
（计划 3.2：只有产物可读且校验通过才标记 ``done``）。
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ..bili.client import upgrade_url
from ..paths import rel_path
from ..ui import NULL_TASK

#: 常见图片 MIME → 扩展名
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}

#: 需要剥离的 CDN 缩放后缀（``xxx.jpg@800w_1e_1c.webp`` → ``xxx.jpg``）
_RESIZE_SEP = "@"


def strip_resize(url: str) -> str:
    """去掉 CDN 缩放后缀，尽量拿到原图。"""
    raw = str(url or "")
    if _RESIZE_SEP in raw:
        head, tail = raw.split(_RESIZE_SEP, 1)
        if any(tail.lower().endswith(ext) or ext in tail.lower() for ext in _ALLOWED_EXT) or "w_" in tail:
            return head
    return raw


def guess_ext(url: str, content_type: str = "") -> str:
    mime = str(content_type or "").split(";")[0].strip().lower()
    if mime in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime]
    path = urlparse(strip_resize(str(url or ""))).path
    ext = Path(path).suffix.lower()
    if ext in _ALLOWED_EXT:
        return ".jpg" if ext == ".jpeg" else ext
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed in _ALLOWED_EXT:
        return ".jpg" if guessed == ".jpeg" else guessed
    return ".jpg"


@dataclass
class ImageRecord:
    """一张图片的下载状态（写入 metadata.extra.images，供重跑精确补做）。"""

    index: int
    url: str
    name: str = ""                 # 相对条目目录的路径，如 images/01.png
    status: str = "pending"        # done | failed | skipped
    size: int = 0
    content_type: str = ""
    error_kind: str = ""
    message: str = ""
    role: str = "body"             # body | avatar | cover
    width: object = None
    height: object = None

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "url": self.url,
            "name": self.name,
            "status": self.status,
            "size": self.size,
            "content_type": self.content_type,
            "error_kind": self.error_kind,
            "message": self.message,
            "role": self.role,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ImageRecord":
        return cls(
            index=int(data.get("index") or 0),
            url=str(data.get("url") or ""),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or "pending"),
            size=int(data.get("size") or 0),
            content_type=str(data.get("content_type") or ""),
            error_kind=str(data.get("error_kind") or ""),
            message=str(data.get("message") or ""),
            role=str(data.get("role") or "body"),
            width=data.get("width"),
            height=data.get("height"),
        )


@dataclass
class ImagePlan:
    """一次下载的实际结果。"""

    records: list[ImageRecord] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for r in self.records if r.status == "done")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.records if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.records if r.status == "skipped")

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def path_for(self, index: int) -> str:
        for record in self.records:
            if record.index == index:
                return record.name if record.status == "done" else record.url
        return ""

    def to_json(self) -> list[dict]:
        return [r.to_json() for r in self.records]

    def summary(self) -> str:
        parts = [f"成功 {self.done}"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        return " / ".join(parts)


def _previous_by_index(previous: list[dict] | None) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for raw in previous or []:
        try:
            out[int(raw.get("index"))] = raw
        except (TypeError, ValueError):
            continue
    return out


def download_images(
    api,
    occurrences: list[dict],
    *,
    entry_dir: Path,
    referer: str = "",
    previous: list[dict] | None = None,
    logger=None,
    role: str = "body",
    names: list[str] | None = None,
    ui=None,
) -> ImagePlan:
    """下载一组图片（``occurrences`` 为 ``{url, width, height, ...}``，保留原顺序）。

    - 同一 URL 只请求一次，重复出现复用同一份文件；
    - ``previous`` 里状态为 ``done`` 且文件仍在的条目直接跳过（重跑不重复下载）；
    - 文件名默认 ``01.png``、``02.jpg``…，可用 ``names`` 指定固定名（如头像）。
    - ``ui`` 给出时挂一个"已下 N 张"的进度任务（只有真的要下载时才创建）。
    """
    images_dir = Path(entry_dir) / "images"
    plan = ImagePlan()
    prev = _previous_by_index(previous)
    task = NULL_TASK
    if ui is not None:
        pending = _pending_count(occurrences, prev, Path(entry_dir))
        if pending:
            task = ui.task(total=pending, label=f"配图（{role or 'body'}）", keep=False)
    url_to_name: dict[str, str] = {}
    index = 0
    for raw in occurrences:
        index += 1
        url = upgrade_url(strip_resize(str((raw or {}).get("url") or "")))
        record = ImageRecord(
            index=index,
            url=str((raw or {}).get("url") or ""),
            role=role,
            width=(raw or {}).get("width"),
            height=(raw or {}).get("height"),
        )
        if not url:
            record.status = "failed"
            record.error_kind = "bad_request"
            record.message = "空图片 URL"
            plan.records.append(record)
            continue

        if url in url_to_name:
            record.name = url_to_name[url]
            record.status = "done"
            record.message = "与前面的图片相同，复用同一文件"
            plan.records.append(record)
            continue

        prior = prev.get(index)
        if (prior and str(prior.get("status")) == "done" and prior.get("name")
                and (Path(entry_dir) / str(prior["name"])).is_file()):
            record.name = str(prior["name"])
            record.status = "done"
            record.size = int(prior.get("size") or 0)
            record.content_type = str(prior.get("content_type") or "")
            record.message = "已存在有效产物，跳过下载"
            url_to_name[url] = record.name
            plan.records.append(record)
            continue

        if names and index <= len(names) and names[index - 1]:
            file_name = names[index - 1]
        else:
            file_name = f"{index:02d}{guess_ext(url)}"
        dest = images_dir / file_name
        result = api.download(url, dest, referer=referer)
        if result.ok:
            # 实际扩展名可能与猜测不同（Content-Type 优先）
            actual_ext = guess_ext(url, result.content_type)
            if actual_ext != dest.suffix.lower() and result.content_type:
                final = dest.with_suffix(actual_ext)
                if final != dest:
                    try:
                        if final.exists():
                            final.unlink()
                        dest.replace(final)
                        dest = final
                        file_name = final.name
                    except OSError:
                        pass
            record.name = rel_path(dest, entry_dir)
            record.status = "done"
            record.size = result.bytes_written
            record.content_type = result.content_type
            url_to_name[url] = record.name
            if logger is not None:
                logger.debug(f"图片下载成功 {record.name}（{result.bytes_written} 字节）")
        else:
            record.status = "failed"
            record.error_kind = result.error_kind
            record.message = result.message[:200]
            # 失败不留半截文件
            try:
                if dest.exists() and dest.stat().st_size == 0:
                    dest.unlink()
            except OSError:
                pass
            if logger is not None:
                logger.warning(f"图片下载失败 {url}：{result.message}")
        plan.records.append(record)
        task.advance()
    return plan


def _pending_count(occurrences: list[dict], prev: dict[int, dict], entry_dir: Path) -> int:
    """真正需要发请求的图片数（用于进度条总量）。

    与主循环同一套判定：空 URL 跳过、同 URL 去重、已 ``done`` 且文件仍在的复用。
    只影响进度条分母，不影响任何产物与状态。
    """
    pending = 0
    seen: set[str] = set()
    for index, raw in enumerate(occurrences, 1):
        url = upgrade_url(strip_resize(str((raw or {}).get("url") or "")))
        if not url or url in seen:
            continue
        seen.add(url)
        prior = prev.get(index)
        if (prior and str(prior.get("status")) == "done" and prior.get("name")
                and (entry_dir / str(prior["name"])).is_file()):
            continue
        pending += 1
    return pending
