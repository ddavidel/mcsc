"""Registry of servers persisted to a JSON file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mcsc.config import ServerConfig, ServerType
from mcsc.exceptions import ServerNotFoundError, ServerRunningError
from mcsc.servers import MinecraftServer, server_from_config
from mcsc.system import suggested_ram_gb
from mcsc.versions import VersionCatalog

logger = logging.getLogger(__name__)


class ServerManager:
    """Creates, loads, persists and deletes servers.

    Every server gets its own folder under ``servers_dir``; their configs are
    stored together in ``registry_file`` (default ``servers_dir/servers.json``).
    The registry also reads the legacy minecraft-server-creator format.
    """

    def __init__(
        self,
        servers_dir: str | os.PathLike[str],
        registry_file: str | os.PathLike[str] | None = None,
        *,
        catalog: VersionCatalog | None = None,
        output_buffer: int = 300,
    ) -> None:
        self.servers_dir = Path(servers_dir)
        self.registry_file = Path(registry_file) if registry_file else self.servers_dir / "servers.json"
        self.catalog = catalog or VersionCatalog()
        self.output_buffer = output_buffer
        self._servers: dict[str, MinecraftServer] = {}

    # -------------------------------------------------------------- access

    def __iter__(self) -> Iterator[MinecraftServer]:
        return iter(list(self._servers.values()))

    def __len__(self) -> int:
        return len(self._servers)

    def __contains__(self, item: object) -> bool:
        server_id = item.id if isinstance(item, MinecraftServer) else item
        return server_id in self._servers

    @property
    def servers(self) -> list[MinecraftServer]:
        return list(self._servers.values())

    @property
    def active_servers(self) -> list[MinecraftServer]:
        """Servers with a running process."""
        return [s for s in self._servers.values() if s.is_active]

    def get(self, server_id: str) -> MinecraftServer:
        try:
            return self._servers[server_id]
        except KeyError:
            raise ServerNotFoundError(f"No server with id {server_id!r}") from None

    def find(self, server_id: str) -> MinecraftServer | None:
        return self._servers.get(server_id)

    def find_by_name(self, name: str) -> MinecraftServer | None:
        return next((s for s in self._servers.values() if s.name == name), None)

    # --------------------------------------------------------- persistence

    def load(self) -> list[MinecraftServer]:
        """Load servers from the registry file. Already loaded servers are kept as they are."""
        self.servers_dir.mkdir(parents=True, exist_ok=True)
        if not self.registry_file.exists():
            return self.servers

        with open(self.registry_file, encoding="utf-8") as file:
            data: dict[str, dict[str, Any]] = json.load(file)

        for key, entry in data.items():
            try:
                config = ServerConfig.from_dict({"id": key, **entry})
                if config.path is None:
                    config.path = self.servers_dir / config.id
                if config.id not in self._servers:
                    self._servers[config.id] = server_from_config(
                        config, output_buffer=self.output_buffer
                    )
            except Exception:  # noqa: BLE001 - one broken entry must not hide the others
                logger.exception("Can't load server %s", key)
        logger.info("Loaded %d servers from %s", len(self._servers), self.registry_file)
        return self.servers

    def save(self) -> None:
        """Write all server configs to the registry file (atomically)."""
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        data = {s.id: s.config.to_dict() for s in self._servers.values()}
        tmp = self.registry_file.with_name(self.registry_file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4)
        os.replace(tmp, self.registry_file)

    # ----------------------------------------------------------- mutations

    def register(self, config: ServerConfig) -> MinecraftServer:
        """Add an already installed server (e.g. imported or migrated) and save."""
        if config.path is None:
            config.path = self.servers_dir / config.id
        if config.id in self._servers:
            raise ValueError(f"Server {config.id!r} already registered")
        server = server_from_config(config, output_buffer=self.output_buffer)
        self._servers[config.id] = server
        self.save()
        return server

    def create(
        self,
        name: str,
        version: str,
        server_type: ServerType = ServerType.VANILLA,
        *,
        accept_eula: bool = False,
        ram_gb: int | None = None,
        **config_options: Any,
    ) -> MinecraftServer:
        """Download, install and register a new server. Blocking.

        ``config_options`` are passed to :class:`ServerConfig` (port, java_path, ...).
        The server can only start once the EULA is accepted, either here or
        later with :meth:`MinecraftServer.accept_eula`.
        """
        name = name.strip()
        if not name:
            raise ValueError("Server name can't be empty")
        if not version:
            raise ValueError("Server version can't be empty")

        server_type = ServerType(server_type)
        url = self.catalog.download_url(server_type, version)
        config = ServerConfig(
            name=name,
            version=version,
            server_type=server_type,
            ram_gb=ram_gb or suggested_ram_gb(),
            **config_options,
        )
        config.path = self.servers_dir / config.id
        server = server_from_config(config, output_buffer=self.output_buffer)

        config.path.mkdir(parents=True)
        try:
            server.install(url, accept_eula=accept_eula)
        except BaseException:
            shutil.rmtree(config.path, ignore_errors=True)
            raise

        self._servers[config.id] = server
        self.save()
        logger.info("Created %r", server)
        return server

    async def create_async(self, *args: Any, **kwargs: Any) -> MinecraftServer:
        """:meth:`create` in a worker thread, so the event loop isn't blocked."""
        return await asyncio.to_thread(self.create, *args, **kwargs)

    def delete(self, server: MinecraftServer | str, *, delete_files: bool = False) -> None:
        """Unregister a stopped server, optionally removing its folder."""
        if isinstance(server, str):
            server = self.get(server)
        if server.is_active:
            raise ServerRunningError(f"Can't delete server {server.name!r} while it's {server.state}")
        if server.id not in self._servers:
            raise ServerNotFoundError(f"No server with id {server.id!r}")

        del self._servers[server.id]
        self.save()
        if delete_files and server.path.exists():
            shutil.rmtree(server.path)
        logger.info("Deleted %r (files removed: %s)", server, delete_files)

    async def stop_all(self, timeout: float | None = 60) -> None:
        """Stop every running server concurrently."""
        servers = self.active_servers
        if servers:
            logger.info("Stopping %d servers", len(servers))
            await asyncio.gather(*(s.stop(timeout) for s in servers), return_exceptions=True)
