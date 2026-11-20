"""Server backup archives.

A backup (or an export, same format) is a ``.zip`` file with:

* ``mcsc-backup.json``: the :class:`BackupManifest`;
* ``server/...``: every file of the server folder (:attr:`MinecraftServer.path`)
  except the excluded patterns.

Archives are written to ``<dest>.partial`` and renamed only once complete, so a
failed or interrupted backup never leaves a half-written ``.zip`` behind.
Extraction validates every entry first (zip-slip, absolute paths, symlinks,
size) and writes nothing if the archive is not valid.

These are the low-level, blocking building blocks; most callers want
:meth:`mcsc.ServerManager.backup` and friends, which also check that the
server is stopped and handle the per-server busy lock and the backups folder.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from mcsc.config import ServerType
from mcsc.exceptions import (
    BackupError,
    InsufficientDiskSpaceError,
    InvalidArchiveError,
    UnsupportedArchiveVersionError,
)

if TYPE_CHECKING:
    from mcsc.servers import MinecraftServer

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
"""Version of the archive layout/manifest written by this library."""

MANIFEST_NAME = "mcsc-backup.json"
FILES_PREFIX = "server/"

DEFAULT_EXCLUDE: tuple[str, ...] = ("logs/", "crash-reports/", "cache/", "*.log")
"""Patterns not included in backups.

A pattern ending in ``/`` excludes every directory with that name (at any
depth); any other pattern is matched against file names (``fnmatch``).
"""

# Already compressed formats: stored as they are, deflating them is slow and saves nothing.
_STORED_SUFFIXES = frozenset(
    {".mca", ".mcc", ".jar", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".dat", ".ogg", ".xz", ".7z"}
)

ProgressCallback = Callable[[int, int], Any]
"""``(files_done, files_total)``. Called from the thread doing the work."""

_FILE_NAME_RE = re.compile(r"^(?P<stamp>\d{8}-\d{6})(?:-\d+)?_(?P<kind>[a-z_]+)\.zip$")


class BackupKind(StrEnum):
    """Why a backup was made."""

    MANUAL = "manual"
    AUTOMATIC = "automatic"
    BEFORE_RESTORE = "before_restore"
    BEFORE_UPGRADE = "before_upgrade"
    EXPORT = "export"


@dataclass
class BackupManifest:
    """Metadata stored in every archive as ``mcsc-backup.json``.

    ``server`` holds the server config without local paths: ``name``,
    ``version``, ``server_type``, ``ram_gb``, ``port``, ``jvm_args``,
    ``java_path`` and the original ``id``.
    """

    kind: BackupKind
    server: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    file_count: int = 0
    size_bytes: int = 0
    """Total uncompressed size of the server files."""
    app_version: str | None = None
    pymcsc_version: str = ""
    format_version: int = FORMAT_VERSION

    @property
    def server_type(self) -> ServerType:
        return ServerType(self.server["server_type"])

    @property
    def version(self) -> str:
        return self.server["version"]

    @property
    def name(self) -> str:
        return self.server["name"]

    @property
    def server_id(self) -> str | None:
        return self.server.get("id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "kind": str(self.kind),
            "app_version": self.app_version,
            "pymcsc_version": self.pymcsc_version,
            "server": self.server,
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> BackupManifest:
        """Parse and validate a manifest; raises :class:`InvalidArchiveError`."""
        if not isinstance(data, dict):
            raise InvalidArchiveError("The backup manifest is not a JSON object")
        try:
            format_version = int(data["format_version"])
        except (KeyError, TypeError, ValueError):
            raise InvalidArchiveError("The backup manifest has no valid format_version") from None
        if format_version > FORMAT_VERSION:
            raise UnsupportedArchiveVersionError(
                f"The archive was created by a newer version of mcsc (format {format_version}, "
                f"supported up to {FORMAT_VERSION})"
            )
        try:
            server = data["server"]
            if not isinstance(server, dict):
                raise TypeError("server must be an object")
            server = dict(server)
            server["name"] = str(server["name"])
            server["version"] = str(server["version"])
            server["server_type"] = int(ServerType(int(server["server_type"])))
            created_at = datetime.fromisoformat(data["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            return cls(
                kind=BackupKind(data["kind"]),
                server=server,
                created_at=created_at,
                file_count=int(data.get("file_count", 0)),
                size_bytes=int(data.get("size_bytes", 0)),
                app_version=data.get("app_version"),
                pymcsc_version=str(data.get("pymcsc_version", "")),
                format_version=format_version,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidArchiveError(f"Invalid backup manifest: {e}") from None

    @classmethod
    def for_server(
        cls, server: MinecraftServer, kind: BackupKind, *, app_version: str | None = None
    ) -> BackupManifest:
        from mcsc import __version__

        config = server.config
        return cls(
            kind=BackupKind(kind),
            server={
                "id": config.id,
                "name": config.name,
                "version": config.version,
                "server_type": int(config.server_type),
                "ram_gb": config.ram_gb,
                "port": config.port,
                "jvm_args": list(config.jvm_args),
                "java_path": config.java_path,
            },
            app_version=app_version,
            pymcsc_version=__version__,
        )


@dataclass(frozen=True)
class Backup:
    """A backup archive in a server's backups folder."""

    path: Path
    server_id: str
    kind: BackupKind
    created_at: datetime
    version: str
    server_type: ServerType
    size_bytes: int
    """Size of the archive on disk."""

    @property
    def id(self) -> str:
        """File name of the archive, unique within the server's backups."""
        return self.path.name

    @classmethod
    def from_archive(cls, path: Path, server_id: str) -> Backup:
        manifest = read_manifest(path)
        return cls(
            path=path,
            server_id=server_id,
            kind=manifest.kind,
            created_at=manifest.created_at,
            version=manifest.version,
            server_type=manifest.server_type,
            size_bytes=path.stat().st_size,
        )


