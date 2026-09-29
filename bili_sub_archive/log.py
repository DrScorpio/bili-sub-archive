"""日志：统一走脱敏过滤，凭据永远不进日志（计划第 4 节）。

- 默认输出到 stderr，级别 INFO；``-v`` 升到 DEBUG，``--quiet`` 降到 WARNING；
- 所有记录在格式化前经过 :class:`bili_sub_archive.redact.Redactor`；
- 中文输出在 Windows 控制台按 UTF-8 写出（避免 cp936 编码错误）——**stdout 同样处理**，
  否则 ``✔/✘`` 这类符号在重定向到文件时会抛 ``UnicodeEncodeError``；
- 传入 ``ui`` 且该终端可用富文本时，handler 换成 rich 的 ``RichHandler``
  （``markup=False``：日志正文里的 ``[发现]`` 等方括号必须原样保留）。
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "bili_sub_archive"


class RedactFilter(logging.Filter):
    def __init__(self, redactor):
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib 命名
        if self.redactor is None:
            return True
        try:
            message = record.getMessage()
            record.msg = self.redactor.redact(message)
            record.args = ()
        except Exception:  # pragma: no cover - 日志永不因脱敏失败而中断
            pass
        return True


def utf8_stream(stream):
    """把流包成 UTF-8，避免 Windows 默认 cp936 打印中文/符号失败。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    return stream


def configure_stdio() -> None:
    """stdout 与 stderr 统一按 UTF-8 写出。

    原来只处理 stderr：``print_summary`` 往 stdout 写 ``✔/✘``，一旦
    ``bsa sync > out.txt``（cp936 文本流）就会 ``UnicodeEncodeError``。
    ``errors="replace"`` 保证任何终端编码下都不因一个符号把运行打断。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            utf8_stream(stream)


def setup_logger(verbose: int = 0, quiet: bool = False, redactor=None, ui=None) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO))
    logger.propagate = False
    stream = utf8_stream(sys.stderr)
    handler = ui.log_handler(verbose=verbose) if ui is not None else None
    if handler is None:
        handler = logging.StreamHandler(stream=stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactFilter(redactor))
    logger.addHandler(handler)
    return logger


class LogAdapter:
    """给归档器等对象用的最小日志接口（与 stdlib Logger 同形）。"""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def debug(self, message: str) -> None:
        self._logger.debug(message)

    def info(self, message: str) -> None:
        self._logger.info(message)

    def warning(self, message: str) -> None:
        self._logger.warning(message)

    def error(self, message: str) -> None:
        self._logger.error(message)
