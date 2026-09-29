"""测试包：使用标准库 ``unittest``（``python -m unittest discover -s tests -t .``）。

测试工作目录不用 ``tempfile``：本仓库所在环境对形如 ``tmp<8位随机>`` 的目录
会拒绝访问，因此统一在 ``.test_tmp/``（已在 .gitignore 中）下自建 ``case_*`` 目录。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

#: 测试期间关闭落盘刷盘（Windows 上每次 fsync 约 50ms，会让测试慢一个数量级）
os.environ.setdefault("BSA_FSYNC", "0")

#: 清空环境变量时保留的键（性能开关，不属于被测配置）
PROTECTED_ENV = frozenset({"BSA_FSYNC"})

#: 仓库根目录（tests/ 的上一级）
ROOT = Path(__file__).resolve().parents[1]
#: 测试工作目录根（不入库）
WORK_ROOT = ROOT / ".test_tmp"


def make_workspace(prefix: str = "case") -> Path:
    """创建一次性测试目录（不使用 tempfile，见模块 docstring）。"""
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORK_ROOT / f"{prefix}_{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def drop_workspace(path: Path) -> None:
    """尽力清理测试目录（失败不影响测试结论）。"""
    shutil.rmtree(path, ignore_errors=True)


class workspace:
    """``with workspace() as tmp:`` —— 替代 ``TemporaryDirectory``。"""

    def __enter__(self) -> Path:
        self.path = make_workspace()
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        drop_workspace(self.path)


def strip_config_env(prefixes: tuple[str, ...] = ("BSA_", "SUBVIDEO_", "BILI_", "OPENAI_")) -> dict:
    """临时移除配置/凭据类环境变量，返回被移除的键值（供 tearDown 还原）。

    含旧前缀 ``SUBVIDEO_``：改名前用户可能已把旧变量写进 shell 配置，
    测试必须连它一起清干净，否则"未配置凭据"这类用例会被真实环境变量污染。
    """
    saved: dict[str, str] = {}
    for key in list(os.environ):
        if key.startswith(prefixes) and key not in PROTECTED_ENV:
            saved[key] = os.environ.pop(key)
    return saved
