"""Server configuration model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4


class ServerType(IntEnum):
    """Supported server distributions.

    Values match the legacy ``jar_type`` integers used by minecraft-server-creator.
    """

    VANILLA = 0
    PAPER = 1
    FORGE = 2
    FABRIC = 3

    @property
    def label(self) -> str:
        """Display name of the server type."""
        return self.name.capitalize()


class ServerState(StrEnum):
    """Lifecycle state of a server process."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


# Legacy keys (minecraft-server-creator servers.json) -> ServerConfig fields
_LEGACY_KEYS = {
    "jar_type": "server_type",
    "dedicated_ram": "ram_gb",
    "folder_path": "path",
    "uuid": "id",
}


@dataclass
class ServerConfig:
    """Persistent settings of a single server.

    ``path`` is the server's root folder. It is assigned by
    :class:`mcsc.ServerManager` when the server is created.
    """

    name: str
    version: str
    server_type: ServerType = ServerType.VANILLA
    ram_gb: int = 2
    port: int = 25565
    address: str | None = None
    java_path: str = "java"
    jvm_args: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid4()))
    path: Path | None = None

    def __post_init__(self) -> None:
        self.server_type = ServerType(self.server_type)
        self.ram_gb = int(self.ram_gb)
        self.port = int(self.port)
        if self.path is not None:
            self.path = Path(self.path)
        if self.address in ("", "default"):
            self.address = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation."""
        data = asdict(self)
        data["server_type"] = int(self.server_type)
        data["path"] = str(self.path) if self.path is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerConfig:
        """Build a config from :meth:`to_dict` output or a legacy servers.json entry.

        Unknown keys are ignored.
        """
        normalized: dict[str, Any] = {}
        for key, value in data.items():
            normalized.setdefault(_LEGACY_KEYS.get(key, key), value)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in normalized.items() if k in known})
