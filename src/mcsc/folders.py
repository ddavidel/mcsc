"""Detect existing Minecraft server folders (for importing servers made outside mcsc)."""

from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from mcsc.config import ServerType
from mcsc.versions import version_key

logger = logging.getLogger(__name__)

FORGE_LIBRARIES = Path("libraries", "net", "minecraftforge", "forge")

_LOG_VERSION_RE = re.compile(r"Starting minecraft server version (\S+)")
_PAPER_HISTORY_RE = re.compile(r"\(MC: ([^)]+)\)")
_PAPER_MARKERS = ("version_history.json", "paper.yml", "config/paper-global.yml")


@dataclass(frozen=True)
class DetectedServer:
    """What :func:`detect_server_folder` found. ``None`` fields could not be detected."""

    server_type: ServerType | None
    version: str | None
    jar: Path | None
    """Server jar (Vanilla/Paper), relative to the folder."""


def find_server_jar(folder: Path) -> Path | None:
    """``server.jar``, or the only ``.jar`` in the folder root."""
    if (folder / "server.jar").is_file():
        return Path("server.jar")
    jars = [p for p in folder.glob("*.jar") if p.is_file()]
    if len(jars) == 1:
        return Path(jars[0].name)
    return None


def _forge_version(folder: Path) -> str | None:
    base = folder / FORGE_LIBRARIES
    if not base.is_dir():
        return None
    versions = sorted((p.name for p in base.iterdir() if p.is_dir()), key=version_key)
    return versions[-1] if versions else None


def _jar_version(jar: Path) -> str | None:
    try:
        with zipfile.ZipFile(jar) as archive:
            data = json.loads(archive.read("version.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    version = data.get("id") or data.get("name")
    return str(version) if version else None


def _paper_version(folder: Path) -> str | None:
    try:
        data = json.loads((folder / "version_history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    match = _PAPER_HISTORY_RE.search(str(data.get("currentVersion", "")))
    return match.group(1) if match else None


def _log_version(folder: Path) -> str | None:
    try:
        with open(folder / "logs" / "latest.log", encoding="utf-8", errors="replace") as file:
            for _, line in zip(range(200), file, strict=False):
                match = _LOG_VERSION_RE.search(line)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def detect_server_folder(folder: str | os.PathLike[str]) -> DetectedServer:
    """Guess type, version and jar of an existing server folder.

    * Forge: a ``libraries/net/minecraftforge/forge/<version>`` folder (Forge 1.17+ layout);
    * Paper: ``version_history.json``, ``paper.yml`` or ``config/paper-global.yml``;
    * Vanilla: any other folder with a server jar.
    """
    folder = Path(folder)
    forge = _forge_version(folder)
    if forge is not None:
        return DetectedServer(ServerType.FORGE, forge, None)

    jar = find_server_jar(folder)
    is_paper = any((folder / marker).is_file() for marker in _PAPER_MARKERS)
    if is_paper:
        version = _paper_version(folder) or _log_version(folder)
        return DetectedServer(ServerType.PAPER, version, jar)
    if jar is not None:
        version = _jar_version(folder / jar) or _log_version(folder)
        return DetectedServer(ServerType.VANILLA, version, jar)
    return DetectedServer(None, _log_version(folder), None)
