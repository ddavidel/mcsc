import json

import pytest

from mcsc import (
    PaperServer,
    ServerManager,
    ServerNotFoundError,
    ServerRunningError,
    ServerType,
    VanillaServer,
    VersionNotFoundError,
)


@pytest.fixture
def manager(tmp_path, catalog):
    return ServerManager(tmp_path / "servers", catalog=catalog)


def test_create_installs_and_persists(manager, no_download):
    server = manager.create("Survival", "1.21.4", ServerType.PAPER, accept_eula=True, ram_gb=3)

    assert isinstance(server, PaperServer)
    assert no_download == ["https://example.invalid/paper.jar"]
    assert server.jar_path.read_bytes() == b"jar"
    assert server.eula_accepted
    assert server in manager

    reloaded = ServerManager(manager.servers_dir, catalog=manager.catalog)
    reloaded.load()
    loaded = reloaded.get(server.id)
    assert loaded.config == server.config


def test_create_without_eula(manager, no_download):
    server = manager.create("NoEula", "1.21.4")
    assert not server.eula_accepted


def test_create_validates_input(manager, no_download):
    with pytest.raises(ValueError):
        manager.create("  ", "1.21.4")
    with pytest.raises(VersionNotFoundError):
        manager.create("X", "0.0.1")
    assert len(manager) == 0
    assert not manager.servers_dir.exists() or not any(manager.servers_dir.iterdir())


def test_failed_install_cleans_up(manager, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("mcsc.servers.base.download_file", boom)
    with pytest.raises(RuntimeError):
        manager.create("X", "1.21.4")
    assert list(manager.servers_dir.iterdir()) == []
    assert len(manager) == 0


async def test_create_async(manager, no_download):
    server = await manager.create_async("Async", "1.21.4")
    assert isinstance(server, VanillaServer)


def test_delete(manager, no_download):
    server = manager.create("Del", "1.21.4")
    manager.delete(server.id, delete_files=True)
    assert server not in manager
    assert not server.path.exists()
    assert json.loads(manager.registry_file.read_text()) == {}
    with pytest.raises(ServerNotFoundError):
        manager.get(server.id)


async def test_delete_running_server_refused(manager, fake_server):
    manager._servers[fake_server.id] = fake_server
    await fake_server.start()
    try:
        with pytest.raises(ServerRunningError):
            manager.delete(fake_server)
    finally:
        await manager.stop_all(timeout=10)
    assert not fake_server.is_active


def test_load_legacy_registry(tmp_path, catalog):
    registry = tmp_path / "config" / "servers.json"
    registry.parent.mkdir()
    registry.write_text(
        json.dumps(
            {
                "abc": {
                    "name": "Old",
                    "dedicated_ram": 4,
                    "version": "1.8.9",
                    "jar_type": 0,
                    "address": "default",
                    "port": 25565,
                    "uuid": "abc",
                    "folder_path": str(tmp_path / "servers" / "abc"),
                },
                "broken": {"name": "missing version"},
            }
        )
    )
    manager = ServerManager(tmp_path / "servers", registry, catalog=catalog)
    servers = manager.load()
    assert [s.name for s in servers] == ["Old"]
    assert manager.get("abc").config.ram_gb == 4
