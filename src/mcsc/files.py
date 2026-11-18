"""Readers and writers for server configuration files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def read_properties(path: Path) -> dict[str, str]:
    """Parse a Java ``.properties`` file (``key=value`` lines, ``#`` comments)."""
    result: dict[str, str] = {}
    with open(path, encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if not line or line.startswith(("#", "!")):
                continue
            key, sep, value = line.partition("=")
            if sep:
                result[key.strip()] = value.strip()
    return result


def write_properties(path: Path, values: dict[str, Any]) -> None:
    """Write ``values`` as ``key=value`` lines, keeping the file's leading comments."""
    path = Path(path)
    header: list[str] = []
    if path.exists():
        with open(path, encoding="utf-8") as file:
            for line in file:
                if not line.startswith("#"):
                    break
                header.append(line.rstrip("\n"))
    with open(path, "w", encoding="utf-8") as file:
        for line in header:
            file.write(line + "\n")
        for key, value in values.items():
            if isinstance(value, bool):
                value = str(value).lower()
            file.write(f"{key}={value}\n")


def read_yaml(path: Path) -> Any:
    """Load a YAML document (``{}`` for an empty file)."""
    with open(path, encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def write_yaml(path: Path, data: Any) -> None:
    """Dump ``data`` as YAML, preserving key order."""
    with open(path, "w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, sort_keys=False, allow_unicode=True)


def read_config_file(path: Path) -> Any:
    """Read a config file, choosing the format from the extension."""
    suffix = Path(path).suffix.lower()
    if suffix == ".properties":
        return read_properties(path)
    if suffix in (".yml", ".yaml"):
        return read_yaml(path)
    if suffix == ".json":
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    raise ValueError(f"Unsupported config file type: {path}")


def write_config_file(path: Path, data: Any) -> None:
    """Write a config file, choosing the format from the extension."""
    suffix = Path(path).suffix.lower()
    if suffix == ".properties":
        write_properties(path, data)
    elif suffix in (".yml", ".yaml"):
        write_yaml(path, data)
    elif suffix == ".json":
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
    else:
        raise ValueError(f"Unsupported config file type: {path}")
