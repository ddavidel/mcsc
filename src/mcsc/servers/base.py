"""Base server model shared by every server type."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from mcsc.config import ServerConfig, ServerState, ServerType
from mcsc.download import download_file
from mcsc.exceptions import (
    EulaNotAcceptedError,
    JavaNotFoundError,
    ServerNotRunningError,
    ServerRunningError,
)
from mcsc.files import read_config_file, write_config_file
from mcsc.monitor import ProcessMonitor

logger = logging.getLogger(__name__)

OutputListener = Callable[["MinecraftServer", str], Any]
"""``(server, line)``; may be sync or async."""

StateListener = Callable[["MinecraftServer", ServerState, ServerState], Any]
"""``(server, old_state, new_state)``; may be sync or async."""

# Printed by Vanilla, Paper and Forge once the world is loaded.
READY_PATTERN = re.compile(r"Done \([\d.,]+s\)!")

_STREAM_LIMIT = 1024 * 1024  # max console line length


class MinecraftServer:
    """A Minecraft server living in its own folder.

    Lifecycle: :meth:`install` once, then :meth:`start` / :meth:`stop` as
    needed. :meth:`start` returns as soon as the process is spawned; use
    :meth:`wait` to wait for it to exit. The state goes
    ``STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED``; ``RUNNING`` is
    reached when the server prints its "Done" line.

    Console output is kept in :attr:`output` (last ``output_buffer`` lines)
    and pushed to output listeners.
    """

    server_type: ClassVar[ServerType]
    JAR_NAME: ClassVar[str] = "server.jar"
    CONFIG_FILES: ClassVar[tuple[str, ...]] = ("server.properties",)
    STOP_COMMAND: ClassVar[str] = "stop"

    def __init__(self, config: ServerConfig, *, output_buffer: int = 300) -> None:
        if config.path is None:
            raise ValueError("ServerConfig.path must be set")
        self.config = config
        self.output: deque[str] = deque(maxlen=output_buffer)
        self.monitor = ProcessMonitor()
        self._state = ServerState.STOPPED
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._exited: asyncio.Event | None = None
        self._returncode: int | None = None
        self._output_listeners: list[OutputListener] = []
        self._state_listeners: list[StateListener] = []

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} {self.name!r} id={self.id} "
            f"version={self.version} state={self.state}>"
        )

    def __str__(self) -> str:
        return self.name

    # ------------------------------------------------------------------ info

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def version(self) -> str:
        return self.config.version

    @property
    def path(self) -> Path:
        """Root folder of the server."""
        assert self.config.path is not None
        return self.config.path

    @property
    def server_dir(self) -> Path:
        """Folder the server runs in (where ``server.properties`` lives)."""
        return self.path

    @property
    def jar_path(self) -> Path:
        return self.server_dir / self.JAR_NAME

    @property
    def socket_address(self) -> str:
        return f"{self.config.address or '0.0.0.0'}:{self.config.port}"

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def is_active(self) -> bool:
        """True while a server process exists (any state but ``STOPPED``)."""
        return self._state is not ServerState.STOPPED

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def returncode(self) -> int | None:
        """Exit code of the last run, ``None`` if it never ran or is still running."""
        return self._returncode

    # ------------------------------------------------------------ listeners

    def add_output_listener(self, listener: OutputListener) -> None:
        self._output_listeners.append(listener)

    def remove_output_listener(self, listener: OutputListener) -> None:
        self._output_listeners.remove(listener)

    def add_state_listener(self, listener: StateListener) -> None:
        self._state_listeners.append(listener)

    def remove_state_listener(self, listener: StateListener) -> None:
        self._state_listeners.remove(listener)

    def _notify(self, listeners: list[Callable[..., Any]], *args: Any) -> None:
        for listener in list(listeners):
            try:
                result = listener(self, *args)
                if inspect.isawaitable(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001 - a listener must not break the server
                logger.exception("Listener %r failed", listener)

    def _set_state(self, state: ServerState) -> None:
        old, self._state = self._state, state
        if old is not state:
            logger.info("Server %s: %s -> %s", self.id, old, state)
            self._notify(self._state_listeners, old, state)

    # ---------------------------------------------------------- installation

    @property
    def eula_path(self) -> Path:
        return self.server_dir / "eula.txt"

    @property
    def eula_accepted(self) -> bool:
        try:
            text = self.eula_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        return re.search(r"^\s*eula\s*=\s*true\s*$", text, re.M | re.I) is not None

    def accept_eula(self) -> None:
        """Accept the Minecraft EULA (https://aka.ms/MinecraftEULA)."""
        self.server_dir.mkdir(parents=True, exist_ok=True)
        self.eula_path.write_text("eula=true\n", encoding="utf-8")

    def install(self, download_url: str, *, accept_eula: bool = False) -> None:
        """Download and set up the server files. Blocking.

        Prefer :meth:`mcsc.ServerManager.create`, which also registers the server.
        """
        logger.info("Installing %r into %s", self, self.path)
        self.server_dir.mkdir(parents=True, exist_ok=True)
        download_file(download_url, self.jar_path)
        if accept_eula:
            self.accept_eula()

    # ------------------------------------------------------------- lifecycle

    def build_command(self) -> list[str]:
        """Command line that launches the server."""
        ram = self.config.ram_gb
        return [
            self.config.java_path,
            f"-Xmx{ram}G",
            f"-Xms{ram}G",
            *self.config.jvm_args,
            "-jar",
            str(self.jar_path),
            "nogui",
        ]

    async def start(self) -> None:
        """Spawn the server process and return immediately."""
        if self.is_active:
            raise ServerRunningError(f"Server {self.name!r} is already {self.state}")
        if not self.eula_accepted:
            raise EulaNotAcceptedError(f"EULA not accepted for server {self.name!r}")

        command = self.build_command()
        self.output.clear()
        self._returncode = None
        self._exited = asyncio.Event()
        self._set_state(ServerState.STARTING)
        logger.info("Starting %r: %s", self, command)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.server_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=_STREAM_LIMIT,
            )
        except FileNotFoundError as e:
            self._finish(None)
            raise JavaNotFoundError(f"Java executable not found: {command[0]!r}") from e
        except BaseException:
            self._finish(None)
            raise

        reader = asyncio.create_task(self._read_output(self._process))
        self._tasks = [
            reader,
            asyncio.create_task(self.monitor.run(self._process.pid)),
            asyncio.create_task(self._watch(self._process, reader)),
        ]

    async def _read_output(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            try:
                raw = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # line too long: drop it
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self.output.append(line)
            if self._state is ServerState.STARTING and READY_PATTERN.search(line):
                self._set_state(ServerState.RUNNING)
            self._notify(self._output_listeners, line)

    async def _watch(self, process: asyncio.subprocess.Process, reader: asyncio.Task) -> None:
        returncode = await process.wait()
        await asyncio.gather(reader, return_exceptions=True)
        logger.info("Server %s exited with code %s", self.id, returncode)
        self._finish(returncode)

    def _finish(self, returncode: int | None) -> None:
        self._returncode = returncode
        self._process = None
        self.monitor.stop()
        self._set_state(ServerState.STOPPED)
        if self._exited is not None:
            self._exited.set()

    async def wait(self) -> int | None:
        """Wait for the server process to exit; returns its exit code."""
        if self._exited is not None:
            await self._exited.wait()
        return self._returncode

    async def send_command(self, command: str) -> None:
        """Write a console command to the server."""
        process = self._process
        if process is None or process.stdin is None:
            raise ServerNotRunningError(f"Server {self.name!r} is not running")
        command = command.strip()
        if command == self.STOP_COMMAND:
            self._set_state(ServerState.STOPPING)
        process.stdin.write((command + "\n").encode())
        await process.stdin.drain()

    async def stop(self, timeout: float | None = 60) -> int | None:
        """Gracefully stop the server, killing it if it doesn't exit within ``timeout`` seconds."""
        if not self.is_active:
            raise ServerNotRunningError(f"Server {self.name!r} is not running")
        if self._state is not ServerState.STOPPING:
            try:
                await self.send_command(self.STOP_COMMAND)
            except (ConnectionError, ServerNotRunningError):
                pass  # already exiting
        try:
            return await asyncio.wait_for(self.wait(), timeout)
        except TimeoutError:
            logger.warning("Server %s did not stop within %ss, killing it", self.id, timeout)
            return await self.kill()

    async def kill(self) -> int | None:
        """Terminate the server process immediately."""
        process = self._process
        if process is None:
            raise ServerNotRunningError(f"Server {self.name!r} is not running")
        self._set_state(ServerState.STOPPING)
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return await self.wait()

    async def restart(self, timeout: float | None = 60) -> None:
        if self.is_active:
            await self.stop(timeout)
        await self.start()

    # --------------------------------------------------------- config files

    def config_file_path(self, name: str) -> Path:
        """Absolute path of a file inside :attr:`server_dir`."""
        root = self.server_dir.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"{name!r} is outside the server folder")
        return path

    def available_config_files(self) -> list[str]:
        """Editable config files (from :attr:`CONFIG_FILES`) that currently exist."""
        return [name for name in self.CONFIG_FILES if self.config_file_path(name).is_file()]

    def read_config(self, name: str) -> Any:
        """Read a config file; format chosen by extension (.properties, .yml, .json)."""
        return read_config_file(self.config_file_path(name))

    def write_config(self, name: str, data: Any) -> None:
        write_config_file(self.config_file_path(name), data)
        logger.info("Saved %s for server %s", name, self.id)

    @property
    def has_server_properties(self) -> bool:
        return self.config_file_path("server.properties").is_file()

    def read_properties(self) -> dict[str, str]:
        """``server.properties`` as a dict (``{}`` if not generated yet)."""
        if not self.has_server_properties:
            return {}
        return self.read_config("server.properties")

    def write_properties(self, values: dict[str, Any]) -> None:
        self.write_config("server.properties", values)
