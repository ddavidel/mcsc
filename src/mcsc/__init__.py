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

from mcsc.config import ServerConfig, ServerState, ServerType
from mcsc.exceptions import (
    DownloadError,
    EulaNotAcceptedError,
    InstallError,
    JavaNotFoundError,
    MCSCError,
    ServerNotFoundError,
    ServerNotRunningError,
    ServerRunningError,
    ServerStateError,
    UnsupportedVersionError,
    VersionNotFoundError,
)
from mcsc.manager import ServerManager
from mcsc.monitor import ProcessMonitor, ResourceUsage
from mcsc.servers import (
    ForgeServer,
    MinecraftServer,
    PaperServer,
    VanillaServer,
    server_from_config,
)
from mcsc.system import suggested_ram_gb, total_ram_gb
from mcsc.versions import VersionCatalog

logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "DownloadError",
    "EulaNotAcceptedError",
    "ForgeServer",
    "InstallError",
    "JavaNotFoundError",
    "MCSCError",
    "MinecraftServer",
    "PaperServer",
    "ProcessMonitor",
    "ResourceUsage",
    "ServerConfig",
    "ServerManager",
    "ServerNotFoundError",
    "ServerNotRunningError",
    "ServerRunningError",
    "ServerState",
    "ServerStateError",
    "ServerType",
    "UnsupportedVersionError",
    "VanillaServer",
    "VersionCatalog",
    "VersionNotFoundError",
    "server_from_config",
    "suggested_ram_gb",
    "total_ram_gb",
]
