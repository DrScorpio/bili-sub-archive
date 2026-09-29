"""本地语音转写（阶段 2 的兜底路径，**默认关闭**）。

需求 2 / 3.3.2 / 6.7 的三条硬约束：

1. ASR 必须**可开启、可关闭**；关闭时**不运行识别**，也不把媒体发给任何云端
   识别服务（本模块只调用本机 ``faster-whisper``，没有任何网络代码）；
2. 只在"该分 P 没有可用字幕"时才执行（字幕是主路径，阶段 0 第 3.3.3 节：
   本项目样本普遍带 ``ai-zh``，ASR 在本样本上几乎不会被触发）；
3. 模型首次下载属于**安装准备**，不是识别过程的一部分；缺依赖/缺模型时如实
   记 ``failed(dependency_missing)`` 并给出安装提示，可 ``retry`` 补做。

依赖都是惰性导入：``import bili_sub_archive`` 不因缺 ffmpeg / faster-whisper 而失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..deps import KIND_DEPENDENCY, INSTALL_HINTS, module_available
from ..errors import KIND_HTTP, KIND_PARSE
from ..paths import ensure_dir
from .srt import Segment

#: ffmpeg 提取音频的参数：16kHz 单声道 PCM —— faster-whisper 的原生输入格式
AUDIO_SAMPLE_RATE = 16000

#: 上游没有可转写的媒体文件（权限门控 / 媒体下载关闭 / media 步骤失败）
KIND_NO_MEDIA = "no_media"
#: 转写跑通了但整段没有人声（VAD 过滤后为空）—— 终态，重跑不会变
KIND_NO_SPEECH = "no_speech"

#: 注入式命令执行器：``(argv) -> (returncode, stdout, stderr)``
Runner = Callable[[list[str]], tuple[int, str, str]]


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    import subprocess

    completed = subprocess.run(  # noqa: S603 - 只执行配置里的 ffmpeg 路径
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


@dataclass
class AsrOutcome:
    """一次转写的结果。"""

    ok: bool = False
    segments: list[Segment] = field(default_factory=list)
    audio_name: str = ""
    model: str = ""
    language: str = ""
    duration: float = 0.0
    error_kind: str = ""
    message: str = ""

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.segments)


# --------------------------------------------------------------------------- #
# 音频提取
# --------------------------------------------------------------------------- #
def extract_audio(ffmpeg: str, source: Path, dest: Path, *, runner: Runner | None = None,
                  logger=None) -> tuple[bool, str]:
    """用 ffmpeg 把媒体里的音轨抽成 16kHz 单声道 WAV。

    返回 ``(是否成功, 说明)``。ffmpeg 缺失或执行失败都不抛异常。
    """
    if not ffmpeg:
        return False, f"未找到 ffmpeg，无法提取音频：{INSTALL_HINTS['ffmpeg']}"
    src = Path(source)
    if not src.is_file():
        return False, f"媒体文件不存在，无法提取音频：{src.name}"
    target = Path(dest)
    ensure_dir(target.parent)
    argv = [
        ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn", "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(target),
    ]
    try:
        code, _out, err = (runner or _default_runner)(argv)
    except Exception as exc:  # noqa: BLE001 - 沙箱限制/超时都算提取失败
        return False, f"ffmpeg 调用失败：{type(exc).__name__}: {exc}"
    if code != 0 or not target.is_file() or target.stat().st_size <= 0:
        tail = (err or "").strip().splitlines()
        detail = tail[-1] if tail else f"退出码 {code}"
        return False, f"ffmpeg 提取音频失败：{detail}"
    if logger is not None:
        logger.debug(f"    音频提取完成：{target.name}（{target.stat().st_size} 字节）")
    return True, str(target)


# --------------------------------------------------------------------------- #
# 转写器
# --------------------------------------------------------------------------- #
@runtime_checkable
class Transcriber(Protocol):
    """转写器协议（真实实现 faster-whisper，测试注入替身）。"""

    name: str

    def probe(self) -> tuple[bool, str]: ...

    def transcribe(self, audio: Path, *, page: int, logger=None,
                   on_segment=None) -> AsrOutcome: ...


class FasterWhisperTranscriber:
    """``faster-whisper`` 适配器（模型实例懒加载并缓存，避免每个分 P 重载）。"""

    name = "faster-whisper"

    def __init__(self, *, model: str = "small", device: str = "cpu",
                 compute_type: str = "int8", language: str = "zh",
                 beam_size: int = 5, vad_filter: bool = True, logger=None):
        self.model_name = model or "small"
        self.device = device or "cpu"
        self.compute_type = compute_type or "int8"
        self.language = language or ""
        self.beam_size = max(1, int(beam_size))
        self.vad_filter = bool(vad_filter)
        self.logger = logger
        self._model = None

    def probe(self) -> tuple[bool, str]:
        if not module_available("faster_whisper"):
            return False, f"未安装 faster-whisper：{INSTALL_HINTS['faster_whisper']}"
        return True, "faster-whisper 可用"

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # 惰性导入

            self._model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def transcribe(self, audio: Path, *, page: int, logger=None,
                   on_segment=None) -> AsrOutcome:
        """转写一个音频文件；``on_segment(累计段数)`` 用于实时进度（可省略）。"""
        ok, note = self.probe()
        outcome = AsrOutcome(model=self.model_name, language=self.language)
        if not ok:
            outcome.error_kind = KIND_DEPENDENCY
            outcome.message = note
            return outcome
        if not Path(audio).is_file():
            outcome.error_kind = KIND_PARSE
            outcome.message = "音频文件不存在"
            return outcome
        try:
            model = self._load()
            raw_segments, info = model.transcribe(
                str(audio),
                language=self.language or None,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
            )
            segments: list[Segment] = []
            for item in raw_segments:      # 生成器：必须在这里消费完
                text = str(getattr(item, "text", "") or "").strip()
                if on_segment is not None:
                    # 进度按"已消费的片段数"走（faster-whisper 不报总量）
                    on_segment(len(segments))
                if not text:
                    continue
                segments.append(
                    Segment(
                        start=float(getattr(item, "start", 0.0) or 0.0),
                        end=float(getattr(item, "end", 0.0) or 0.0),
                        text=text, page=page, source="asr",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 模型加载/推理失败都要如实回报
            outcome.error_kind = KIND_HTTP
            outcome.message = f"转写失败：{type(exc).__name__}: {exc}"[:300]
            return outcome

        outcome.segments = segments
        outcome.duration = float(getattr(info, "duration", 0.0) or 0.0)
        outcome.ok = bool(segments)
        if not segments:
            # 不是"失败"：识别确实跑完了，只是没有人声。终态，不该被反复重试。
            outcome.error_kind = KIND_NO_SPEECH
            outcome.message = "转写未产出任何文字片段（可能整段无人声）"
        elif logger is not None:
            logger.info(f"    P{page:02d} 本地转写完成：{len(segments)} 段 / {outcome.chars} 字")
        return outcome


def find_media_file(entry_dir: Path, page: int) -> Path | None:
    """在 ``videos/`` 下找该分 P 的媒体文件（扩展名由下载器决定）。"""
    videos = Path(entry_dir) / "videos"
    if not videos.is_dir():
        return None
    stem = f"P{int(page):02d}"
    candidates = [
        p for p in sorted(videos.glob(f"{stem}.*"))
        if p.is_file() and p.suffix.lower() not in (".part", ".ytdl")
        and p.stat().st_size > 0
    ]
    return candidates[0] if candidates else None


def transcribe_page(
    transcriber: Transcriber,
    *,
    entry_dir: Path,
    page,
    ffmpeg: str = "",
    keep_audio: bool = False,
    logger=None,
    runner: Runner | None = None,
    on_segment=None,
) -> AsrOutcome:
    """对一个分 P 执行"提音频 → 本地转写"，并（默认）清理中间音频。"""
    number = int(getattr(page, "page", 0) or 0)
    outcome = AsrOutcome()
    media = find_media_file(Path(entry_dir), number)
    if media is None:
        # 不是"转写失败"，而是"上游没给到可转写的媒体"（例如充电视频无权限、
        # 媒体下载关闭或 media 步骤失败）。用独立分类，避免被当成可无限重试的失败。
        outcome.error_kind = KIND_NO_MEDIA
        outcome.message = f"P{number:02d} 没有可用的媒体文件，无法本地转写（先补做 media 步骤）"
        return outcome

    available, note = transcriber.probe()
    if not available:
        outcome.error_kind = KIND_DEPENDENCY
        outcome.message = note
        return outcome
    if not ffmpeg:
        outcome.error_kind = KIND_DEPENDENCY
        outcome.message = f"未找到 ffmpeg，无法提取音频：{INSTALL_HINTS['ffmpeg']}"
        return outcome

    audio = Path(entry_dir) / "transcript" / f"P{number:02d}.audio.wav"
    ok, detail = extract_audio(ffmpeg, media, audio, runner=runner, logger=logger)
    if not ok:
        outcome.error_kind = KIND_DEPENDENCY if "ffmpeg" in detail else KIND_HTTP
        outcome.message = detail
        return outcome

    try:
        # ``on_segment`` 只在真的需要进度时才传：测试替身的 transcribe 签名不含它
        if on_segment is None:
            result = transcriber.transcribe(audio, page=number, logger=logger)
        else:
            result = transcriber.transcribe(audio, page=number, logger=logger,
                                           on_segment=on_segment)
    finally:
        if not keep_audio:
            try:
                audio.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 清理失败不影响结论
                pass
    result.audio_name = audio.name if keep_audio else ""
    return result
