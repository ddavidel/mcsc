"""Server types."""

from mcsc.config import ServerConfig, ServerType
from mcsc.servers.base import MinecraftServer, OutputListener, StateListener
from mcsc.servers.forge import ForgeServer
from mcsc.servers.paper import PaperServer
from mcsc.servers.vanilla import VanillaServer

SERVER_CLASSES: dict[ServerType, type[MinecraftServer]] = {
    ServerType.VANILLA: VanillaServer,
    ServerType.PAPER: PaperServer,
    ServerType.FORGE: ForgeServer,
}


def server_from_config(config: ServerConfig, **kwargs) -> MinecraftServer:
    """Instantiate the right :class:`MinecraftServer` subclass for ``config``."""
    return SERVER_CLASSES[config.server_type](config, **kwargs)


__all__ = [
    "SERVER_CLASSES",
    "ForgeServer",
    "MinecraftServer",
    "OutputListener",
    "PaperServer",
    "StateListener",
    "VanillaServer",
    "server_from_config",
]
