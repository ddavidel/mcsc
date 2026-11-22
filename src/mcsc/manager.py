"""Registry of servers persisted to a JSON file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcsc.backup import (
    DEFAULT_EXCLUDE,
    Backup,
    BackupKind,
    BackupManifest,
    ProgressCallback,
    archive_contains,
    backup_file_name,
    check_free_space,
    create_archive,
    extract_archive,
    folder_size,
    is_backup_file_name,
    read_manifest,
)
from mcsc.config import ServerConfig, ServerType
from mcsc.exceptions import (
    BackupError,
    IncompatibleBackupError,
    InvalidArchiveError,
    InvalidServerFolderError,
    ServerBusyError,
    ServerNotFoundError,
    ServerRunningError,
)
from mcsc.folders import FORGE_LIBRARIES, find_server_jar
from mcsc.servers import SERVER_CLASSES, ForgeServer, MinecraftServer, server_from_config
from mcsc.system import suggested_ram_gb
from mcsc.versions import VersionCatalog

logger = logging.getLogger(__name__)

_EULA_TRUE_RE = re.compile(rb"^\s*eula\s*=\s*true\s*$", re.M | re.I)


@dataclass(frozen=True)
class ArchiveInfo:
    """Preview of an archive before importing it (see :meth:`ServerManager.inspect_archive`)."""

    path: Path
    manifest: BackupManifest
    eula_accepted: bool
    """The EULA was accepted in the archived server."""
    suggested_name: str
    """Manifest name, made unique among the registered servers."""
    archive_size: int


class ServerManager:
    """Creates, loads, persists and deletes servers.

    Every server gets its own folder under ``servers_dir``; their configs are
    stored together in ``registry_file`` (default ``servers_dir/servers.json``).
    The registry also reads the legacy minecraft-server-creator format.

    Backups of a server are stored in ``backups_dir/<server id>/`` (default
    ``servers_dir/../backups``), outside the server folders so they are never
    included in later backups. ``app_version`` is written to backup manifests.

    Backup, restore and export require the server to be stopped. Only one
    backup, restore, export or delete may run on a server at a time; while one
    runs the server can't be started (:class:`~mcsc.exceptions.ServerBusyError`).
    """

    def __init__(
        self,
        servers_dir: str | os.PathLike[str],
        registry_file: str | os.PathLike[str] | None = None,
        *,
        catalog: VersionCatalog | None = None,
        output_buffer: int = 300,
        backups_dir: str | os.PathLike[str] | None = None,
        app_version: str | None = None,
        backup_exclude: Iterable[str] = DEFAULT_EXCLUDE,
    ) -> None:
        self.servers_dir = Path(servers_dir)
        self.registry_file = Path(registry_file) if registry_file else self.servers_dir / "servers.json"
        self.catalog = catalog or VersionCatalog()
        self.output_buffer = output_buffer
        self.backups_dir = Path(backups_dir) if backups_dir else self.servers_dir.parent / "backups"
        self.app_version = app_version
        self.backup_exclude = tuple(backup_exclude)
        self._servers: dict[str, MinecraftServer] = {}
        self._lock = threading.Lock()

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

    @property
    def busy_servers(self) -> list[MinecraftServer]:
        """Servers with a backup, restore, export or delete in progress."""
        return [s for s in self._servers.values() if s.is_busy]

    def get(self, server_id: str) -> MinecraftServer:
        try:
            return self._servers[server_id]
        except KeyError:
            raise ServerNotFoundError(f"No server with id {server_id!r}") from None

    def find(self, server_id: str) -> MinecraftServer | None:
        return self._servers.get(server_id)

    def find_by_name(self, name: str) -> MinecraftServer | None:
        return next((s for s in self._servers.values() if s.name == name), None)

    def unique_name(self, name: str) -> str:
        """``name``, or ``name (2)``, ``name (3)``, ... if a server already uses it."""
        name = name.strip()
        used = {s.name for s in self._servers.values()}
        if name not in used:
            return name
        base = re.sub(r" \(\d+\)$", "", name)
        n = 2
        while f"{base} ({n})" in used:
            n += 1
        return f"{base} ({n})"

    def _resolve(self, server: MinecraftServer | str) -> MinecraftServer:
        return self.get(server) if isinstance(server, str) else server

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

    def delete(
        self,
        server: MinecraftServer | str,
        *,
        delete_files: bool = False,
        delete_backups: bool = False,
    ) -> None:
        """Unregister a stopped server, optionally removing its folder and its backups."""
        server = self._resolve(server)
        with self._hold(server, "delete", require_stopped=True):
            if server.id not in self._servers:
                raise ServerNotFoundError(f"No server with id {server.id!r}")
            del self._servers[server.id]
            self.save()
            if delete_files and server.path.exists():
                shutil.rmtree(server.path)
            if delete_backups:
                shutil.rmtree(self.backups_path(server), ignore_errors=True)
        logger.info(
            "Deleted %r (files removed: %s, backups removed: %s)",
            server,
            delete_files,
            delete_backups,
        )

    async def stop_all(self, timeout: float | None = 60) -> None:
        """Stop every running server concurrently."""
        servers = self.active_servers
        if servers:
            logger.info("Stopping %d servers", len(servers))
            await asyncio.gather(*(s.stop(timeout) for s in servers), return_exceptions=True)

    # ------------------------------------------------------------ busy lock

    @contextmanager
    def _hold(
        self, server: MinecraftServer, operation: str, *, require_stopped: bool = False
    ) -> Iterator[None]:
        """Mark ``server`` busy with ``operation`` (one operation per server at a time)."""
        with self._lock:
            if server.operation is not None:
                raise ServerBusyError(
                    f"Server {server.name!r} is busy ({server.operation} in progress)"
                )
            if require_stopped and server.is_active:
                raise ServerRunningError(
                    f"Can't {operation} server {server.name!r} while it's {server.state}"
                )
            server._operation = operation
        try:
            yield
        finally:
            server._operation = None

    # -------------------------------------------------------------- backups

    def backups_path(self, server: MinecraftServer | str) -> Path:
        """Folder holding the backups of a server."""
        server_id = server if isinstance(server, str) else server.id
        return self.backups_dir / server_id

    def _write_backup(
        self,
        server: MinecraftServer,
        kind: BackupKind,
        *,
        progress: ProgressCallback | None = None,
    ) -> Backup:
        folder = self.backups_path(server)
        name = backup_file_name(kind)
        dest = folder / name
        n = 2
        while dest.exists():
            dest = folder / name.replace("_", f"-{n}_", 1)
            n += 1
        create_archive(
            server,
            dest,
            kind=kind,
            exclude=self.backup_exclude,
            app_version=self.app_version,
            progress=progress,
        )
        return Backup.from_archive(dest, server.id)

    def backup(
        self,
        server: MinecraftServer | str,
        kind: BackupKind = BackupKind.MANUAL,
        *,
        progress: ProgressCallback | None = None,
    ) -> Backup:
        """Back up a stopped server into its backups folder. Blocking.

        Raises :class:`ServerRunningError` if the server is not stopped.
        """
        server = self._resolve(server)
        with self._hold(server, "backup", require_stopped=True):
            return self._write_backup(server, BackupKind(kind), progress=progress)

    async def backup_async(
        self,
        server: MinecraftServer | str,
        kind: BackupKind = BackupKind.MANUAL,
        *,
        progress: ProgressCallback | None = None,
    ) -> Backup:
        """:meth:`backup` in a worker thread. ``progress`` is called from that thread."""
        server = self._resolve(server)
        kind = BackupKind(kind)
        with self._hold(server, "backup", require_stopped=True):
            return await asyncio.to_thread(self._write_backup, server, kind, progress=progress)

    def list_backups(self, server: MinecraftServer | str) -> list[Backup]:
        """Backups of a server, newest first. Unreadable archives are skipped (and logged)."""
        server_id = server if isinstance(server, str) else server.id
        folder = self.backups_path(server_id)
        if not folder.is_dir():
            return []
        backups = []
        for path in folder.glob("*.zip"):
            if not is_backup_file_name(path.name):
                continue
            try:
                backups.append(Backup.from_archive(path, server_id))
            except (InvalidArchiveError, OSError) as e:
                logger.warning("Skipping unreadable backup %s: %s", path, e)
        backups.sort(key=lambda b: (b.created_at, b.id), reverse=True)
        return backups

    def delete_backup(self, backup: Backup) -> None:
        """Remove a backup archive."""
        if not backup.path.resolve().is_relative_to(self.backups_dir.resolve()):
            raise ValueError(f"{backup.path} is not in the backups folder")
        server = self.find(backup.server_id)
        if server is None:
            backup.path.unlink(missing_ok=True)
        else:
            with self._hold(server, "delete backup"):
                backup.path.unlink(missing_ok=True)
        logger.info("Deleted backup %s", backup.path)

    def prune_backups(
        self,
        server: MinecraftServer | str,
        keep: int,
        kind: BackupKind = BackupKind.AUTOMATIC,
    ) -> list[Backup]:
        """Delete the oldest backups of ``kind`` beyond the newest ``keep``.

        ``keep <= 0`` means unlimited (nothing is deleted). Backups of other
        kinds are never touched. Returns the deleted backups.
        """
        if keep <= 0:
            return []
        kind = BackupKind(kind)
        removed = [b for b in self.list_backups(server) if b.kind is kind][keep:]
        for backup in removed:
            try:
                backup.path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Can't delete old backup %s", backup.path)
        if removed:
            logger.info("Pruned %d %s backups of %s", len(removed), kind, removed[0].server_id)
        return removed

    # -------------------------------------------------------------- restore

    def _restore(
        self,
        server: MinecraftServer,
        archive: Path,
        progress: ProgressCallback | None,
    ) -> Backup | None:
        manifest = read_manifest(archive)
        if manifest.server_type is not server.config.server_type:
            raise IncompatibleBackupError(
                "This backup belongs to a different server type "
                f"({manifest.server_type.label}, server is {server.config.server_type.label})"
            )
        safety = (
            self._write_backup(server, BackupKind.BEFORE_RESTORE) if server.path.is_dir() else None
        )

        path = server.path
        staging = path.with_name(path.name + ".restore")
        old = path.with_name(path.name + ".old")
        for leftover in (staging, old):
            if leftover.exists():
                shutil.rmtree(leftover)
        extract_archive(archive, staging, progress=progress)
        try:
            if path.exists():
                path.rename(old)
            staging.rename(path)
        except OSError as e:
            if not path.exists() and old.exists():
                old.rename(path)
            shutil.rmtree(staging, ignore_errors=True)
            raise BackupError(f"Can't replace the server folder: {e}") from e
        shutil.rmtree(old, ignore_errors=True)

        server.config.version = manifest.version
        if server.id in self._servers:
            self.save()
        logger.info("Restored %r from %s", server, archive)
        return safety

    def restore_backup(
        self,
        server: MinecraftServer | str,
        backup: Backup | str | os.PathLike[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> Backup | None:
        """Replace a stopped server's files with a backup (or export) archive. Blocking.

        The current state is backed up first (kind ``before_restore``, returned;
        ``None`` if the server folder didn't exist). The archive is extracted
        next to the server folder and swapped in only when complete, so the
        server is left untouched on failure. ``config.version`` is set to the
        archived version. Raises :class:`IncompatibleBackupError` if the archive
        is for another server type.
        """
        server = self._resolve(server)
        archive = backup.path if isinstance(backup, Backup) else Path(backup)
        with self._hold(server, "restore", require_stopped=True):
            return self._restore(server, archive, progress)

    async def restore_backup_async(
        self,
        server: MinecraftServer | str,
        backup: Backup | str | os.PathLike[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> Backup | None:
        """:meth:`restore_backup` in a worker thread."""
        server = self._resolve(server)
        archive = backup.path if isinstance(backup, Backup) else Path(backup)
        with self._hold(server, "restore", require_stopped=True):
            return await asyncio.to_thread(self._restore, server, archive, progress)

    # --------------------------------------------------------------- export

    def _export(
        self,
        server: MinecraftServer,
        dest: Path,
        progress: ProgressCallback | None,
    ) -> BackupManifest:
        return create_archive(
            server,
            dest,
            kind=BackupKind.EXPORT,
            exclude=self.backup_exclude,
            app_version=self.app_version,
            progress=progress,
        )

    def export(
        self,
        server: MinecraftServer | str,
        dest: str | os.PathLike[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> BackupManifest:
        """Export a stopped server to a ``.zip`` at ``dest`` (same format as a backup). Blocking.

        An existing ``dest`` is replaced. Raises :class:`ServerRunningError` if
        the server is not stopped.
        """
        server = self._resolve(server)
        with self._hold(server, "export", require_stopped=True):
            return self._export(server, Path(dest), progress)

    async def export_async(
        self,
        server: MinecraftServer | str,
        dest: str | os.PathLike[str],
        *,
        progress: ProgressCallback | None = None,
    ) -> BackupManifest:
        """:meth:`export` in a worker thread."""
        server = self._resolve(server)
        with self._hold(server, "export", require_stopped=True):
            return await asyncio.to_thread(self._export, server, Path(dest), progress)

    # --------------------------------------------------------------- import

    def inspect_archive(self, archive: str | os.PathLike[str]) -> ArchiveInfo:
        """Read an export/backup without extracting it, to preview an import.

        Raises :class:`InvalidArchiveError` (or a subclass) for invalid archives.
        """
        path = Path(archive)
        manifest = read_manifest(path)
        probe = SERVER_CLASSES[manifest.server_type](
            ServerConfig(name="probe", version=manifest.version, path=Path("_"))
        )
        eula = archive_contains(path, probe.eula_path.relative_to(Path("_")).as_posix())
        return ArchiveInfo(
            path=path,
            manifest=manifest,
            eula_accepted=bool(eula and _EULA_TRUE_RE.search(eula)),
            suggested_name=self.unique_name(manifest.name),
            archive_size=path.stat().st_size,
        )

    @staticmethod
    def _java_path(java_path: Any) -> str:
        """Keep an archived java path only if it exists on this machine."""
        if not isinstance(java_path, str) or not java_path:
            return "java"
        if java_path == "java" or shutil.which(java_path) or Path(java_path).is_file():
            return java_path
        logger.info("Java %r from the archive not found here, using 'java'", java_path)
        return "java"

    def _add_installed(self, config: ServerConfig, accept_eula: bool) -> MinecraftServer:
        server = server_from_config(config, output_buffer=self.output_buffer)
        if accept_eula:
            server.accept_eula()
        elif server.eula_path.is_file():
            server.eula_path.write_text("eula=false\n", encoding="utf-8")
        self._servers[config.id] = server
        self.save()
        return server

    def import_archive(
        self,
        archive: str | os.PathLike[str],
        *,
        name: str | None = None,
        ram_gb: int | None = None,
        accept_eula: bool = False,
        progress: ProgressCallback | None = None,
        **config_options: Any,
    ) -> MinecraftServer:
        """Create a new server (new id and folder) from an export/backup archive. Blocking.

        ``name`` defaults to the archived name made unique; ``ram_gb`` to the
        archived value. The EULA is set to ``accept_eula`` whatever the archive
        says. ``config_options`` override other :class:`ServerConfig` fields.
        The archive is left unchanged and nothing is written if it's invalid.
        """
        path = Path(archive)
        manifest = read_manifest(path)
        archived = manifest.server
        name = (name or "").strip() or self.unique_name(manifest.name)
        options: dict[str, Any] = {
            "port": archived.get("port", 25565),
            "jvm_args": list(archived.get("jvm_args") or []),
            "java_path": self._java_path(archived.get("java_path")),
            **config_options,
        }
        config = ServerConfig(
            name=name,
            version=manifest.version,
            server_type=manifest.server_type,
            ram_gb=ram_gb or archived.get("ram_gb") or suggested_ram_gb(),
            **options,
        )
        config.path = self.servers_dir / config.id
        staging = self.servers_dir / f"{config.id}.partial"
        self.servers_dir.mkdir(parents=True, exist_ok=True)
        extract_archive(path, staging, progress=progress)
        try:
            staging.rename(config.path)
            server = self._add_installed(config, accept_eula)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(config.path, ignore_errors=True)
            self._servers.pop(config.id, None)
            raise
        logger.info("Imported %r from %s", server, path)
        return server

    async def import_archive_async(self, *args: Any, **kwargs: Any) -> MinecraftServer:
        """:meth:`import_archive` in a worker thread."""
        return await asyncio.to_thread(self.import_archive, *args, **kwargs)

    def import_folder(
        self,
        folder: str | os.PathLike[str],
        server_type: ServerType,
        version: str,
        *,
        name: str,
        ram_gb: int | None = None,
        accept_eula: bool = False,
        jar: str | os.PathLike[str] | None = None,
        **config_options: Any,
    ) -> MinecraftServer:
        """Copy (never move) an existing server folder into a new registered server. Blocking.

        Vanilla/Paper/Fabric folders need a server jar: ``jar`` (relative to the
        folder), else ``server.jar`` or the only ``.jar`` in the folder; it is
        renamed to ``server.jar`` in the copy. Forge folders need the 1.17+
        layout (``libraries/net/minecraftforge/forge``). Use
        :func:`mcsc.detect_server_folder` to pre-fill type and version.
        """
        source = Path(folder)
        if not source.is_dir():
            raise InvalidServerFolderError(f"{source} is not a folder")
        name = name.strip()
        if not name:
            raise ValueError("Server name can't be empty")
        if not version:
            raise ValueError("Server version can't be empty")
        server_type = ServerType(server_type)

        jar_path: Path | None = None
        if server_type is ServerType.FORGE:
            if not (source / FORGE_LIBRARIES).is_dir():
                raise InvalidServerFolderError(
                    f"{source} is not a Forge 1.17+ server (no {FORGE_LIBRARIES.as_posix()})"
                )
        else:
            jar_path = Path(jar) if jar else find_server_jar(source)
            if jar_path is None or not (source / jar_path).is_file():
                raise InvalidServerFolderError(f"No server jar found in {source}")

        config = ServerConfig(
            name=name,
            version=version,
            server_type=server_type,
            ram_gb=ram_gb or suggested_ram_gb(),
            **config_options,
        )
        config.path = self.servers_dir / config.id
        staging = self.servers_dir / f"{config.id}.partial"
        _, size = folder_size(source)
        self.servers_dir.mkdir(parents=True, exist_ok=True)
        check_free_space(self.servers_dir, size, "importing the server")

        target = staging / ForgeServer.INSTALL_DIR if server_type is ServerType.FORGE else staging
        try:
            shutil.copytree(source, target, symlinks=False)
            if jar_path is not None and jar_path != Path(MinecraftServer.JAR_NAME):
                (target / jar_path).rename(target / MinecraftServer.JAR_NAME)
            staging.rename(config.path)
            server = self._add_installed(config, accept_eula)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(config.path, ignore_errors=True)
            self._servers.pop(config.id, None)
            raise
        logger.info("Imported %r from folder %s", server, source)
        return server

    async def import_folder_async(self, *args: Any, **kwargs: Any) -> MinecraftServer:
        """:meth:`import_folder` in a worker thread."""
        return await asyncio.to_thread(self.import_folder, *args, **kwargs)

    # -------------------------------------------------------------- cleanup

    def cleanup_temporary_files(self) -> list[Path]:
        """Remove leftovers of interrupted backups, restores and imports.

        Call it at startup, after :meth:`load` and before any operation. A
        server folder lost in the middle of a restore swap is recovered from
        its ``.old`` copy. Returns the removed paths.
        """
        removed: list[Path] = []

        def remove(path: Path) -> None:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(path)
            except OSError:
                logger.exception("Can't remove leftover %s", path)

        if self.backups_dir.is_dir():
            for path in self.backups_dir.glob("*/*.partial"):
                remove(path)
        if self.servers_dir.is_dir():
            for path in self.servers_dir.glob("*.partial"):
                remove(path)
        for server in self._servers.values():
            path = server.path
            staging = path.with_name(path.name + ".restore")
            old = path.with_name(path.name + ".old")
            if staging.exists():
                remove(staging)
            if old.exists():
                if path.exists():
                    remove(old)
                else:
                    logger.warning("Recovering %s from an interrupted restore", path)
                    old.rename(path)
        if removed:
            logger.info("Removed %d leftover temporary files", len(removed))
        return removed
