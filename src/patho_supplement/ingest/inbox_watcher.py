"""结果投递目录监听。

免疫组化室 / 分子实验室把回报落成文件投进结果目录，守护进程负责盯着这个目录。
目录监听有两件必须做对的事：

1. **别读到写了一半的文件。** 回报文件可能还在往里写（先写头、再写结果段）。
   本模块用「大小 + 修改时间连续两次不变」判断文件已落稳，落稳之前不读。
2. **同一个文件不要处理两次。** 处理完成的报文移进 ``processed/`` 归档目录；
   尚未接入的申请侧报文移进 ``deferred/`` 目录（等解析接上后再处理）。
   目录里的文件本身因此成为「未处理」的权威清单 —— 重启后不用另建一套状态。

守护进程不必自建文件事件监听：院内机上的文件系统（常见的是共享存储或 NFS）
对 inotify 不可靠，**轮询 + 落稳判定**是更稳的做法，也更容易在出问题时复现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "InboxWatcher",
    "PROCESSED_DIR_NAME",
    "DEFERRED_DIR_NAME",
]

#: 处理完成的报文归档目录名。
PROCESSED_DIR_NAME = "processed"

#: 尚未处理（如申请侧报文）的归档目录名。
DEFERRED_DIR_NAME = "deferred"


@dataclass(frozen=True)
class _Observation:
    """一次文件状态观测。"""

    size: int
    mtime: float
    stable_since: float


class InboxWatcher:
    """结果投递目录的轮询监听器。

    用法::

        watcher = InboxWatcher(inbox_path, poll_interval=2.0, settle_seconds=0.5)
        for path in watcher.scan():
            ...  # 处理
            watcher.mark_processed(path)
    """

    def __init__(
        self,
        inbox: Path | str,
        *,
        poll_interval: float = 2.0,
        settle_seconds: float = 0.5,
        processed_dir_name: str = PROCESSED_DIR_NAME,
        deferred_dir_name: str = DEFERRED_DIR_NAME,
    ) -> None:
        self.inbox = Path(inbox).expanduser()
        self.poll_interval = float(poll_interval)
        self.settle_seconds = float(settle_seconds)
        self.processed_dir_name = processed_dir_name
        self.deferred_dir_name = deferred_dir_name
        self._observations: dict[Path, _Observation] = {}

    # ------------------------------------------------------------ 目录

    @property
    def processed_dir(self) -> Path:
        return self.inbox / self.processed_dir_name

    @property
    def deferred_dir(self) -> Path:
        return self.inbox / self.deferred_dir_name

    # ------------------------------------------------------------ 扫描

    def candidate_files(self) -> list[Path]:
        """列出待处理的报文文件。

        只取目录**顶层**的普通文件：``processed/``、``deferred/`` 是归档，
        ``*.json`` 是本地基线快照等资料、点开头的文件是写入中的临时文件 ——
        两者都不按报文解析。
        """
        if not self.inbox.is_dir():
            return []
        files: list[Path] = []
        for path in sorted(self.inbox.iterdir()):
            if not path.is_file():
                continue
            if path.name.startswith("."):
                continue
            if path.suffix.lower() == ".json":
                continue
            files.append(path)
        return files

    def scan(self, *, force: bool = False, now: float | None = None) -> list[Path]:
        """扫描目录，返回**已落稳**可以处理的文件。

        落稳判定有两条：

        1. 连续两次观测到的大小与修改时间都没变，且距首次观测已超过
           ``settle_seconds`` —— 文件写完了，正在静静躺着。
        2. 文件自己的**修改时间**距今已超过 ``settle_seconds`` —— 不管观测到
           几次，它已经躺够久了。守护进程重启后目录里的旧文件靠这一条直接
           取走，不必再等一轮。

        ``force=True`` 是**兜底扫描**：清空观测缓存，全部按修改时间重新判定，
        接住守护进程停机期间投进来的文件。
        """
        moment = time.time() if now is None else now
        ready: list[Path] = []
        seen: set[Path] = set()

        if force:
            self._observations.clear()

        for path in self.candidate_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            seen.add(path)
            key = (stat.st_size, stat.st_mtime)
            age = moment - stat.st_mtime
            previous = self._observations.get(path)

            if previous is not None and (previous.size, previous.mtime) == key:
                # 没变：连续观测稳定超过静默期才算落稳。
                if moment - previous.stable_since >= self.settle_seconds:
                    ready.append(path)
                continue

            # 变了，或者第一次看到：重新计时，并按修改时间判断。
            self._observations[path] = _Observation(
                size=stat.st_size, mtime=stat.st_mtime, stable_since=moment
            )
            if age >= self.settle_seconds:
                ready.append(path)

        # 清掉已经不在目录里的文件的观测，避免观测表无限增长。
        for path in [p for p in self._observations if p not in seen]:
            self._observations.pop(path, None)

        return ready

    # ------------------------------------------------------------ 归档

    def mark_processed(self, path: Path | str) -> Path:
        """把处理完成的报文移进 ``processed/``。"""
        return self._archive(Path(path), self.processed_dir)

    def mark_deferred(self, path: Path | str) -> Path:
        """把暂不处理的报文移进 ``deferred/``（如申请侧报文）。"""
        return self._archive(Path(path), self.deferred_dir)

    def _archive(self, path: Path, target_dir: Path) -> Path:
        """移动文件到归档目录；同名文件加序号，不覆盖已有的。"""
        self._observations.pop(path, None)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        index = 1
        while target.exists():
            target = target_dir / f"{path.stem}-{index}{path.suffix}"
            index += 1
        path.replace(target)
        return target

    # ------------------------------------------------------------ 状态

    def pending_count(self) -> int:
        """目录里还没处理的报文数（含尚未落稳的）。"""
        return len(self.candidate_files())
