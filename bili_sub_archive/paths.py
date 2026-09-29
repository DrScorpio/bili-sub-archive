"""路径与落盘：目录命名清理、作者目录定位、原子写入、单写者锁。

计划 3.2 节：

- 作者目录 ``<UID>_<清理后的UP名称>``；条目目录
  ``YYYY-MM-DD_<类型>_<清理后的标题或摘要>_<平台ID>``；
- 同一 UID 的名称变化**不应**生成第二个作者目录，以 UID 为定位依据；
- 路径组件截断并清理 Windows 保留字符与保留名称；
- 文件写入临时文件后原子替换；一次运行只允许一个进程写入同一 UID 的目录。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .errors import OutputError
from .models import KIND_LABEL
from .timeutil import now_iso

# --------------------------------------------------------------------------- #
# 名称清理
# --------------------------------------------------------------------------- #
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: 空白/点/下划线的连续串折叠，避免出现 ``____`` 或尾随空格
_SPACE_RE = re.compile(r"[\s\u3000]+")
_DOTS_RE = re.compile(r"\.{2,}")

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
    "conin$", "conout$",
}

#: 目录名整体长度上限（组件级）。Windows 单组件 255，这里留出余量给
#: ``<日期>_<类型>_`` 前后缀与可能的 ``_2`` 去重后缀。
DEFAULT_TITLE_BUDGET = 60


def sanitize_component(text: str, budget: int = DEFAULT_TITLE_BUDGET, fallback: str = "无标题") -> str:
    """把任意文本清理成合法的 Windows 路径组件。"""
    raw = str(text or "")
    # 先把所有空白（含换行/制表/全角空格）折成单空格，避免英文单词被粘在一起
    spaced = _SPACE_RE.sub(" ", raw)
    cleaned = _ILLEGAL_RE.sub("", spaced)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    cleaned = _DOTS_RE.sub(".", cleaned).strip(" .")
    if not cleaned:
        return fallback
    if cleaned.lower() in WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > budget:
        cleaned = cleaned[:budget].rstrip(" .")
    if not cleaned or cleaned.lower() in WINDOWS_RESERVED:
        return fallback
    return cleaned


def author_dir_name(uid: int, name: str) -> str:
    """``<UID>_<清理后的UP名称>``；UID 是定位依据，名称只求可读。"""
    return f"{int(uid)}_{sanitize_component(name, budget=40, fallback='未知UP')}"


def entry_dir_name(date_stamp: str, kind: str, title: str, platform_id: str, budget: int = DEFAULT_TITLE_BUDGET) -> str:
    """``YYYY-MM-DD_<类型>_<标题或摘要>_<平台ID>``。"""
    label = KIND_LABEL.get(kind, kind)
    short = sanitize_component(title, budget=budget, fallback="无标题")
    pid = sanitize_component(platform_id, budget=32, fallback="unknown")
    return f"{date_stamp}_{label}_{short}_{pid}"


def find_existing_author_dir(root: Path, uid: int) -> tuple[Path | None, list[str]]:
    """按 UID 在输出根目录里找已存在的作者目录。

    返回 ``(目录, 警告列表)``。命中多个时选 ``index.json`` 里 uid 匹配且更新时间
    最新的一个，并把其他候选写进警告（计划 3.2：名称变化不生成第二个目录）。
    """
    warnings: list[str] = []
    root = Path(root)
    if not root.exists():
        return None, warnings
    candidates: list[tuple[float, Path]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not child.name.startswith(f"{uid}_"):
            # 目录名被清理后仍以 UID_ 开头；也接受 index.json 里 uid 匹配的目录
            index_file = child / "index.json"
            if not index_file.is_file():
                continue
            try:
                data = json.loads(index_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if int(data.get("uid") or 0) != int(uid):
                continue
            candidates.append((index_file.stat().st_mtime, child))
            continue
        candidates.append(((child / "index.json").stat().st_mtime if (child / "index.json").is_file() else 0.0, child))
    if not candidates:
        return None, warnings
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    chosen = candidates[0][1]
    if len(candidates) > 1:
        others = ", ".join(c.name for _m, c in candidates[1:])
        warnings.append(
            f"UID {uid} 在输出目录下命中多个候选作者目录，选用 {chosen.name}（更新时间最新），"
            f"其余候选：{others}；请人工确认后清理，避免重复归档"
        )
    return chosen, warnings


def resolve_author_dir(root: Path, uid: int, name: str) -> tuple[Path, list[str]]:
    """定位/创建作者目录：已存在则复用（UID 定位），否则新建 ``<UID>_<名称>``。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    existing, warnings = find_existing_author_dir(root, uid)
    if existing is not None:
        return existing, warnings
    target = root / author_dir_name(uid, name)
    if target.exists() and not target.is_dir():
        raise OutputError(f"输出路径被同名文件占用：{target}")
    target.mkdir(parents=True, exist_ok=True)
    return target, warnings


