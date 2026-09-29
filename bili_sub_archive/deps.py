"""运行时依赖探测（阶段 2 起需要外部工具与可选库）。

阶段 1 是**纯标准库**；阶段 2 引入以下依赖，全部按"惰性导入 + 明确报缺"处理，
绝不让 ``import bili_sub_archive`` 因缺依赖而失败：

| 依赖 | 用途 | 缺失后果 |
| --- | --- | --- |
| ``yt-dlp`` | 视频分 P 下载 | ``media`` 步骤 ``failed(dependency_missing)``，可装好后 ``retry`` |
| ``ffmpeg`` | DASH 音视频合流、提取 ASR 音频 | 只能下到渐进式单流；ASR 无法提取音频 |
| ``ffprobe`` | 校验媒体可播放、读时长/分辨率 | 降级为"文件大小 + 容器魔数"校验 |
| ``faster-whisper`` | 本地语音转写（默认关闭） | 开启 ASR 且缺依赖时 ``transcript`` 记 ``failed`` |
| ``Pillow`` | 动态单张长图 | ``render`` 步骤 ``failed(dependency_missing)`` |
| ``mmdc``（mermaid-cli） | ``mindmap.mmd`` 渲染 PNG（阶段 3） | ``mindmap`` 记 ``failed``，``.mmd`` 仍保留 |
| ``openai``（可选） | 总结走 OpenAI SDK（阶段 3） | 缺失时改用标准库直连同一端点，功能等价 |

设计原则（计划第 4 节）：未配置的依赖不影响其他步骤；每个缺失项都要给出
**可操作的安装提示**，而不是抛一句 ImportError。
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path

#: 缺依赖时统一的错误分类（区别于网络/权限/解析失败）
KIND_DEPENDENCY = "dependency_missing"

#: 各依赖的安装提示（写进 metadata 与运行摘要，供用户照做）
INSTALL_HINTS: dict[str, str] = {
    "yt_dlp": "pip install \"bili-sub-archive[media]\"（或 pip install yt-dlp）",
    "ffmpeg": "安装 FFmpeg 并把 ffmpeg.exe 加入 PATH（winget install Gyan.FFmpeg）",
    "ffprobe": "ffprobe 随 FFmpeg 一起安装，确认与 ffmpeg.exe 同目录且在 PATH 中",
    "faster_whisper": "pip install \"bili-sub-archive[asr]\"（或 pip install faster-whisper）",
    "PIL": "pip install \"bili-sub-archive[media]\"（或 pip install Pillow）",
    "openai": "pip install \"bili-sub-archive[summary]\"（或 pip install openai）",
    "rich": "pip install \"bili-sub-archive[ui]\"（或 pip install rich）：终端彩色与实时进度，不装也能跑",
    "mmdc": "npm install -g @mermaid-js/mermaid-cli（需要 Node.js；首次渲染会下载 Chromium）",
}


@dataclass(frozen=True)
class Dependency:
    """一项运行时依赖的探测结果。"""

    key: str
    label: str
    purpose: str
    available: bool
    detail: str = ""
    hint: str = ""
    #: 可选依赖：缺失时不影响交付物（例如没装 openai SDK 也能用标准库直连端点），
    #: 因此 :func:`missing` 默认不把它算进"缺失依赖"告警里，只在 ``check`` 里展示。
    optional: bool = False

    @property
    def state(self) -> str:
        return "已安装" if self.available else ("未安装（可选）" if self.optional else "缺失")


def module_available(name: str) -> bool:
    """模块是否可导入（不真的导入，避免副作用与耗时）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def module_version(name: str) -> str:
    """尽力读取模块版本号；失败返回空串（不抛异常）。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(name)
        except PackageNotFoundError:
            return ""
    except Exception:  # pragma: no cover - 版本号只是锦上添花
        return ""


def executable_path(name: str, explicit: str = "") -> str:
    """定位可执行文件：显式配置优先，其次 PATH。

    ``explicit`` 可以是可执行文件本身，也可以是所在目录（FFmpeg 的常见用法）。
    """
    raw = str(explicit or "").strip()
    if raw:
        candidate = Path(raw)
        if candidate.is_dir():
            for suffix in (".exe", ""):
                probe = candidate / f"{name}{suffix}"
                if probe.is_file():
                    return str(probe)
        elif candidate.is_file():
            return str(candidate)
        # 显式配置无效时不静默接受，继续回落到 PATH
    found = shutil.which(name)
    return found or ""


def probe_ffmpeg(config=None) -> Dependency:
    explicit = getattr(config, "ffmpeg_path", "") if config is not None else ""
    path = executable_path("ffmpeg", explicit)
    return Dependency(
        key="ffmpeg", label="ffmpeg", purpose="DASH 音视频合流、提取 ASR 音频",
        available=bool(path), detail=path or "未在 PATH 中找到",
        hint=INSTALL_HINTS["ffmpeg"],
    )


def probe_ffprobe(config=None) -> Dependency:
    explicit = getattr(config, "ffprobe_path", "") if config is not None else ""
    path = executable_path("ffprobe", explicit)
    return Dependency(
        key="ffprobe", label="ffprobe", purpose="校验媒体可播放、读取时长与分辨率",
        available=bool(path), detail=path or "未在 PATH 中找到",
        hint=INSTALL_HINTS["ffprobe"],
    )


def probe_ytdlp() -> Dependency:
    ok = module_available("yt_dlp")
    version = module_version("yt-dlp") if ok else ""
    return Dependency(
        key="yt_dlp", label="yt-dlp", purpose="视频分 P 下载（并发/续传）",
        available=ok, detail=f"版本 {version}" if version else ("可导入" if ok else "未安装"),
        hint=INSTALL_HINTS["yt_dlp"],
    )


def probe_whisper() -> Dependency:
    ok = module_available("faster_whisper")
    version = module_version("faster-whisper") if ok else ""
    return Dependency(
        key="faster_whisper", label="faster-whisper", purpose="本地语音转写（默认关闭，可选）",
        available=ok, detail=f"版本 {version}" if version else ("可导入" if ok else "未安装"),
        hint=INSTALL_HINTS["faster_whisper"],
    )


def probe_pillow() -> Dependency:
    ok = module_available("PIL")
    version = module_version("Pillow") if ok else ""
    return Dependency(
        key="PIL", label="Pillow", purpose="动态单张长 PNG",
        available=ok, detail=f"版本 {version}" if version else ("可导入" if ok else "未安装"),
        hint=INSTALL_HINTS["PIL"],
    )


def probe_openai() -> Dependency:
    """``openai`` SDK 是**可选**的：没装时 :mod:`bili_sub_archive.summarize.llm` 用标准库直连端点。"""
    ok = module_available("openai")
    version = module_version("openai") if ok else ""
    return Dependency(
        key="openai", label="openai SDK", purpose="总结（OpenAI 兼容接口；未装则标准库直连）",
        available=ok, detail=f"版本 {version}" if version else ("可导入" if ok else "未安装"),
        hint=INSTALL_HINTS["openai"], optional=True,
    )


def probe_rich() -> Dependency:
    """``rich`` 是**可选**的：没装时 ``bili_sub_archive.ui`` 用内置纯文本皮肤，功能等价。"""
    ok = module_available("rich")
    version = module_version("rich") if ok else ""
    return Dependency(
        key="rich", label="rich", purpose="终端彩色、面板表格与实时进度（没装则纯文本）",
        available=ok, detail=f"版本 {version}" if version else ("可导入" if ok else "未安装"),
        hint=INSTALL_HINTS["rich"], optional=True,
    )


def probe_mermaid_cli(config=None) -> Dependency:
    explicit = getattr(config, "mindmap_mmdc_path", "") if config is not None else ""
    path = executable_path("mmdc", explicit)
    return Dependency(
        key="mmdc", label="mermaid-cli", purpose="思维导图 PNG 渲染（mindmap.mmd 不依赖它）",
        available=bool(path), detail=path or "未在 PATH 中找到",
        hint=INSTALL_HINTS["mmdc"],
    )


def probe_dependencies(config=None) -> list[Dependency]:
    """探测阶段 2/3 的全部外部依赖（供 ``check`` 报告）。"""
    return [
        probe_ytdlp(),
        probe_ffmpeg(config),
        probe_ffprobe(config),
        probe_pillow(),
        probe_whisper(),
        probe_mermaid_cli(config),
        probe_openai(),
        probe_rich(),
    ]


def by_key(deps: list[Dependency]) -> dict[str, Dependency]:
    return {dep.key: dep for dep in deps}


def missing(deps: list[Dependency], *, include_optional: bool = False) -> list[Dependency]:
    """缺失且**必需**的依赖（可选依赖只在 ``check`` 里展示，不进告警）。"""
    return [dep for dep in deps if not dep.available and (include_optional or not dep.optional)]
