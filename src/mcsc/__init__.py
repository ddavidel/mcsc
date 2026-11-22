"""mcsc - create, run and manage Minecraft servers.

Quick start::

    import asyncio
    from mcsc import ServerManager, ServerType

    manager = ServerManager("servers")
    manager.load()
    server = manager.create("My server", "1.21.4", ServerType.PAPER, accept_eula=True)

    async def main():
        server.add_output_listener(lambda srv, line: print(line))
        await server.start()
        ...
        await server.stop()

    asyncio.run(main())
"""

import logging

from mcsc.backup import (
    DEFAULT_EXCLUDE,
    FORMAT_VERSION,
    Backup,
    BackupKind,
    BackupManifest,
    create_archive,
    extract_archive,
    read_manifest,
)
from mcsc.config import ServerConfig, ServerState, ServerType
from mcsc.exceptions import (
    BackupError,
    DownloadError,
    EulaNotAcceptedError,
    IncompatibleBackupError,
    InstallError,
    InsufficientDiskSpaceError,
    InvalidArchiveError,
    InvalidServerFolderError,
    JavaNotFoundError,
    MCSCError,
    ServerBusyError,
    ServerNotFoundError,
    ServerNotRunningError,
    ServerRunningError,
    ServerStateError,
    UnsupportedArchiveVersionError,
    UnsupportedVersionError,
    VersionNotFoundError,
)
from mcsc.folders import DetectedServer, detect_server_folder
from mcsc.manager import ArchiveInfo, ServerManager
from mcsc.monitor import ProcessMonitor, ResourceUsage
from mcsc.servers import (
    FabricServer,
    ForgeServer,
    MinecraftServer,
    PaperServer,
    VanillaServer,
    server_from_config,
)
from mcsc.system import suggested_ram_gb, total_ram_gb
from mcsc.versions import VersionCatalog

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "0.2.1"

__all__ = [
    "DEFAULT_EXCLUDE",
    "FORMAT_VERSION",
    "ArchiveInfo",
    "Backup",
    "BackupError",
    "BackupKind",
    "BackupManifest",
    "DetectedServer",
    "DownloadError",
    "EulaNotAcceptedError",
    "FabricServer",
    "ForgeServer",
    "IncompatibleBackupError",
    "InstallError",
    "InsufficientDiskSpaceError",
    "InvalidArchiveError",
    "InvalidServerFolderError",
    "JavaNotFoundError",
    "MCSCError",
    "MinecraftServer",
    "PaperServer",
    "ProcessMonitor",
    "ResourceUsage",
    "ServerBusyError",
    "ServerConfig",
    "ServerManager",
    "ServerNotFoundError",
    "ServerNotRunningError",
    "ServerRunningError",
    "ServerState",
    "ServerStateError",
    "ServerType",
    "UnsupportedArchiveVersionError",
    "UnsupportedVersionError",
    "VanillaServer",
    "VersionCatalog",
    "VersionNotFoundError",
    "create_archive",
    "detect_server_folder",
    "extract_archive",
    "read_manifest",
    "server_from_config",
    "suggested_ram_gb",
    "total_ram_gb",
]
