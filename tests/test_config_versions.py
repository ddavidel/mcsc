from pathlib import Path

import pytest

from mcsc import ServerConfig, ServerType, VersionNotFoundError
from mcsc.versions import version_key


def test_config_roundtrip():
    config = ServerConfig(name="A", version="1.21.4", server_type=ServerType.PAPER, path=Path("x"))
    assert ServerConfig.from_dict(config.to_dict()) == config


def test_config_reads_legacy_entry():
    legacy = {
        "name": "Survival 1.8.9",
        "dedicated_ram": 4,
        "version": "1.8.9",
        "jar_type": 2,
        "address": "default",
        "port": 25565,
        "uuid": "9b116f29-25ba-49cf-a367-958dbe3d4191",
        "folder_path": "C:\\servers\\9b116f29",
    }
    config = ServerConfig.from_dict(legacy)
    assert config.server_type is ServerType.FORGE
    assert config.ram_gb == 4
    assert config.id == legacy["uuid"]
    assert config.path == Path(legacy["folder_path"])
    assert config.address is None


def test_versions_sorted_and_filtered(catalog):
    assert catalog.versions(ServerType.VANILLA) == ["1.21.4", "1.8.9"]
    assert "24w14a" in catalog.versions(ServerType.VANILLA, stable_only=False)
    assert catalog.latest(ServerType.VANILLA) == "1.21.4"


def test_forge_versions_before_1_17_are_dropped(catalog):
    assert catalog.versions(ServerType.FORGE) == ["1.20.6-50.1.39"]


def test_download_url(catalog):
    assert catalog.download_url(ServerType.PAPER, "1.21.4").endswith("paper.jar")
    with pytest.raises(VersionNotFoundError):
        catalog.download_url(ServerType.PAPER, "1.0")


def test_version_key_orders_numerically():
    assert version_key("1.10.2") > version_key("1.9.4")
