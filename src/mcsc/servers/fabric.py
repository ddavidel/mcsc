"""Fabric server."""

from pathlib import Path

from mcsc.config import ServerType
from mcsc.servers.base import MinecraftServer


class FabricServer(MinecraftServer):
    """Fabric server (mods via Fabric Loader).

    The downloaded jar is the Fabric server launcher: it runs like a Vanilla
    jar and, on first start, downloads the Minecraft server and the loader
    libraries into :attr:`server_dir`.
    """

    server_type = ServerType.FABRIC

    @property
    def mods_dir(self) -> Path:
        return self.server_dir / "mods"
