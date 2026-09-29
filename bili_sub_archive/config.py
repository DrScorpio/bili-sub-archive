"""配置：TOML 模板 + 本地覆盖 + 环境变量 + 命令行，四层优先级。

```text
默认值  <  config.toml  <  config.local.toml  <  环境变量 / 命令行
```

- ``config.toml``：可入库的常规配置（默认值、并发、开关）；
- ``config.local.toml``：不入库的本地配置（Cookie、LLM 密钥、个人输出目录）；
- 模板 ``config.example.toml`` 只含占位符，不含任何凭据。

计划第 4 节：配置使用本地 TOML；Cookie/API 密钥优先从环境变量读取，
也允许由未入库的本地配置文件提供；模板配置只含占位符。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .credentials import Credentials, load_credentials
from .errors import ConfigError
from .models import ALL_KINDS, KIND_LABEL
from .timeutil import parse_date

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 各配置段被代码接受的键（含别名与凭据键：``[account] cookie``、``[summary] api_key``）。
#: 不在这里的键 = 拼错或放错了段，会**静默失效**，因此加载时统一告警。
SECTION_KEYS: dict[str, frozenset[str]] = {
    "account": frozenset({"uid", "mid", "cookie"}),
    "output": frozenset({"dir", "download_images", "image_workers"}),
    "scope": frozenset({"dynamic", "video", "article"}),
    "filter": frozenset({"from", "to", "latest"}),
    "request": frozenset({"interval_seconds", "timeout_seconds", "retries", "max_pages", "user_agent"}),
    "discovery": frozenset({"scan_charging_video_subset", "scan_dynamic_secondary_source",
                            "probe_video_access"}),
    "transcript": frozenset({"prefer_subtitle"}),
    "asr": frozenset({"enabled", "model", "device", "compute_type", "language", "beam_size",
                      "keep_audio"}),
    "download": frozenset({"media", "video_workers", "segment_workers", "quality", "timeout_seconds",
                           "ffmpeg_path", "ffprobe_path"}),
    "render": frozenset({"dynamic_png", "width", "max_height", "font"}),
    "summary": frozenset({"enabled", "base_url", "model", "api_key", "llm_api_key", "client",
                          "prompt_file", "chunk_chars", "max_chunks", "overlap_chars", "temperature",
                          "max_tokens", "timeout_seconds", "retries", "min_chars"}),
    "mindmap": frozenset({"enabled", "mmdc_path", "puppeteer_config", "max_nodes", "max_depth",
                          "label_chars", "width", "background", "timeout_seconds"}),
    "storage": frozenset({"lock_stale_hours"}),
}

#: 放错段时给一句明确指路（这些键只在特定段生效）
KEY_SECTION_HINTS: dict[str, str] = {
    "api_key": "[summary] api_key",
    "llm_api_key": "[summary] api_key",
    "cookie": "[account] cookie",
    "base_url": "[summary] base_url",
    "model": "[summary] model",
}


@dataclass
class Config:
    # 账号与目标
    uid: int = 0
    # 输出
    output_dir: Path = field(default_factory=lambda: Path("output"))
    download_images: bool = True
    image_workers: int = 4
    # 范围
    kinds: dict[str, bool] = field(
        default_factory=lambda: {kind: True for kind in ALL_KINDS}
    )
    date_from: date | None = None
    date_to: date | None = None
    latest: int | None = None
    # 请求
    interval_seconds: float = 1.2
    timeout_seconds: float = 20.0
    retries: int = 3
    max_pages: int = 40
    user_agent: str = DEFAULT_USER_AGENT
    # 发现策略
    scan_charging_video_subset: bool = True
    scan_dynamic_secondary_source: bool = True
    probe_video_access: bool = True
    # 视频媒体下载（阶段 2）
    download_media: bool = True
    video_workers: int = 3
    segment_workers: int = 4
    video_quality: str = "1080p"
    video_timeout_seconds: float = 60.0
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    # 文字稿（阶段 2）
    prefer_subtitle: bool = True
    # ASR（阶段 2；默认关闭，未启用不下载模型、不跑识别）
    asr_enabled: bool = False
    asr_model: str = "small"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_language: str = "zh"
    asr_beam_size: int = 5
    asr_keep_audio: bool = False
    # 动态长图（阶段 2）
    render_dynamic_png: bool = True
    render_width: int = 1080
    render_max_height: int = 20000
    render_font: str = ""
    # 总结（阶段 3）
    summary_enabled: bool = True
    summary_base_url: str = ""
    summary_model: str = ""
    summary_client: str = "auto"          # auto | sdk | http（见 summarize.llm）
    summary_prompt_file: str = ""
    summary_chunk_chars: int = 6000
    summary_max_chunks: int = 40
    summary_overlap_chars: int = 200
    summary_temperature: float = 0.2
    summary_max_tokens: int = 2048
    summary_timeout_seconds: float = 120.0
    summary_retries: int = 2
    #: 文字稿少于该字数就不生成总结（需求 3.3.6：文字不足不得编造）
    summary_min_chars: int = 200
    # 思维导图（阶段 3；mmdc = Mermaid CLI，渲染 PNG 用）
    mindmap_enabled: bool = True
    mindmap_mmdc_path: str = ""
    mindmap_puppeteer_config: str = ""
    mindmap_max_nodes: int = 60
    mindmap_max_depth: int = 3
    mindmap_label_chars: int = 24
    mindmap_width: int = 1600
    mindmap_background: str = "white"
    mindmap_timeout_seconds: float = 120.0
    # 存储
    lock_stale_hours: float = 6.0
    # 元信息
    config_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary_configured(self) -> bool:
        """LLM 端点与模型名都已配置（密钥可选：本地端点常不需要）。"""
        return bool(self.summary_base_url.strip() and self.summary_model.strip())

    @property
    def enabled_kinds(self) -> list[str]:
        return [kind for kind in ALL_KINDS if self.kinds.get(kind)]

    @property
    def kind_labels(self) -> str:
        return "、".join(KIND_LABEL[k] for k in self.enabled_kinds)

    def describe_filter(self) -> str:
        parts = []
        if self.date_from or self.date_to:
            start = self.date_from.isoformat() if self.date_from else "最早"
            end = self.date_to.isoformat() if self.date_to else "最新"
            parts.append(f"日期 {start} ~ {end}")
        if self.latest:
            parts.append(f"跨类最新 {self.latest} 条")
        return " + ".join(parts) if parts else "全量（受 max_pages 限制）"


# --------------------------------------------------------------------------- #
# TOML 读取
# --------------------------------------------------------------------------- #
def _read_toml(path: Path) -> dict:
    try:
        with Path(path).open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"配置文件解析失败 {path}：{exc}") from exc


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _section(data: dict, name: str) -> dict:
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置段 [{name}] 必须是表（table）")
    return value


def _warn_unknown_keys(cfg: Config, data: dict, source: str = "") -> None:
    """未知段/键只告警不报错：拼错的键会静默失效，必须让用户看见。

    典型场景：把 LLM 密钥写成 ``[llm] api_key`` 或 ``[account] api_key`` ——
    密钥确实填了，但只有 ``[summary] api_key``（或环境变量）会被读取。
    """
    where = f"{source}：" if source else ""
    for section_name, section in data.items():
        keys = sorted(str(key) for key in section) if isinstance(section, dict) else []
        if section_name not in SECTION_KEYS:
            known_sections = "、".join(f"[{name}]" for name in SECTION_KEYS)
            cfg.warnings.append(
                f"{where}代码不识别的段 [{section_name}]，整段被忽略"
                f"（可用段：{known_sections}）"
            )
            unknown = keys
        else:
            unknown = [key for key in keys if key not in SECTION_KEYS[section_name]]
            if unknown:
                cfg.warnings.append(
                    f"{where}配置段 [{section_name}] 里有代码不识别的键："
                    f"{'、'.join(unknown)}（拼错的键会静默失效）"
                )
        for key in unknown:
            hint = KEY_SECTION_HINTS.get(key)
            if hint and hint.split(" ", 1)[0] != f"[{section_name}]":
                cfg.warnings.append(f"{where}→ {key} 应写成 {hint}（或环境变量）")


def _get_bool(section: dict, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    raise ConfigError(f"配置项 {key} 应为布尔值，收到 {value!r}")


def _get_int(section: dict, key: str, default: int, minimum: int | None = None) -> int:
    value = section.get(key, default)
    if value in (None, ""):
        return default
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 应为整数，收到 {value!r}") from exc
    if minimum is not None and out < minimum:
        raise ConfigError(f"配置项 {key} 不能小于 {minimum}，收到 {out}")
    return out


def _get_float(section: dict, key: str, default: float, minimum: float | None = None) -> float:
    value = section.get(key, default)
    if value in (None, ""):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 应为数字，收到 {value!r}") from exc
    if minimum is not None and out < minimum:
        raise ConfigError(f"配置项 {key} 不能小于 {minimum}，收到 {out}")
    return out


def _get_str(section: dict, key: str, default: str = "") -> str:
    value = section.get(key, default)
    return "" if value is None else str(value).strip()


def _get_date(section: dict, key: str) -> date | None:
    raw = _get_str(section, key)
    if not raw:
        return None
    try:
        return parse_date(raw)
    except ValueError as exc:
        raise ConfigError(f"配置项 {key} 不是合法日期（{exc}）") from exc


def _apply_toml(cfg: Config, data: dict, root: Path) -> None:
    account = _section(data, "account")
    output = _section(data, "output")
    scope = _section(data, "scope")
    filt = _section(data, "filter")
    request = _section(data, "request")
    discovery = _section(data, "discovery")
    transcript = _section(data, "transcript")
    asr = _section(data, "asr")
    download = _section(data, "download")
    render = _section(data, "render")
    summary = _section(data, "summary")
    mindmap = _section(data, "mindmap")
    storage = _section(data, "storage")

    if account:
        cfg.uid = _get_int(account, "uid", cfg.uid, minimum=0) or _get_int(
            account, "mid", cfg.uid, minimum=0
        )
    if output:
        raw_dir = _get_str(output, "dir")
        if raw_dir:
            candidate = Path(raw_dir)
            cfg.output_dir = candidate if candidate.is_absolute() else (root / candidate)
        cfg.download_images = _get_bool(output, "download_images", cfg.download_images)
        cfg.image_workers = _get_int(output, "image_workers", cfg.image_workers, minimum=1)
    if scope:
        for kind in ALL_KINDS:
            if kind in scope:
                cfg.kinds[kind] = _get_bool(scope, kind, cfg.kinds[kind])
    if filt:
        cfg.date_from = _get_date(filt, "from") or cfg.date_from
        cfg.date_to = _get_date(filt, "to") or cfg.date_to
        latest = _get_int(filt, "latest", 0, minimum=0)
        cfg.latest = latest or None
    if request:
        cfg.interval_seconds = _get_float(request, "interval_seconds", cfg.interval_seconds, minimum=0.0)
        cfg.timeout_seconds = _get_float(request, "timeout_seconds", cfg.timeout_seconds, minimum=1.0)
        cfg.retries = _get_int(request, "retries", cfg.retries, minimum=1)
        cfg.max_pages = _get_int(request, "max_pages", cfg.max_pages, minimum=1)
        ua = _get_str(request, "user_agent")
        if ua:
            cfg.user_agent = ua
    if discovery:
        cfg.scan_charging_video_subset = _get_bool(
            discovery, "scan_charging_video_subset", cfg.scan_charging_video_subset
        )
        cfg.scan_dynamic_secondary_source = _get_bool(
            discovery, "scan_dynamic_secondary_source", cfg.scan_dynamic_secondary_source
        )
        cfg.probe_video_access = _get_bool(
            discovery, "probe_video_access", cfg.probe_video_access
        )
    if transcript:
        cfg.prefer_subtitle = _get_bool(transcript, "prefer_subtitle", cfg.prefer_subtitle)
    if asr:
        cfg.asr_enabled = _get_bool(asr, "enabled", cfg.asr_enabled)
        cfg.asr_model = _get_str(asr, "model", cfg.asr_model) or cfg.asr_model
        cfg.asr_device = _get_str(asr, "device", cfg.asr_device) or cfg.asr_device
        cfg.asr_compute_type = _get_str(asr, "compute_type", cfg.asr_compute_type) or cfg.asr_compute_type
        cfg.asr_language = _get_str(asr, "language", cfg.asr_language)
        cfg.asr_beam_size = _get_int(asr, "beam_size", cfg.asr_beam_size, minimum=1)
        cfg.asr_keep_audio = _get_bool(asr, "keep_audio", cfg.asr_keep_audio)
    if download:
        cfg.download_media = _get_bool(download, "media", cfg.download_media)
        cfg.video_workers = _get_int(download, "video_workers", cfg.video_workers, minimum=1)
        cfg.segment_workers = _get_int(download, "segment_workers", cfg.segment_workers, minimum=1)
        cfg.video_quality = _get_str(download, "quality", cfg.video_quality) or cfg.video_quality
        cfg.video_timeout_seconds = _get_float(download, "timeout_seconds",
                                               cfg.video_timeout_seconds, minimum=1.0)
        cfg.ffmpeg_path = _get_str(download, "ffmpeg_path", cfg.ffmpeg_path)
        cfg.ffprobe_path = _get_str(download, "ffprobe_path", cfg.ffprobe_path)
    if render:
        cfg.render_dynamic_png = _get_bool(render, "dynamic_png", cfg.render_dynamic_png)
        cfg.render_width = _get_int(render, "width", cfg.render_width, minimum=320)
        cfg.render_max_height = _get_int(render, "max_height", cfg.render_max_height, minimum=500)
        cfg.render_font = _get_str(render, "font", cfg.render_font)
    if summary:
        cfg.summary_enabled = _get_bool(summary, "enabled", cfg.summary_enabled)
        cfg.summary_base_url = _get_str(summary, "base_url", cfg.summary_base_url)
        cfg.summary_model = _get_str(summary, "model", cfg.summary_model)
        cfg.summary_client = _get_str(summary, "client", cfg.summary_client) or cfg.summary_client
        raw_prompt = _get_str(summary, "prompt_file", cfg.summary_prompt_file)
        if raw_prompt:
            candidate = Path(raw_prompt).expanduser()
            cfg.summary_prompt_file = str(candidate if candidate.is_absolute() else root / candidate)
        cfg.summary_chunk_chars = _get_int(summary, "chunk_chars", cfg.summary_chunk_chars, minimum=500)
        cfg.summary_max_chunks = _get_int(summary, "max_chunks", cfg.summary_max_chunks, minimum=1)
        cfg.summary_overlap_chars = _get_int(summary, "overlap_chars", cfg.summary_overlap_chars,
                                             minimum=0)
        cfg.summary_temperature = _get_float(summary, "temperature", cfg.summary_temperature,
                                             minimum=0.0)
        cfg.summary_max_tokens = _get_int(summary, "max_tokens", cfg.summary_max_tokens, minimum=0)
        cfg.summary_timeout_seconds = _get_float(summary, "timeout_seconds",
                                                 cfg.summary_timeout_seconds, minimum=1.0)
        cfg.summary_retries = _get_int(summary, "retries", cfg.summary_retries, minimum=1)
        cfg.summary_min_chars = _get_int(summary, "min_chars", cfg.summary_min_chars, minimum=0)
    if mindmap:
        cfg.mindmap_enabled = _get_bool(mindmap, "enabled", cfg.mindmap_enabled)
        cfg.mindmap_mmdc_path = _get_str(mindmap, "mmdc_path", cfg.mindmap_mmdc_path)
        raw_puppeteer = _get_str(mindmap, "puppeteer_config", cfg.mindmap_puppeteer_config)
        if raw_puppeteer:
            candidate = Path(raw_puppeteer).expanduser()
            cfg.mindmap_puppeteer_config = str(
                candidate if candidate.is_absolute() else root / candidate
            )
        cfg.mindmap_max_nodes = _get_int(mindmap, "max_nodes", cfg.mindmap_max_nodes, minimum=2)
        cfg.mindmap_max_depth = _get_int(mindmap, "max_depth", cfg.mindmap_max_depth, minimum=1)
        cfg.mindmap_label_chars = _get_int(mindmap, "label_chars", cfg.mindmap_label_chars, minimum=2)
        cfg.mindmap_width = _get_int(mindmap, "width", cfg.mindmap_width, minimum=200)
        cfg.mindmap_background = _get_str(mindmap, "background", cfg.mindmap_background)
        cfg.mindmap_timeout_seconds = _get_float(mindmap, "timeout_seconds",
                                                 cfg.mindmap_timeout_seconds, minimum=1.0)
    if storage:
        cfg.lock_stale_hours = _get_float(storage, "lock_stale_hours", cfg.lock_stale_hours, minimum=0.1)


def _apply_overrides(cfg: Config, overrides: dict) -> None:
    """命令行覆盖：只处理显式提供的项（None 视为未提供）。"""
    simple = {
        "uid": ("uid", int),
        "interval_seconds": ("interval_seconds", float),
        "timeout_seconds": ("timeout_seconds", float),
        "retries": ("retries", int),
        "max_pages": ("max_pages", int),
        "image_workers": ("image_workers", int),
        "video_workers": ("video_workers", int),
        "segment_workers": ("segment_workers", int),
        "video_quality": ("video_quality", str),
        "ffmpeg_path": ("ffmpeg_path", str),
        "ffprobe_path": ("ffprobe_path", str),
        "asr_model": ("asr_model", str),
        "asr_language": ("asr_language", str),
        "summary_base_url": ("summary_base_url", str),
        "summary_model": ("summary_model", str),
        "summary_client": ("summary_client", str),
        "summary_prompt_file": ("summary_prompt_file", str),
        "summary_max_tokens": ("summary_max_tokens", int),
        "summary_timeout_seconds": ("summary_timeout_seconds", float),
        "summary_min_chars": ("summary_min_chars", int),
        "summary_chunk_chars": ("summary_chunk_chars", int),
        "summary_max_chunks": ("summary_max_chunks", int),
        "summary_overlap_chars": ("summary_overlap_chars", int),
        "mindmap_mmdc_path": ("mindmap_mmdc_path", str),
        "mindmap_max_nodes": ("mindmap_max_nodes", int),
        "mindmap_max_depth": ("mindmap_max_depth", int),
        "mindmap_label_chars": ("mindmap_label_chars", int),
        "mindmap_background": ("mindmap_background", str),
        "mindmap_width": ("mindmap_width", int),
        "mindmap_timeout_seconds": ("mindmap_timeout_seconds", float),
    }
    for key, (attr, cast) in simple.items():
        value = overrides.get(key)
        if value is None:
            continue
        try:
            setattr(cfg, attr, cast(value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"参数 --{key.replace('_', '-')} 取值非法：{value!r}") from exc

    if overrides.get("output_dir"):
        cfg.output_dir = Path(str(overrides["output_dir"])).expanduser()
    if overrides.get("download_images") is not None:
        cfg.download_images = bool(overrides["download_images"])
    if overrides.get("download_media") is not None:
        cfg.download_media = bool(overrides["download_media"])
    if overrides.get("render_dynamic_png") is not None:
        cfg.render_dynamic_png = bool(overrides["render_dynamic_png"])
    if overrides.get("prefer_subtitle") is not None:
        cfg.prefer_subtitle = bool(overrides["prefer_subtitle"])
    if overrides.get("asr_enabled") is not None:
        cfg.asr_enabled = bool(overrides["asr_enabled"])
    if overrides.get("summary_enabled") is not None:
        cfg.summary_enabled = bool(overrides["summary_enabled"])
    if overrides.get("mindmap_enabled") is not None:
        cfg.mindmap_enabled = bool(overrides["mindmap_enabled"])
    if overrides.get("date_from") is not None:
        cfg.date_from = overrides["date_from"]
    if overrides.get("date_to") is not None:
        cfg.date_to = overrides["date_to"]
    if overrides.get("latest") is not None:
        cfg.latest = int(overrides["latest"]) or None
    kinds = overrides.get("kinds")
    if kinds is not None:
        selected = {str(k).strip().lower() for k in kinds}
        unknown = selected - set(ALL_KINDS)
        if unknown:
            raise ConfigError(
                f"未知内容类型：{', '.join(sorted(unknown))}（可选 {', '.join(ALL_KINDS)}）"
            )
        cfg.kinds = {kind: (kind in selected) for kind in ALL_KINDS}
    for key in ("summary_base_url", "summary_model", "summary_prompt_file", "summary_client"):
        if overrides.get(key):
            setattr(cfg, key, str(overrides[key]).strip())


def _env_first(*names: str) -> tuple[str, str]:
    """按序取第一个非空环境变量，返回 ``(值, 变量名)``；都没有时返回 ``("", "")``。"""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip(), name
    return "", ""


def _apply_env(cfg: Config) -> None:
    """环境变量可以覆盖输出目录与 LLM 端点（密钥由 credentials 模块处理）。

    每个键都同时接受新前缀 ``BSA_*`` 与旧前缀 ``SUBVIDEO_*``（项目改名兼容，
    旧前缀保留一个版本）；命中哪个名字，告警里就报哪个名字。
    """
    out_dir, _ = _env_first("BSA_OUTPUT_DIR", "SUBVIDEO_OUTPUT_DIR")
    if out_dir:
        cfg.output_dir = Path(out_dir).expanduser()
    base_url, _ = _env_first("BSA_LLM_BASE_URL", "SUBVIDEO_LLM_BASE_URL")
    if base_url:
        cfg.summary_base_url = base_url
    model, _ = _env_first("BSA_LLM_MODEL", "SUBVIDEO_LLM_MODEL")
    if model:
        cfg.summary_model = model
    mmdc, _ = _env_first("BSA_MMDC", "SUBVIDEO_MMDC")
    if mmdc:
        cfg.mindmap_mmdc_path = mmdc
    interval, interval_name = _env_first("BSA_REQUEST_INTERVAL", "SUBVIDEO_REQUEST_INTERVAL")
    if interval:
        try:
            cfg.interval_seconds = float(interval)
        except ValueError:
            cfg.warnings.append(f"环境变量 {interval_name} 取值非法，已忽略：{interval!r}")


def _validate(cfg: Config) -> None:
    if cfg.uid <= 0:
        raise ConfigError("缺少目标 UID：请用 --uid 指定，或在 [account] 段配置 uid")
    if not cfg.enabled_kinds:
        raise ConfigError("内容类型不能全部关闭：至少开启 dynamic / video / article 中的一类")
    if cfg.date_from and cfg.date_to and cfg.date_from > cfg.date_to:
        raise ConfigError(f"起始日期 {cfg.date_from} 晚于结束日期 {cfg.date_to}")
    if cfg.latest is not None and cfg.latest <= 0:
        cfg.latest = None
    if cfg.interval_seconds < 0.5:
        cfg.warnings.append(
            f"请求间隔 {cfg.interval_seconds}s 偏小，易触发 -352 风控（阶段 0 实测 0.9~1.2s 较稳）"
        )
    _validate_summary(cfg)


def _validate_summary(cfg: Config) -> None:
    """总结/导图相关配置：取值合法 + 自定义 prompt 文件真的可用（阶段 3）。"""
    from .summarize.llm import CLIENT_MODES
    from .summarize.prompt import PromptError, load_prompt_spec

    if cfg.summary_client not in CLIENT_MODES:
        raise ConfigError(
            f"[summary] client 取值非法：{cfg.summary_client!r}（可选 {', '.join(CLIENT_MODES)}）"
        )
    if cfg.summary_enabled and cfg.summary_prompt_file:
        try:
            load_prompt_spec(cfg.summary_prompt_file)
        except PromptError as exc:
            raise ConfigError(f"自定义 prompt 不可用：{exc}") from exc
    if cfg.summary_enabled and cfg.summary_configured:
        if not cfg.summary_base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"[summary] base_url 必须以 http:// 或 https:// 开头，收到 {cfg.summary_base_url!r}"
            )


@dataclass
class LoadedConfig:
    config: Config
    credentials: Credentials

    def secrets(self) -> list[str]:
        return self.credentials.secrets()


def candidate_config_files(root: Path, explicit: str | Path | None = None) -> list[Path]:
    """确定要读取的配置文件列表（用户显式指定时不读默认文件）。"""
    root = Path(root)
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise ConfigError(f"指定的配置文件不存在：{path}")
        return [path]
    out = []
    for name in ("config.toml", "config.local.toml"):
        path = root / name
        if path.exists():
            out.append(path)
    return out


def load_config(
    root: Path | None = None,
    config_file: str | Path | None = None,
    overrides: dict | None = None,
) -> LoadedConfig:
    """装配配置与凭据。异常一律抛 :class:`ConfigError`（退出码 2）。"""
    root = Path(root or Path.cwd())
    cfg = Config()
    files = candidate_config_files(root, config_file)
    data: dict = {}
    for path in files:
        overlay = _read_toml(path)
        _warn_unknown_keys(cfg, overlay, source=path.name)
        data = _deep_merge(data, overlay)
    cfg.config_files = list(files)
    if data:
        _apply_toml(cfg, data, root)
    _apply_env(cfg)
    _apply_overrides(cfg, overrides or {})
    # 环境变量/命令行给的相对路径按当前工作目录（root）解析，便于 `check` 提前报错
    for attr in ("summary_prompt_file", "mindmap_puppeteer_config"):
        raw = str(getattr(cfg, attr, "") or "").strip()
        if raw:
            candidate = Path(raw).expanduser()
            setattr(cfg, attr, str(candidate if candidate.is_absolute() else root / candidate))

    # 凭据：TOML 中的 [account] / [summary] 段 + 环境变量 + require.txt
    account = _section(data, "account") if data else {}
    summary = _section(data, "summary") if data else {}
    creds = load_credentials(root=root, toml_sections=[account, summary],
                             uid_override=cfg.uid)
    if creds.uid:
        cfg.uid = creds.uid
    if not cfg.summary_base_url:
        cfg.summary_base_url = _get_str(summary, "base_url")
    if not cfg.summary_model:
        cfg.summary_model = _get_str(summary, "model")

    _validate(cfg)
    return LoadedConfig(config=cfg, credentials=creds)
