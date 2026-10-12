"""「这条提醒今天发过没有」的落地记录。

作用只有一个：**幂等**。容器重启、插件重载、或者 APScheduler 因为
``misfire_grace_time`` 补跑一次时，同一条提醒不应该在同一天里发第二遍。

写法刻意保守：文件坏了、目录不可写、磁盘满 —— 一律当作"没有记录"，
打一条 warning 就继续发。提醒能发出去比记录准确重要。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class SentLog:
    """一张 ``{提醒 key: 最近一次发送日期}`` 的小表。"""

    def __init__(self, path: str | os.PathLike[str], logger: object | None = None) -> None:
        """初始化。

        Args:
            path: 落盘路径（JSON 文件）。
            logger: 可选的 logger，用于报告读写失败。
        """
        self.path = Path(path)
        self.logger = logger
        self._data: dict[str, str] = {}
        self._loaded = False

    def _warn(self, message: str, exc: object) -> None:
        """尽力打一条 warning；没有 logger 就静默。"""
        log = self.logger
        if log is None:
            return
        try:
            log.warning(message, exc)
        except Exception:  # noqa: BLE001 - 记日志失败不该影响主流程
            pass

    def load(self) -> dict[str, str]:
        """读取记录；文件不存在或坏掉都返回空表。

        Returns:
            ``{key: 日期}`` 字典。
        """
        if not self._loaded:
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._data = {str(k): str(v) for k, v in data.items() if isinstance(k, str)}
            except FileNotFoundError:
                self._data = {}
            except Exception as exc:  # noqa: BLE001 - 坏了就当没有记录
                self._warn("[定时提醒] 读取发送记录失败：%s", exc)
                self._data = {}
            self._loaded = True
        return self._data

    def get(self, key: str) -> str:
        """返回某条提醒最近一次发送的日期，没发过返回空串。

        Args:
            key: 提醒 key。

        Returns:
            日期字符串（``YYYY-MM-DD``）或空串。
        """
        return self.load().get(str(key), "")

    def mark(self, key: str, stamp: str) -> None:
        """记录某条提醒已在 ``stamp`` 这一天发送。

        Args:
            key: 提醒 key。
            stamp: 日期字符串（``YYYY-MM-DD``）。
        """
        data = dict(self.load())
        data[str(key)] = str(stamp)
        self._data = data
        self._flush()

    def prune(self, keep: int = 200) -> None:
        """丢弃过老的记录，避免文件无限增长。

        记录值是可排序的时间戳字符串（``YYYY-MM-DD HH:MM`` 或 ``YYYY-MM-DD``），
        所以按"最近的 N 个不同时间戳"来保留即可。

        Args:
            keep: 保留最近多少个不同时间戳；``<=0`` 表示不清理。
        """
        if keep <= 0:
            return
        data = self.load()
        stamps = sorted({value for value in data.values() if value})
        if len(stamps) <= keep:
            return
        cutoff = stamps[-keep]
        kept = {k: v for k, v in data.items() if v >= cutoff}
        if kept != data:
            self._data = kept
            self._flush()

    def _flush(self) -> None:
        """原子写回。任何失败都只警告，不抛。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp_name, self.path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001 - 记录失败不能挡住发送
            self._warn("[定时提醒] 写入发送记录失败：%s", exc)
