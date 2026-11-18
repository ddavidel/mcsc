"""Paper server."""

from pathlib import Path

from mcsc.config import ServerType
from mcsc.servers.base import MinecraftServer


class PaperServer(MinecraftServer):
    """PaperMC server (plugins via Bukkit/Spigot/Paper API)."""

    server_type = ServerType.PAPER
    CONFIG_FILES = (
        "server.properties",
        "bukkit.yml",
        "spigot.yml",
        "paper.yml",  # Paper < 1.19
        "config/paper-global.yml",  # Paper >= 1.19
        "config/paper-world-defaults.yml",
    )

    @property
    def plugins_dir(self) -> Path:
        return self.server_dir / "plugins"
