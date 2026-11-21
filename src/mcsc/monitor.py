"""Resource usage monitoring for server processes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


@dataclass(frozen=True)
class ResourceUsage:
    """Resource usage of a process tree over the last sampling interval."""

    ram_mb: float = 0.0
    cpu_percent: float = 0.0
    disk_read_mb: float = 0.0
    disk_write_mb: float = 0.0


class ProcessMonitor:
    """Periodically samples RAM/CPU/disk usage of a process and its children.

    Read :attr:`usage` for the latest sample; it resets to zeros when the
    monitor stops.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.usage = ResourceUsage()
        self.active = False
        self._procs: dict[int, psutil.Process] = {}
        self._prev_io: tuple[int, int] | None = None
        # Bumped by every run() and stop(); a run() only acts while its own
        # generation is still the current one.
        self._generation = 0

    def __repr__(self) -> str:
        return f"ProcessMonitor(active={self.active}, usage={self.usage})"

    def _tree(self, root: psutil.Process) -> list[psutil.Process]:
        # Reuse Process objects so cpu_percent() measures since the previous sample.
        current = [root, *root.children(recursive=True)]
        tree = []
        for proc in current:
            tree.append(self._procs.setdefault(proc.pid, proc))
        alive = {p.pid for p in current}
        for pid in list(self._procs):
            if pid not in alive:
                del self._procs[pid]
        return tree

    def _sample(self, root: psutil.Process) -> ResourceUsage:
        ram = cpu = 0.0
        read = write = 0
        for proc in self._tree(root):
            try:
                with proc.oneshot():
                    ram += proc.memory_info().rss
                    cpu += proc.cpu_percent(interval=None)
                    try:
                        io = proc.io_counters()
                        read += io.read_bytes
                        write += io.write_bytes
                    except (AttributeError, psutil.AccessDenied):
                        pass  # io_counters is unavailable on macOS
            except psutil.NoSuchProcess:
                continue

        prev_read, prev_write = self._prev_io or (read, write)
        self._prev_io = (read, write)
        return ResourceUsage(
            ram_mb=round(ram / _MB, 2),
            cpu_percent=round(cpu, 2),
            disk_read_mb=round(max(read - prev_read, 0) / _MB, 2),
            disk_write_mb=round(max(write - prev_write, 0) / _MB, 2),
        )

    async def run(self, pid: int) -> None:
        """Sample process ``pid`` every :attr:`interval` seconds until it exits or :meth:`stop` is called.

        A new call always takes over: any previous :meth:`run` still pending
        becomes stale and exits at its next wake-up without touching the
        state of the newer run.
        """
        self._generation += 1
        generation = self._generation
        self._reset()
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        self.active = True
        try:
            while self._generation == generation and root.is_running():
                self.usage = self._sample(root)
                await asyncio.sleep(self.interval)
        except psutil.NoSuchProcess:
            pass
        except Exception:  # noqa: BLE001 - monitoring must never crash the server task
            logger.exception("Error monitoring process %s", pid)
        finally:
            # Only the current run may clean up: a stale one must not stop a newer run.
            if self._generation == generation:
                self.stop()

    def stop(self) -> None:
        """Stop sampling and reset :attr:`usage`."""
        self._generation += 1  # any pending run() is now stale
        self._reset()

    def _reset(self) -> None:
        self.active = False
        self.usage = ResourceUsage()
        self._procs.clear()
        self._prev_io = None