# ------------------------------------------------------------------ helpers


def backup_file_name(kind: BackupKind, created_at: datetime | None = None) -> str:
    """``<YYYYmmdd-HHMMSS>_<kind>.zip`` (UTC)."""
    created_at = (created_at or datetime.now(UTC)).astimezone(UTC)
    return f"{created_at:%Y%m%d-%H%M%S}_{BackupKind(kind)}.zip"


def is_backup_file_name(name: str) -> bool:
    return _FILE_NAME_RE.match(name) is not None


def partial_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".partial")


def is_excluded(relative: PurePosixPath, exclude: Iterable[str]) -> bool:
    """True if a path (relative to the server folder) matches an exclude pattern."""
    for pattern in exclude:
        if pattern.endswith("/"):
            name = pattern.rstrip("/")
            if any(fnmatch.fnmatch(part, name) for part in relative.parts[:-1]):
                return True
        elif fnmatch.fnmatch(relative.name, pattern):
            return True
    return False


def iter_server_files(
    root: Path, exclude: Iterable[str] = DEFAULT_EXCLUDE
) -> Iterator[tuple[Path, PurePosixPath]]:
    """``(absolute path, relative path)`` of every regular file to back up.

    Symlinks are skipped (they could point outside the folder and are refused
    on restore anyway).
    """
    exclude = tuple(exclude)
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_base = PurePosixPath(base.relative_to(root).as_posix())
        kept = []
        for name in sorted(dirnames):
            child = base / name
            if child.is_symlink():
                logger.info("Skipping symlink %s", child)
                continue
            if is_excluded(rel_base / name / "x", exclude):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = base / name
            relative = rel_base / name
            if path.is_symlink() or not path.is_file():
                logger.info("Skipping %s (not a regular file)", path)
                continue
            if is_excluded(relative, exclude):
                continue
            yield path, relative


def check_free_space(folder: Path, needed_bytes: int, what: str = "the operation") -> None:
    """Raise :class:`InsufficientDiskSpaceError` if ``folder``'s disk has less than ``needed_bytes`` free."""
    probe = folder
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < needed_bytes:
        raise InsufficientDiskSpaceError(
            f"Not enough disk space for {what}: {needed_bytes} bytes needed, {free} free"
        )


def folder_size(root: Path, exclude: Iterable[str] = ()) -> tuple[int, int]:
    """``(file count, total bytes)`` of the files under ``root``."""
    count = size = 0
    for path, _ in iter_server_files(root, exclude):
        try:
            size += path.stat().st_size
            count += 1
        except OSError:
            pass
    return count, size


# ------------------------------------------------------------------ archives