# --------------------------------------------------------------------------- #
# 原子写入
# --------------------------------------------------------------------------- #
def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fsync_enabled() -> bool:
    """``BSA_FSYNC=0`` 可关闭落盘前的强制刷盘（仅供测试/CI 提速，默认开启）。

    旧名 ``SUBVIDEO_FSYNC`` 仍认（项目改名兼容，保留一个版本）。
    """
    raw = os.environ.get("BSA_FSYNC") or os.environ.get("SUBVIDEO_FSYNC", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def atomic_write_bytes(path: Path, blob: bytes) -> Path:
    """先写同目录临时文件再 ``os.replace``，避免中断留下半截文件。

    默认在替换前 ``fsync``（抗掉电）；``BSA_FSYNC=0`` 时跳过刷盘换取速度。
    """
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            if _fsync_enabled():
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, str(text).encode(encoding))


def atomic_write_json(path: Path, obj, indent: int = 2) -> Path:
    text = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=False)
    return atomic_write_text(path, text + "\n")


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def rel_path(path: Path, base: Path) -> str:
    """相对路径（posix 风格），失败时退回绝对路径字符串。"""
    try:
        return Path(path).relative_to(base).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


# --------------------------------------------------------------------------- #
# 单写者锁
# --------------------------------------------------------------------------- #
class DirLock:
    """作者目录级互斥锁（计划 3.2：一次运行只允许一个进程写入同一 UID 的目录）。

    实现为 ``.lock`` 文件的 ``O_CREAT | O_EXCL`` 原子创建；超过
    ``stale_hours`` 未更新的锁视为陈旧锁（上次运行被强杀），允许抢占并在
    日志里明确告警——不做 PID 存活检查，因为 Windows 上 ``os.kill(pid, 0)``
    不可靠。
    """

    def __init__(self, author_dir: Path, stale_hours: float = 6.0, logger=None):
        self.path = Path(author_dir) / ".lock"
        self.stale_hours = float(stale_hours)
        self.logger = logger
        self.acquired = False

    def __enter__(self) -> "DirLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        ensure_dir(self.path.parent)
        payload = {
            "pid": os.getpid(),
            "started_at": now_iso(),
            "host": os.environ.get("COMPUTERNAME", ""),
        }
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for _attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, blob)
                finally:
                    os.close(fd)
                self.acquired = True
                return
            except FileExistsError:
                age_hours = self._age_hours()
                if age_hours is not None and age_hours > self.stale_hours:
                    self._warn(
                        f"发现陈旧写锁（{age_hours:.1f} 小时未更新），判定为上次运行被中断，抢占：{self.path}"
                    )
                    try:
                        self.path.unlink()
                    except OSError as exc:
                        raise OutputError(f"无法清理陈旧写锁 {self.path}：{exc}") from exc
                    continue
                holder = self._read()
                raise OutputError(
                    f"作者目录已被另一次运行占用：{self.path}"
                    f"（pid={holder.get('pid')} started_at={holder.get('started_at')}）。"
                    "若确认没有其他运行在进行，删除该 .lock 文件后重试。"
                )
        raise OutputError(f"无法获取写锁：{self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False

    # -- 内部 -- #
    def _age_hours(self) -> float | None:
        try:
            return (time.time() - self.path.stat().st_mtime) / 3600.0
        except OSError:
            return None

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _warn(self, message: str) -> None:
        if self.logger:
            self.logger.warning(message)
