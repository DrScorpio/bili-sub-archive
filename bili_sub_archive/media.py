"""视频媒体下载（阶段 2）：并发 / 续传 / 分 P / 可播放校验。

计划第 5 节阶段 2 完成门槛："多 P 与多图样本产物完整；关闭识别不运行模型；失败后补做"。
需求 3.3.1 的落地要点：

1. 下载当前账号**有权获取**的视频文件；多 P 作为一个条目、一个文件夹，
   各 P 媒体文件保存在同一文件夹内（``videos/P01.mp4``、``videos/P02.mp4``），
   **不强制拼接**为单个视频文件（需求第 4 节结构）；
2. 并发可配置（``video_workers`` 同时处理的视频数、``segment_workers`` 单视频分片并发）；
3. 支持断点续传（yt-dlp 的 ``continuedl`` + ``.part`` 文件）与可恢复的重试；
4. 完成后**校验文件可播放且大小非零** —— 有 ``ffprobe`` 时读真实流信息，
   没有时降级为"大小阈值 + 容器魔数"校验，并如实标注校验方式（不假装校验过）。

设计取舍：

- 下载器抽象成 :class:`Downloader` 协议，真实实现是 :class:`YtDlpDownloader`
  （惰性导入 ``yt_dlp``）；离线测试注入替身，**测试绝不联网**；
- 缺依赖不是崩溃而是 ``failed(dependency_missing)``，装好后 ``retry`` 可补做；
- 试看片段绝不下载：``permission=preview_only`` 在归档层就被拦下（阶段 1 已实现）。
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .bili.client import WWW_ROOT
from .deps import KIND_DEPENDENCY, module_available
from .errors import (
    KIND_DENIED,
    KIND_HTTP,
    KIND_NETWORK,
    KIND_NOT_FOUND,
    KIND_PARSE,
    KIND_RISK,
)
from .paths import ensure_dir, rel_path

#: 低于该字节数一律视为无效产物（空文件 / 只有 HTML 错误页）
MIN_MEDIA_BYTES = 4096

#: 清晰度别名 → 高度上限（0 表示不限制）
QUALITY_HEIGHTS: dict[str, int] = {
    "360p": 360, "480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440, "4k": 2160,
}

#: 容器魔数（阶段 0 只验证过 dash/mp4 与 flv；这里覆盖常见落盘形态）
_MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"ftyp", 4, "mp4"),          # ISO BMFF（mp4 / m4s / fMP4）
    (b"FLV\x01", 0, "flv"),
    (b"\x1a\x45\xdf\xa3", 0, "matroska"),
    (b"RIFF", 0, "riff"),
    (b"ID3", 0, "mp3"),
    (b"OggS", 0, "ogg"),
)


def quality_height(quality: str) -> int:
    """``"1080p"`` / ``"1080"`` / ``"best"`` → 高度上限（``best`` 返回 0 = 不限制）。"""
    raw = str(quality or "").strip().lower()
    if raw in ("", "best", "max", "auto"):
        return 0
    if raw in QUALITY_HEIGHTS:
        return QUALITY_HEIGHTS[raw]
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else 0


def build_format_selector(quality: str, *, has_ffmpeg: bool) -> str:
    """构造 yt-dlp 的 ``format`` 表达式。

    没有 ``ffmpeg`` 时**不能**选 DASH 分离流（选了也合流不了），退化为渐进式单流，
    此时清晰度通常更低 —— 这一点由调用方写进 notes，不做静默降级。
    """
    height = quality_height(quality)
    cap = f"[height<=?{height}]" if height else ""
    if has_ffmpeg:
        return f"bestvideo{cap}+bestaudio/best{cap}/best"
    return f"best{cap}[ext=mp4]/best{cap}/best"


def classify_download_error(message: str) -> str:
    """把 yt-dlp / 网络的错误文本映射到项目统一错误分类。"""
    text = str(message or "")
    lowered = text.lower()
    if "412" in text or "352" in text or "risk" in lowered:
        return KIND_RISK
    if "403" in text or "forbidden" in lowered:
        return KIND_DENIED
    if "404" in text or "not found" in lowered:
        return KIND_NOT_FOUND
    if any(token in lowered for token in ("timed out", "timeout", "connection", "unable to download",
                                          "temporary failure", "network", "ssl")):
        return KIND_NETWORK
    if any(token in lowered for token in ("no video formats", "unsupported url", "unable to extract")):
        return KIND_PARSE
    return KIND_HTTP


def sniff_container(path: Path) -> str:
    """按文件头猜测容器；读不到或认不出返回空串。"""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return ""
    for magic, offset, name in _MAGIC:
        if head[offset:offset + len(magic)] == magic:
            return name
    return ""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class MediaFile:
    """单个分 P 的下载与校验结果（写入 ``metadata.extra.media[]``，供精确补做）。"""

    page: int
    cid: int = 0
    part: str = ""
    name: str = ""                  # 相对条目目录，如 videos/P01.mp4
    status: str = "pending"         # done | failed | skipped
    size: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    container: str = ""
    format_id: str = ""
    vcodec: str = ""
    acodec: str = ""
    has_audio: bool = False
    verified: bool = False
    verify_note: str = ""
    resumed: bool = False
    attempts: int = 0
    error_kind: str = ""
    message: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def to_json(self) -> dict:
        return {
            "page": self.page, "cid": self.cid, "part": self.part, "name": self.name,
            "status": self.status, "size": self.size, "duration": self.duration,
            "width": self.width, "height": self.height, "container": self.container,
            "format_id": self.format_id, "vcodec": self.vcodec, "acodec": self.acodec,
            "has_audio": self.has_audio, "verified": self.verified,
            "verify_note": self.verify_note, "resumed": self.resumed,
            "attempts": self.attempts, "error_kind": self.error_kind,
            "message": self.message, "url": self.url,
        }

    @classmethod
    def from_json(cls, data: dict) -> "MediaFile":
        data = data or {}
        return cls(
            page=int(data.get("page") or 0),
            cid=int(data.get("cid") or 0),
            part=str(data.get("part") or ""),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or "pending"),
            size=int(data.get("size") or 0),
            duration=float(data.get("duration") or 0.0),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            container=str(data.get("container") or ""),
            format_id=str(data.get("format_id") or ""),
            vcodec=str(data.get("vcodec") or ""),
            acodec=str(data.get("acodec") or ""),
            has_audio=bool(data.get("has_audio")),
            verified=bool(data.get("verified")),
            verify_note=str(data.get("verify_note") or ""),
            resumed=bool(data.get("resumed")),
            attempts=int(data.get("attempts") or 0),
            error_kind=str(data.get("error_kind") or ""),
            message=str(data.get("message") or ""),
            url=str(data.get("url") or ""),
        )


@dataclass
class MediaPlan:
    """一次媒体下载的整体结果。"""

    records: list[MediaFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    downloader: str = ""
    format_selector: str = ""
    verify_mode: str = ""

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
    def bytes_written(self) -> int:
        return sum(r.size for r in self.records if r.status == "done")

    def to_json(self) -> list[dict]:
        return [r.to_json() for r in self.records]

    def summary(self) -> str:
        parts = [f"成功 {self.done}"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        return " / ".join(parts)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
@dataclass
class MediaProbe:
    ok: bool = False
    container: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    vcodec: str = ""
    acodec: str = ""
    has_audio: bool = False
    mode: str = ""                  # ffprobe | sniff | missing
    note: str = ""
    message: str = ""


#: 注入式命令执行器：``(argv) -> (returncode, stdout, stderr)``
Runner = Callable[[list[str]], tuple[int, str, str]]


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """默认用 ``subprocess`` 执行 ffprobe（捕获输出）。

    Windows 上 CPython 的 ``subprocess`` 用匿名管道而非命名管道，因此在受限沙箱
    里也能工作；万一环境不允许，调用方会拿到异常并降级到魔数校验。
    """
    import subprocess

    completed = subprocess.run(  # noqa: S603 - 只执行白名单里的 ffprobe 路径
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def probe_media(path: Path, *, ffprobe: str = "", runner: Runner | None = None) -> MediaProbe:
    """校验媒体产物：大小非零 → （ffprobe 可读流信息 | 容器魔数兜底）。

    需求 3.3.1 要求"完成后校验文件可播放且大小非零"。有 ``ffprobe`` 时给出
    真实结论；没有时**只声明结构上像媒体文件**，并把 ``verify_note`` 写成
    "未用 ffprobe 校验"，绝不把魔数校验说成"已验证可播放"。
    """
    target = Path(path)
    if not target.is_file():
        return MediaProbe(ok=False, mode="missing", message="产物文件不存在")
    size = target.stat().st_size
    if size <= 0:
        return MediaProbe(ok=False, mode="missing", message="产物为空文件")
    if size < MIN_MEDIA_BYTES:
        return MediaProbe(ok=False, mode="missing",
                          message=f"产物仅 {size} 字节，小于最小阈值 {MIN_MEDIA_BYTES}（疑似错误页/空流）")

    if ffprobe:
        probe = _probe_with_ffprobe(target, ffprobe, runner or _default_runner)
        if probe is not None:
            return probe
        # ffprobe 执行失败 → 降级，但把原因带上
        fallback = _probe_by_sniff(target)
        fallback.note = (fallback.note + "；ffprobe 调用失败，已降级为容器魔数校验").strip("；")
        return fallback
    return _probe_by_sniff(target)


def _probe_by_sniff(target: Path) -> MediaProbe:
    container = sniff_container(target)
    if not container:
        return MediaProbe(
            ok=False, mode="sniff", note="未使用 ffprobe（不可用）",
            message="文件头不匹配任何已知媒体容器（可能下载到了错误页）",
        )
    return MediaProbe(
        ok=True, container=container, mode="sniff",
        note=f"未使用 ffprobe 校验（不可用）；按容器魔数识别为 {container}，"
             "可播放性未经解码验证",
    )


def _probe_with_ffprobe(target: Path, ffprobe: str, runner: Runner) -> MediaProbe | None:
    """调用 ffprobe 读取流信息；执行失败返回 ``None``（由调用方降级）。"""
    import json

    argv = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(target),
    ]
    try:
        code, stdout, stderr = runner(argv)
    except Exception:  # noqa: BLE001 - 沙箱限制/超时/权限都走降级
        return None
    if code != 0:
        return None
    try:
        payload = json.loads(stdout or "{}")
    except ValueError:
        return None
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float(fmt.get("duration") or (video or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if video is None and audio is None:
        return MediaProbe(ok=False, mode="ffprobe",
                          message="ffprobe 未找到任何音视频流（产物不可播放）")
    if video is not None and duration <= 0:
        return MediaProbe(ok=False, mode="ffprobe",
                          message="ffprobe 未读到有效时长（产物可能不完整）")
    return MediaProbe(
        ok=True,
        container=str(fmt.get("format_name") or sniff_container(target)),
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        vcodec=str((video or {}).get("codec_name") or ""),
        acodec=str((audio or {}).get("codec_name") or ""),
        has_audio=audio is not None,
        mode="ffprobe",
        note="ffprobe 校验通过",
    )


# --------------------------------------------------------------------------- #
# 下载器
# --------------------------------------------------------------------------- #
@dataclass
class DownloadRequest:
    """一个分 P 的下载请求。"""

    bvid: str
    page: int
    cid: int
    part: str
    entry_dir: Path
    referer: str = ""
    cookie: str = ""
    user_agent: str = ""
    quality: str = "1080p"
    segment_workers: int = 4
    timeout: float = 60.0
    retries: int = 3
    ffmpeg: str = ""
    previous: dict | None = None
    #: 该分 P 的进度句柄（``bili_sub_archive.ui.TaskHandle`` 或 no-op）；下载器按需更新
    progress: Any = None

    @property
    def page_url(self) -> str:
        return f"{WWW_ROOT}/video/{self.bvid}?p={self.page}"

    @property
    def stem(self) -> str:
        """``P01``、``P02``…（需求第 4 节：``videos/P01.mp4``）。"""
        return f"P{self.page:02d}"

    @property
    def videos_dir(self) -> Path:
        return Path(self.entry_dir) / "videos"


@runtime_checkable
class Downloader(Protocol):
    """媒体下载器协议（真实实现 yt-dlp，测试注入替身）。"""

    name: str

    def probe(self) -> tuple[bool, str]: ...

    def download(self, request: DownloadRequest) -> MediaFile: ...


class YtDlpDownloader:
    """yt-dlp 适配器（惰性导入，缺依赖时给出可操作提示）。"""

    name = "yt-dlp"

    def __init__(self, *, logger=None):
        self.logger = logger
        self._lock = threading.Lock()

    # -- 可用性 -- #
    def probe(self) -> tuple[bool, str]:
        if not module_available("yt_dlp"):
            from .deps import INSTALL_HINTS

            return False, f"未安装 yt-dlp：{INSTALL_HINTS['yt_dlp']}"
        return True, "yt-dlp 可用"

    # -- 下载 -- #
    def download(self, request: DownloadRequest) -> MediaFile:
        record = MediaFile(page=request.page, cid=request.cid, part=request.part,
                           url=request.page_url)
        ok, note = self.probe()
        if not ok:
            record.status = "failed"
            record.error_kind = KIND_DEPENDENCY
            record.message = note
            return record

        import yt_dlp  # 惰性导入：模块顶层不依赖第三方库

        has_ffmpeg = bool(request.ffmpeg)
        selector = build_format_selector(request.quality, has_ffmpeg=has_ffmpeg)
        record.format_id = selector
        videos_dir = ensure_dir(request.videos_dir)

        def _hook(status: dict) -> None:
            if self.logger is not None and status.get("status") == "finished":
                self.logger.debug(f"    {request.stem} 下载完成：{status.get('filename')}")
            handle = request.progress
            if handle is None or not handle.active:
                return
            state = status.get("status")
            if state == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                handle.update(
                    completed=int(status.get("downloaded_bytes") or 0),
                    total=int(total) if total else None,
                )
            elif state == "finished":
                handle.update(description=f"{request.stem} 下载完成")
            elif state == "postprocessing":
                handle.update(description=f"{request.stem} 合并中")

        def _pp_hook(status: dict) -> None:
            """合并/后处理阶段（DASH 音视频合流）单独上报，进度条不会卡在 100% 看着像死住。"""
            handle = request.progress
            if handle is None or not handle.active:
                return
            if status.get("status") == "started":
                handle.update(description=f"{request.stem} 合并中")

        options: dict[str, Any] = {
            "format": selector,
            "outtmpl": str(videos_dir / f"{request.stem}.%(ext)s"),
            "noplaylist": True,
            "continuedl": True,                     # 断点续传：复用 .part 文件
            "retries": max(1, int(request.retries)),
            "fragment_retries": max(1, int(request.retries)),
            "concurrent_fragment_downloads": max(1, int(request.segment_workers)),
            "socket_timeout": float(request.timeout),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "merge_output_format": "mp4",
            "progress_hooks": [_hook],
            "postprocessor_hooks": [_pp_hook],
            "http_headers": {
                "Referer": request.referer or request.page_url,
                "Origin": WWW_ROOT,
                **({"Cookie": request.cookie} if request.cookie else {}),
                **({"User-Agent": request.user_agent} if request.user_agent else {}),
            },
        }
        if request.ffmpeg:
            options["ffmpeg_location"] = request.ffmpeg

        record.attempts = 1
        # 续传标记必须在下载前判断：yt-dlp 完成后会删掉 .part 残片
        record.resumed = _has_part_artifacts(videos_dir, request.stem)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(request.page_url, download=True)
        except Exception as exc:  # noqa: BLE001 - yt-dlp 的异常层级不稳定，统一分类
            record.status = "failed"
            record.error_kind = classify_download_error(str(exc))
            record.message = f"{type(exc).__name__}: {exc}"[:300]
            return record

        produced = _produced_path(info, videos_dir, request.stem)
        if produced is None:
            record.status = "failed"
            record.error_kind = KIND_PARSE
            record.message = "yt-dlp 未报告产物路径（下载可能被跳过）"
            return record

        record.name = rel_path(produced, request.entry_dir)
        record.size = produced.stat().st_size if produced.is_file() else 0
        if isinstance(info, dict):
            record.duration = float(info.get("duration") or 0.0)
            record.width = int(info.get("width") or 0)
            record.height = int(info.get("height") or 0)
        record.status = "done"
        record.message = "下载完成" + ("（续传）" if record.resumed else "")
        return record


def _produced_path(info: Any, videos_dir: Path, stem: str) -> Path | None:
    """从 yt-dlp 的 info 里取出最终产物路径；取不到时按 ``P01.*`` 兜底扫描。"""
    if isinstance(info, dict):
        downloads = info.get("requested_downloads")
        if isinstance(downloads, list) and downloads:
            raw = (downloads[0] or {}).get("filepath")
            if raw and Path(raw).is_file():
                return Path(raw)
        raw = info.get("filepath") or info.get("_filename")
        if raw and Path(raw).is_file():
            return Path(raw)
    candidates = [
        p for p in sorted(Path(videos_dir).glob(f"{stem}.*"))
        if p.is_file() and p.suffix not in (".part", ".ytdl") and not p.name.endswith(".part")
    ]
    return candidates[0] if candidates else None


def _has_part_artifacts(videos_dir: Path, stem: str) -> bool:
    try:
        return any(Path(videos_dir).glob(f"{stem}*.part"))
    except OSError:  # pragma: no cover - 目录异常不该影响结论
        return False


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def _reusable(previous: dict | None, entry_dir: Path) -> MediaFile | None:
    """上一次已 ``done`` 且文件仍有效 → 直接复用（幂等重跑，不重复下载）。"""
    if not previous:
        return None
    record = MediaFile.from_json(previous)
    if record.status != "done" or not record.name:
        return None
    path = Path(entry_dir) / record.name
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    record.message = "已存在有效产物，跳过下载"
    record.resumed = False
    return record


def download_media(
    *,
    bvid: str,
    pages: list,
    entry_dir: Path,
    referer: str = "",
    cookie: str = "",
    user_agent: str = "",
    quality: str = "1080p",
    video_workers: int = 3,
    segment_workers: int = 4,
    timeout: float = 60.0,
    retries: int = 3,
    ffmpeg: str = "",
    ffprobe: str = "",
    previous: list[dict] | None = None,
    downloader: Downloader | None = None,
    probe_runner: Runner | None = None,
    logger=None,
    ui=None,
) -> MediaPlan:
    """下载一个视频条目的全部分 P（并发 + 续传 + 校验）。

    ``pages`` 为 :class:`bili_sub_archive.bili.parse.PageInfo` 列表（按 page 升序）。
    返回的 :class:`MediaPlan` 里每条记录都带 ``status`` 与失败原因，调用方据此
    决定步骤记 ``done`` / ``failed`` / ``partial``。
    ``ui``（``bili_sub_archive.ui.Ui``）给出时，每个分 P 挂一个字节级进度任务；不传则无进度。
    """
    plan = MediaPlan(downloader=getattr(downloader, "name", "") or "yt-dlp")
    engine = downloader or YtDlpDownloader(logger=logger)
    plan.downloader = getattr(engine, "name", "yt-dlp")

    available, note = engine.probe()
    if not available:
        plan.notes.append(note)
        plan.records = [
            MediaFile(page=getattr(p, "page", 0), cid=getattr(p, "cid", 0),
                      part=getattr(p, "part", ""), status="failed",
                      error_kind=KIND_DEPENDENCY, message=note)
            for p in pages
        ]
        return plan

    has_ffmpeg = bool(ffmpeg)
    plan.format_selector = build_format_selector(quality, has_ffmpeg=has_ffmpeg)
    plan.verify_mode = "ffprobe" if ffprobe else "sniff"
    if not has_ffmpeg:
        plan.notes.append(
            "未找到 ffmpeg：只能下载渐进式单流（清晰度通常低于 DASH 分离流，"
            "且无法提取 ASR 音频）；安装 FFmpeg 后可 retry 以更高清晰度补做"
        )

    prev_by_page = {int(r.get("page") or 0): r for r in (previous or [])}
    handles: dict[int, Any] = {}
    requests: list[DownloadRequest] = []
    for page in pages:
        number = int(getattr(page, "page", 0) or 0)
        reused = _reusable(prev_by_page.get(number), Path(entry_dir))
        if reused is not None:
            plan.records.append(reused)
            continue
        handle = ui.task(total=None, label=f"{bvid} P{number:02d}", kind="bytes",
                         keep=False) if ui else None
        handles[number] = handle
        requests.append(
            DownloadRequest(
                bvid=bvid, page=number, cid=int(getattr(page, "cid", 0) or 0),
                part=str(getattr(page, "part", "") or ""), entry_dir=Path(entry_dir),
                referer=referer, cookie=cookie, user_agent=user_agent, quality=quality,
                segment_workers=segment_workers, timeout=timeout, retries=retries,
                ffmpeg=ffmpeg, previous=prev_by_page.get(number), progress=handle,
            )
        )

    if not requests:
        plan.records.sort(key=lambda r: r.page)
        return plan

    workers = max(1, min(int(video_workers), len(requests)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media") as pool:
        futures = {pool.submit(engine.download, req): req for req in requests}
        for future in as_completed(futures):
            req = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 - 替身/适配器异常不该中断整批
                record = MediaFile(page=req.page, cid=req.cid, part=req.part,
                                   status="failed", error_kind=KIND_HTTP,
                                   message=f"{type(exc).__name__}: {exc}"[:300],
                                   url=req.page_url)
            plan.records.append(record)
            handle = handles.get(req.page)
            if handle is not None and handle.active:
                if record.status == "done":
                    handle.done(description=f"P{req.page:02d} 下载完成")
                else:
                    handle.done(description=f"P{req.page:02d} 失败")

    # ---- 逐个校验产物 ---- #
    for record in plan.records:
        if record.status != "done" or not record.name:
            continue
        probe = probe_media(Path(entry_dir) / record.name, ffprobe=ffprobe,
                            runner=probe_runner)
        record.verified = probe.ok
        record.verify_note = probe.note
        if not probe.ok:
            record.status = "failed"
            record.error_kind = KIND_PARSE
            record.message = f"产物校验失败：{probe.message}"
            continue
        if probe.duration:
            record.duration = probe.duration
        if probe.width:
            record.width = probe.width
            record.height = probe.height
        record.container = probe.container or record.container
        record.vcodec = probe.vcodec or record.vcodec
        record.acodec = probe.acodec or record.acodec
        record.has_audio = probe.has_audio or record.has_audio

    plan.records.sort(key=lambda r: r.page)
    return plan


def media_notes(plan: MediaPlan) -> list[str]:
    """把失败项整理成可读说明（写入 metadata 与运行摘要）。"""
    out = list(plan.notes)
    for record in plan.records:
        if record.status == "failed":
            out.append(
                f"P{record.page:02d} 下载失败（{record.error_kind}）：{record.message}"
                "（重跑只补失败项）"
            )
    return out
