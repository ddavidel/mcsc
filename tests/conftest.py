import sys
from pathlib import Path

import pytest

from mcsc import ServerConfig, ServerType, VanillaServer, VersionCatalog

FAKE_SERVER = Path(__file__).with_name("fake_server.py")


class FakeServer(VanillaServer):
    """Runs tests/fake_server.py instead of Java."""

    extra_args: list[str] = []

    def build_command(self) -> list[str]:
        return [sys.executable, "-u", str(FAKE_SERVER), *self.extra_args]


@pytest.fixture
def fake_server(tmp_path) -> FakeServer:
    config = ServerConfig(name="Test", version="1.21.4", path=tmp_path / "srv")
    server = FakeServer(config)
    server.accept_eula()
    return server


@pytest.fixture
def catalog() -> VersionCatalog:
    return VersionCatalog(
        data={
            ServerType.VANILLA: {
                "1.21.4": "https://example.invalid/vanilla-1.21.4.jar",
                "1.8.9": "https://example.invalid/vanilla-1.8.9.jar",
                "24w14a": "https://example.invalid/snapshot.jar",
                "1.21.5-rc1": "https://example.invalid/rc.jar",
            },
            ServerType.PAPER: {"1.21.4": "https://example.invalid/paper.jar"},
            ServerType.FABRIC: {"1.21.4": "https://example.invalid/fabric.jar"},
            ServerType.FORGE: {
                "1.16.5-36.2.39": "https://example.invalid/forge-old.jar",
                "1.20.6-50.1.39": "https://example.invalid/forge-new.jar",
            },
        }
    )


@pytest.fixture
def no_download(monkeypatch):
    """Replace the jar download with writing a dummy file."""
    downloaded: list[str] = []

    def fake_download(url, destination, **_):
        downloaded.append(url)
        Path(destination).write_bytes(b"jar")
        return Path(destination)

    monkeypatch.setattr("mcsc.servers.base.download_file", fake_download)
    monkeypatch.setattr("mcsc.servers.forge.download_file", fake_download)
    return downloaded
