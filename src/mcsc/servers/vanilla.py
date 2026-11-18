"""Vanilla (Mojang) server."""

from mcsc.config import ServerType
from mcsc.servers.base import MinecraftServer


class VanillaServer(MinecraftServer):
    """Official Mojang server jar."""

    server_type = ServerType.VANILLA
