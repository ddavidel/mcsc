"""Catalog of downloadable server versions."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping

import requests

from mcsc.config import ServerType
from mcsc.exceptions import DownloadError, VersionNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_SOURCES: dict[ServerType, str] = {
    ServerType.VANILLA: "https://raw.githubusercontent.com/ddavidel/minecraft-server-jars/refs/heads/main/versions/vanilla_version_list.json",
    ServerType.PAPER: "https://raw.githubusercontent.com/ddavidel/minecraft-server-jars/refs/heads/main/versions/paper_version_list.json",
    ServerType.FORGE: "https://raw.githubusercontent.com/ddavidel/minecraft-forge-links/refs/heads/main/version_list.json",
    ServerType.FABRIC: "https://raw.githubusercontent.com/ddavidel/minecraft-server-jars/refs/heads/main/versions/fabric_version_list.json",
}

# Forge installs older than this use a layout mcsc cannot run.
MIN_FORGE_MC_VERSION = (1, 17)

_STABLE_RE = re.compile(r"^\d+(\.\d+)*(-\d+(\.\d+)*)?$")


def minecraft_version(version: str) -> str:
    """Minecraft part of a version id (``"1.20.6-50.1.39"`` -> ``"1.20.6"``)."""
    return version.split("-", 1)[0]


def version_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted versions. Non-numeric parts sort as 0."""
    parts = re.split(r"[.\-]", version)
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


def is_stable(version: str) -> bool:
    """True for release versions (no snapshot/pre/rc suffix)."""
    return bool(_STABLE_RE.match(version))


class VersionCatalog:
    """Maps ``(server type, version)`` to a download URL.

    Data is fetched lazily from JSON documents shaped ``{version: url}``.
    Pass ``data`` to use a pre-built catalog (no network access).
    """

    def __init__(
        self,
        sources: Mapping[ServerType, str] | None = None,
        data: Mapping[ServerType, Mapping[str, str]] | None = None,
        timeout: float = 10,
    ) -> None:
        self.sources = dict(sources or DEFAULT_SOURCES)
        self.timeout = timeout
        self._data: dict[ServerType, dict[str, str]] = {}
        self._lock = threading.Lock()
        for server_type, versions in (data or {}).items():
            self._data[ServerType(server_type)] = self._filter(ServerType(server_type), versions)

    @staticmethod
    def _filter(server_type: ServerType, versions: Mapping[str, str]) -> dict[str, str]:
        if server_type is ServerType.FORGE:
            return {
                v: url
                for v, url in versions.items()
                if version_key(minecraft_version(v))[:2] >= MIN_FORGE_MC_VERSION
            }
        return dict(versions)

    def _fetch(self, server_type: ServerType) -> dict[str, str]:
        url = self.sources[server_type]
        try:
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            raise DownloadError(f"Can't load {server_type.label} versions from {url}: {e}") from e
        logger.info("Loaded %d %s versions", len(payload), server_type.label)
        return self._filter(server_type, payload)

    def _get(self, server_type: ServerType) -> dict[str, str]:
        server_type = ServerType(server_type)
        with self._lock:
            if server_type not in self._data:
                self._data[server_type] = self._fetch(server_type)
            return self._data[server_type]

    def load(self, *server_types: ServerType) -> None:
        """Eagerly fetch the given types (all known sources if none given)."""
        for server_type in server_types or tuple(self.sources):
            self._get(server_type)

    def refresh(self) -> None:
        """Drop cached data and fetch it again."""
        with self._lock:
            loaded = list(self._data)
            self._data.clear()
        self.load(*loaded)

    def versions(self, server_type: ServerType, *, stable_only: bool = True) -> list[str]:
        """Available versions, newest first."""
        versions = self._get(server_type)
        result = [v for v in versions if not stable_only or is_stable(v)]
        return sorted(result, key=version_key, reverse=True)

    def latest(self, server_type: ServerType, *, stable_only: bool = True) -> str | None:
        """Newest available version, or ``None`` if there are none."""
        versions = self.versions(server_type, stable_only=stable_only)
        return versions[0] if versions else None

    def download_url(self, server_type: ServerType, version: str) -> str:
        """URL of the server jar/installer for ``version``."""
        url = self._get(server_type).get(version)
        if not url:
            raise VersionNotFoundError(
                f"{ServerType(server_type).label} version {version!r} not found"
            )
        return url