def create_archive(
    server: MinecraftServer,
    dest: str | os.PathLike[str],
    *,
    kind: BackupKind = BackupKind.MANUAL,
    exclude: Iterable[str] = DEFAULT_EXCLUDE,
    app_version: str | None = None,
    progress: ProgressCallback | None = None,
) -> BackupManifest:
    """Archive the server folder into ``dest``. Blocking.

    The server should be stopped, so the world on disk is consistent. The
    archive is written to ``dest.partial`` and renamed when complete; an
    existing ``dest`` is replaced.

    Raises :class:`InsufficientDiskSpaceError` before writing anything if the
    destination disk can't hold the uncompressed files.
    """
    dest = Path(dest)
    root = server.path
    if not root.is_dir():
        raise BackupError(f"Server folder {root} does not exist")

    files = list(iter_server_files(root, exclude))
    total_size = 0
    for path, _ in files:
        try:
            total_size += path.stat().st_size
        except OSError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    check_free_space(dest.parent, total_size, "the backup")

    manifest = BackupManifest.for_server(server, kind, app_version=app_version)
    tmp = partial_path(dest)
    done = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
            for path, relative in files:
                compression = (
                    zipfile.ZIP_STORED
                    if path.suffix.lower() in _STORED_SUFFIXES
                    else zipfile.ZIP_DEFLATED
                )
                archive.write(path, FILES_PREFIX + str(relative), compress_type=compression)
                manifest.file_count += 1
                manifest.size_bytes += path.stat().st_size
                done += 1
                if progress is not None:
                    progress(done, len(files))
            archive.writestr(MANIFEST_NAME, json.dumps(manifest.to_dict(), indent=2))
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(
        "Archived server %s into %s (%d files, %d bytes)",
        server.id,
        dest,
        manifest.file_count,
        manifest.size_bytes,
    )
    return manifest


def _open_archive(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except FileNotFoundError:
        raise
    except (zipfile.BadZipFile, OSError) as e:
        raise InvalidArchiveError(f"{path.name} is not a valid zip archive: {e}") from e


def _read_manifest(archive: zipfile.ZipFile) -> BackupManifest:
    try:
        raw = archive.read(MANIFEST_NAME)
    except KeyError:
        raise InvalidArchiveError("The archive has no mcsc-backup.json manifest") from None
    except (zipfile.BadZipFile, OSError) as e:
        raise InvalidArchiveError(f"Can't read the backup manifest: {e}") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise InvalidArchiveError(f"The backup manifest is not valid JSON: {e}") from e
    return BackupManifest.from_dict(data)


def read_manifest(path: str | os.PathLike[str]) -> BackupManifest:
    """Read and validate the manifest of an archive without extracting it."""
    with _open_archive(Path(path)) as archive:
        return _read_manifest(archive)


def archive_contains(path: str | os.PathLike[str], relative: str) -> bytes | None:
    """Content of a server file inside an archive (``None`` if missing). For small files."""
    with _open_archive(Path(path)) as archive:
        try:
            return archive.read(FILES_PREFIX + relative)
        except KeyError:
            return None


def _validate_entries(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Server entries with their relative paths; raises on anything unsafe."""
    entries = []
    for info in archive.infolist():
        name = info.filename
        if name == MANIFEST_NAME:
            continue
        if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise InvalidArchiveError(f"Unsafe path in archive: {name!r}")
        if not name.startswith(FILES_PREFIX):
            raise InvalidArchiveError(f"Unexpected entry in archive: {name!r}")
        relative = PurePosixPath(name[len(FILES_PREFIX) :])
        if any(part in ("..", "") for part in relative.parts) or relative.is_absolute():
            raise InvalidArchiveError(f"Unsafe path in archive: {name!r}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise InvalidArchiveError(f"Symlinks are not allowed in archives: {name!r}")
        if info.is_dir() or not relative.parts:
            continue
        entries.append((info, relative))
    return entries


def extract_archive(
    path: str | os.PathLike[str],
    target_dir: str | os.PathLike[str],
    *,
    progress: ProgressCallback | None = None,
) -> BackupManifest:
    """Validate an archive and extract its server files into ``target_dir``. Blocking.

    ``target_dir`` must not exist (or be empty). Nothing is written if the
    archive is invalid; if extraction fails halfway ``target_dir`` is removed.
    """
    path = Path(path)
    target = Path(target_dir)
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"Target folder {target} is not empty")

    with _open_archive(path) as archive:
        manifest = _read_manifest(archive)
        entries = _validate_entries(archive)
        total_size = sum(info.file_size for info, _ in entries)
        if manifest.size_bytes and total_size > manifest.size_bytes:
            raise InvalidArchiveError("The archive content is larger than its manifest declares")
        check_free_space(target.parent, total_size, "extracting the archive")

        created = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        root = target.resolve()
        try:
            for done, (info, relative) in enumerate(entries, 1):
                dest = (root / Path(*relative.parts)).resolve()
                if not dest.is_relative_to(root) or dest == root:
                    raise InvalidArchiveError(f"Unsafe path in archive: {info.filename!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(dest, "wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
                if progress is not None:
                    progress(done, len(entries))
        except zipfile.BadZipFile as e:
            _cleanup(target, created)
            raise InvalidArchiveError(f"The archive is corrupted: {e}") from e
        except BaseException:
            _cleanup(target, created)
            raise
    logger.info("Extracted %s into %s (%d files)", path, target, len(entries))
    return manifest


def _cleanup(target: Path, created: bool) -> None:
    if created:
        shutil.rmtree(target, ignore_errors=True)
    else:
        for child in target.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
