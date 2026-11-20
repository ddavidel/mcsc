import asyncio
import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from pathlib import PurePosixPath as P

import pytest

from mcsc import (
    BackupKind,
    BackupManifest,
    ForgeServer,
    IncompatibleBackupError,
    InsufficientDiskSpaceError,
    InvalidArchiveError,
    InvalidServerFolderError,
    PaperServer,
    ServerBusyError,
    ServerConfig,
    ServerManager,
    ServerRunningError,
    ServerState,
    ServerType,
    UnsupportedArchiveVersionError,
    VanillaServer,
    create_archive,
    detect_server_folder,
    extract_archive,
    read_manifest,
)
from mcsc.backup import FILES_PREFIX, MANIFEST_NAME, is_excluded



async def wait_for_state(server, state, timeout=10):
    async def _poll():
        while server.state is not state:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout)


EXCLUDED = ("logs/latest.log", "crash-reports/crash.txt", "cache/mojang.jar", "debug.log")


def populate(root: Path) -> None:
    files = {
        "server.jar": b"jar",
        "server.properties": b"motd=hello\n",
        "world/level.dat": os.urandom(256),
        "world/region/r.0.0.mca": os.urandom(4096),
        "plugins/Foo/config.yml": b"a: 1\n",
        "empty.txt": b"",
    }
    for rel in EXCLUDED:
        files[rel] = b"excluded"
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def hashes(root: Path, *, skip_excluded: bool = True) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if skip_excluded and rel in EXCLUDED:
                continue
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@pytest.fixture
def server(fake_server):
    populate(fake_server.path)
    return fake_server


@pytest.fixture
def manager(tmp_path, catalog, server):
    manager = ServerManager(tmp_path / "servers", catalog=catalog, app_version="9.9.9")
    manager._servers[server.id] = server
    return manager


def make_zip(path: Path, entries: dict[str, bytes], manifest: dict | None = None) -> Path:
    if manifest is None:
        manifest = {
            "format_version": 1,
            "created_at": "2026-11-20T10:00:00+00:00",
            "kind": "export",
            "server": {"name": "Evil", "version": "1.21.4", "server_type": 0},
        }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if manifest is not False:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest))
    return path


# ---------------------------------------------------------------- archives


def test_is_excluded():
    assert is_excluded(P("logs/a.txt"), ["logs/"])
    assert is_excluded(P("server/logs/a.gz"), ["logs/"])
    assert is_excluded(P("x.log"), ["*.log"])
    assert not is_excluded(P("logs"), ["logs/"])  # a file named "logs"
    assert not is_excluded(P("world/level.dat"), ["logs/", "*.log"])


def test_archive_round_trip(server, tmp_path):
    dest = tmp_path / "out" / "backup.zip"
    manifest = create_archive(server, dest, kind=BackupKind.MANUAL, app_version="1.0")

    assert dest.is_file()
    assert not dest.with_name("backup.zip.partial").exists()
    expected = hashes(server.path)
    assert manifest.file_count == len(expected)
    assert manifest.server == {
        "id": server.id,
        "name": "Test",
        "version": "1.21.4",
        "server_type": 0,
        "ram_gb": 2,
        "port": 25565,
        "jvm_args": [],
        "java_path": "java",
    }
    with zipfile.ZipFile(dest) as archive:
        names = set(archive.namelist())
        data = json.loads(archive.read(MANIFEST_NAME))
    assert MANIFEST_NAME in names
    assert not any(n.removeprefix(FILES_PREFIX) in EXCLUDED for n in names)
    assert data["app_version"] == "1.0"
    assert data["kind"] == "manual"
    assert data["pymcsc_version"]
    assert "path" not in data["server"]

    target = tmp_path / "restored"
    read_back = extract_archive(dest, target)
    assert read_back.server == manifest.server
    assert hashes(target) == expected


def test_manifest_round_trip():
    manifest = BackupManifest(kind=BackupKind.AUTOMATIC, server={"name": "a", "version": "1", "server_type": 2})
    again = BackupManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert again == manifest
    assert again.server_type is ServerType.FORGE


def test_failed_archive_leaves_nothing(server, tmp_path, monkeypatch):
    calls = 0
    original = zipfile.ZipFile.write

    def flaky(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", flaky)
    dest = tmp_path / "out" / "b.zip"
    with pytest.raises(OSError):
        create_archive(server, dest)
    assert list(dest.parent.iterdir()) == []


def test_not_enough_disk_space(server, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mcsc.backup.shutil.disk_usage", lambda _: type("U", (), {"free": 10})()
    )
    dest = tmp_path / "out" / "b.zip"
    with pytest.raises(InsufficientDiskSpaceError):
        create_archive(server, dest)
    assert list(dest.parent.iterdir()) == []


def _symlink_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("server/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
        archive.writestr(
            MANIFEST_NAME,
            json.dumps(
                {
                    "format_version": 1,
                    "created_at": "2026-11-20T10:00:00+00:00",
                    "kind": "export",
                    "server": {"name": "x", "version": "1", "server_type": 0},
                }
            ),
        )
    return path


@pytest.mark.parametrize(
    "build",
    [
        lambda p: make_zip(p, {"server/../evil.txt": b"x"}),
        lambda p: make_zip(p, {"server/a/../../evil.txt": b"x"}),
        lambda p: make_zip(p, {"/etc/evil": b"x"}),
        lambda p: make_zip(p, {"C:/evil": b"x"}),
        lambda p: make_zip(p, {"server\\..\\evil": b"x"}),
        lambda p: make_zip(p, {"other/file": b"x"}),
        lambda p: make_zip(p, {"server/a": b"x"}, manifest=False),
        lambda p: make_zip(p, {"server/a": b"x"}, manifest={"format_version": 1}),
        lambda p: (p.write_bytes(b"not a zip"), p)[1],
        _symlink_zip,
    ],
)
def test_malicious_or_invalid_archives_rejected(tmp_path, build):
    archive = build(tmp_path / "bad.zip")
    target = tmp_path / "target"
    with pytest.raises(InvalidArchiveError):
        extract_archive(archive, target)
    assert not target.exists()
    assert not (tmp_path / "evil.txt").exists()


def test_newer_format_rejected(tmp_path):
    archive = make_zip(
        tmp_path / "new.zip",
        {},
        manifest={
            "format_version": 99,
            "created_at": "2026-11-20T10:00:00+00:00",
            "kind": "export",
            "server": {"name": "x", "version": "1", "server_type": 0},
        },
    )
    with pytest.raises(UnsupportedArchiveVersionError):
        read_manifest(archive)


def test_corrupted_entry_cleans_up(server, tmp_path):
    dest = tmp_path / "b.zip"
    create_archive(server, dest)
    data = bytearray(dest.read_bytes())
    # Corrupt the region file's stored data (found by its local header name).
    offset = data.find(b"server/world/region/r.0.0.mca") + 100
    data[offset : offset + 50] = b"\0" * 50
    dest.write_bytes(bytes(data))
    target = tmp_path / "target"
    with pytest.raises(InvalidArchiveError):
        extract_archive(dest, target)
    assert not target.exists()


# ------------------------------------------------------------ manager: backup


def test_backup_stopped_server(manager, server):
    backup = manager.backup(server)

    assert backup.path.parent == manager.backups_path(server) == manager.backups_dir / server.id
    assert backup.kind is BackupKind.MANUAL
    assert backup.version == "1.21.4"
    assert backup.server_type is ServerType.VANILLA
    assert backup.size_bytes == backup.path.stat().st_size
    assert read_manifest(backup.path).app_version == "9.9.9"
    assert manager.list_backups(server) == [backup]
    assert server.operation is None


def test_backups_listed_newest_first_and_unique(manager, server):
    first = manager.backup(server)
    second = manager.backup(server, BackupKind.MANUAL)
    assert first.id != second.id
    assert manager.list_backups(server) == [second, first]


def test_list_backups_ignores_broken_and_missing(manager, server):
    assert manager.list_backups(server) == []
    manager.backup(server)
    (manager.backups_path(server) / "20260101-000000_manual.zip").write_bytes(b"junk")
    (manager.backups_path(server) / "notes.txt").write_text("hi")
    assert len(manager.list_backups(server)) == 1


async def test_backup_and_export_refuse_running_server(manager, server, tmp_path):
    await server.start()
    try:
        await wait_for_state(server, ServerState.RUNNING)
        with pytest.raises(ServerRunningError):
            manager.backup(server)
        with pytest.raises(ServerRunningError):
            await manager.backup_async(server)
        with pytest.raises(ServerRunningError):
            manager.export(server, tmp_path / "e.zip")
        with pytest.raises(ServerRunningError):
            await manager.export_async(server, tmp_path / "e.zip")
    finally:
        await server.stop(timeout=10)
    assert manager.list_backups(server) == []
    assert not (tmp_path / "e.zip").exists()
    assert server.operation is None


async def test_backup_and_export_async(manager, server, tmp_path):
    backup = await manager.backup_async(server, BackupKind.AUTOMATIC)
    assert backup.kind is BackupKind.AUTOMATIC
    manifest = await manager.export_async(server, tmp_path / "e.zip")
    assert manifest.kind is BackupKind.EXPORT





async def test_busy_lock_blocks_other_operations(manager, server):
    with manager._hold(server, "backup"):
        assert server.is_busy
        assert manager.busy_servers == [server]
        with pytest.raises(ServerBusyError):
            await server.start()
        with pytest.raises(ServerBusyError):
            manager.backup(server)
        with pytest.raises(ServerBusyError):
            manager.delete(server)
        with pytest.raises(ServerBusyError):
            manager.restore_backup(server, "whatever.zip")
    assert not server.is_busy


async def test_start_blocked_during_async_backup(manager, server, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_progress(done, total):
        if done == 1:
            loop.call_soon_threadsafe(started.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()

    task = asyncio.create_task(manager.backup_async(server, progress=slow_progress))
    await started.wait()
    with pytest.raises(ServerBusyError):
        await server.start()
    release.set()
    await task
    assert not server.is_busy


def test_prune_only_touches_automatic(manager, server):
    manual = manager.backup(server)
    automatic = [manager.backup(server, BackupKind.AUTOMATIC) for _ in range(4)]
    upgrade = manager.backup(server, BackupKind.BEFORE_UPGRADE)

    removed = manager.prune_backups(server, keep=2)

    assert set(removed) == set(automatic[:2])
    remaining = manager.list_backups(server)
    assert manual in remaining and upgrade in remaining
    assert [b for b in remaining if b.kind is BackupKind.AUTOMATIC] == automatic[:1:-1]
    assert manager.prune_backups(server, keep=0) == []


def test_delete_backup(manager, server):
    backup = manager.backup(server)
    manager.delete_backup(backup)
    assert not backup.path.exists()
    assert manager.list_backups(server) == []


def test_delete_server_with_backups(manager, server):
    manager.backup(server)
    manager.delete(server, delete_files=True, delete_backups=True)
    assert not manager.backups_path(server).exists()
    assert not server.path.exists()


def test_delete_server_keeps_backups_by_default(manager, server):
    manager.backup(server)
    manager.delete(server, delete_files=True)
    assert len(manager.list_backups(server.id)) == 1


# ----------------------------------------------------------- manager: restore


def test_restore_is_lossless(manager, server):
    expected = hashes(server.path)
    backup = manager.backup(server)

    (server.path / "server.properties").write_text("motd=changed\n")
    shutil.rmtree(server.path / "world")
    (server.path / "new-file.txt").write_text("new")
    server.config.version = "1.20.1"

    safety = manager.restore_backup(server, backup)

    assert hashes(server.path) == expected
    assert server.config.version == "1.21.4"
    assert safety is not None and safety.kind is BackupKind.BEFORE_RESTORE
    assert safety in manager.list_backups(server)
    assert not server.path.with_name(server.path.name + ".restore").exists()
    assert not server.path.with_name(server.path.name + ".old").exists()
    saved = json.loads(manager.registry_file.read_text())
    assert saved[server.id]["version"] == "1.21.4"


async def test_restore_async(manager, server):
    backup = manager.backup(server)
    (server.path / "server.properties").write_text("motd=changed\n")
    await manager.restore_backup_async(server, backup)
    assert (server.path / "server.properties").read_text() == "motd=hello\n"


async def test_restore_refused_while_running(manager, server):
    backup = manager.backup(server)
    await server.start()
    try:
        with pytest.raises(ServerRunningError):
            manager.restore_backup(server, backup)
        with pytest.raises(ServerRunningError):
            await manager.restore_backup_async(server, backup)
    finally:
        await server.stop(timeout=10)


def test_restore_other_server_type_refused(manager, server, tmp_path):
    other = PaperServer(ServerConfig(name="P", version="1.21.4", server_type=ServerType.PAPER, path=tmp_path / "p"))
    populate(other.path)
    archive = tmp_path / "paper.zip"
    create_archive(other, archive)
    before = hashes(server.path, skip_excluded=False)

    with pytest.raises(IncompatibleBackupError):
        manager.restore_backup(server, archive)
    assert hashes(server.path, skip_excluded=False) == before
    assert manager.list_backups(server) == []


def test_failed_restore_leaves_server_untouched(manager, server, tmp_path):
    archive = make_zip(tmp_path / "bad.zip", {"server/../../evil": b"x"})
    before = hashes(server.path, skip_excluded=False)
    with pytest.raises(InvalidArchiveError):
        manager.restore_backup(server, archive)
    assert hashes(server.path, skip_excluded=False) == before
    assert not server.path.with_name(server.path.name + ".restore").exists()


def test_failed_swap_rolls_back(manager, server, monkeypatch):
    backup = manager.backup(server)
    before = hashes(server.path, skip_excluded=False)
    original = Path.rename

    def rename(self, target):
        if self.name.endswith(".restore"):
            raise OSError("locked")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(Exception, match="replace the server folder"):
        manager.restore_backup(server, backup)
    monkeypatch.undo()
    assert hashes(server.path, skip_excluded=False) == before


# ------------------------------------------------------ export / import


def test_export_import_round_trip(manager, server, tmp_path):
    server.config.jvm_args = ["-XX:+UseG1GC"]
    server.config.ram_gb = 3
    export = tmp_path / "exports" / "my-server.zip"
    manifest = manager.export(server, export)
    assert manifest.kind is BackupKind.EXPORT
    assert export.is_file()
    assert manager.list_backups(server) == []

    info = manager.inspect_archive(export)
    assert info.manifest.name == "Test"
    assert info.suggested_name == "Test (2)"
    assert info.eula_accepted

    imported = manager.import_archive(export, accept_eula=True)

    assert imported.id != server.id
    assert imported.path == manager.servers_dir / imported.id
    assert imported.name == "Test (2)"
    assert imported.config.ram_gb == 3
    assert imported.config.jvm_args == ["-XX:+UseG1GC"]
    assert isinstance(imported, VanillaServer)
    assert imported.eula_accepted
    assert hashes(imported.path) == hashes(server.path)
    assert export.is_file()
    reloaded = ServerManager(manager.servers_dir, catalog=manager.catalog)
    reloaded.load()
    assert reloaded.get(imported.id).config == imported.config


def test_import_overrides_and_eula_not_confirmed(manager, server, tmp_path):
    export = tmp_path / "e.zip"
    manager.export(server, export)
    imported = manager.import_archive(export, name="  Copy  ", ram_gb=5, accept_eula=False)
    assert imported.name == "Copy"
    assert imported.config.ram_gb == 5
    assert not imported.eula_accepted


def test_import_unknown_java_falls_back(manager, server, tmp_path):
    server.config.java_path = str(tmp_path / "no" / "java.exe")
    export = tmp_path / "e.zip"
    manager.export(server, export)
    assert manager.import_archive(export).config.java_path == "java"


def test_import_forge_eula_path(tmp_path, catalog):
    forge = ForgeServer(ServerConfig(name="F", version="1.20.6-50.1.39", server_type=ServerType.FORGE, path=tmp_path / "f"))
    forge.accept_eula()
    (forge.path / "forge-installer.jar").write_bytes(b"x")
    archive = tmp_path / "forge.zip"
    create_archive(forge, archive)
    manager = ServerManager(tmp_path / "servers", catalog=catalog)
    assert manager.inspect_archive(archive).eula_accepted
    imported = manager.import_archive(archive, accept_eula=True)
    assert isinstance(imported, ForgeServer)
    assert (imported.path / "server" / "eula.txt").is_file()


def test_invalid_import_writes_nothing(manager, tmp_path):
    archive = make_zip(tmp_path / "bad.zip", {"server/ok": b"x", "server/../../evil": b"x"})
    with pytest.raises(InvalidArchiveError):
        manager.import_archive(archive)
    assert not manager.servers_dir.exists() or list(manager.servers_dir.iterdir()) == []
    assert len(manager) == 1



async def test_import_async(manager, server, tmp_path):
    export = tmp_path / "e.zip"
    manager.export(server, export)
    imported = await manager.import_archive_async(export, name="Async")
    assert imported in manager


# ---------------------------------------------------------- import folder


def test_detect_and_import_vanilla_folder(manager, tmp_path):
    folder = tmp_path / "external"
    folder.mkdir()
    with zipfile.ZipFile(folder / "minecraft_server.1.21.4.jar", "w") as jar:
        jar.writestr("version.json", json.dumps({"id": "1.21.4"}))
    (folder / "world").mkdir()
    (folder / "world" / "level.dat").write_bytes(b"lvl")

    detected = detect_server_folder(folder)
    assert detected.server_type is ServerType.VANILLA
    assert detected.version == "1.21.4"

    imported = manager.import_folder(folder, detected.server_type, detected.version, name="Ext")
    assert imported.jar_path.is_file()
    assert (imported.path / "world" / "level.dat").read_bytes() == b"lvl"
    assert (folder / "minecraft_server.1.21.4.jar").is_file()  # copied, not moved
    assert not imported.eula_accepted


def test_detect_paper_folder(tmp_path):
    (tmp_path / "server.jar").write_bytes(b"x")
    (tmp_path / "version_history.json").write_text(
        json.dumps({"currentVersion": "git-Paper-123 (MC: 1.21.4)"})
    )
    detected = detect_server_folder(tmp_path)
    assert (detected.server_type, detected.version) == (ServerType.PAPER, "1.21.4")


def test_import_forge_folder(manager, tmp_path):
    folder = tmp_path / "forge"
    (folder / "libraries/net/minecraftforge/forge/1.20.6-50.1.39").mkdir(parents=True)
    (folder / "eula.txt").write_text("eula=true\n")

    detected = detect_server_folder(folder)
    assert (detected.server_type, detected.version) == (ServerType.FORGE, "1.20.6-50.1.39")

    imported = manager.import_folder(
        folder, ServerType.FORGE, detected.version, name="Forge", accept_eula=True
    )
    assert isinstance(imported, ForgeServer)
    assert (imported.server_dir / "libraries").is_dir()
    assert imported.eula_accepted


def test_import_folder_without_jar_refused(manager, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(InvalidServerFolderError):
        manager.import_folder(tmp_path / "empty", ServerType.VANILLA, "1.21.4", name="X")
    with pytest.raises(InvalidServerFolderError):
        manager.import_folder(tmp_path / "empty", ServerType.FORGE, "1.20.6", name="X")
    assert detect_server_folder(tmp_path / "empty").server_type is None


# ----------------------------------------------------------------- cleanup


def test_cleanup_temporary_files(manager, server):
    backups = manager.backups_path(server)
    backups.mkdir(parents=True)
    (backups / "20260101-000000_manual.zip.partial").write_bytes(b"x")
    manager.servers_dir.mkdir(parents=True)
    (manager.servers_dir / "abc.partial").mkdir()
    restore = server.path.with_name(server.path.name + ".restore")
    restore.mkdir()

    removed = manager.cleanup_temporary_files()

    assert len(removed) == 3
    assert not any(p.exists() for p in removed)
    assert server.path.is_dir()


def test_cleanup_recovers_interrupted_swap(manager, server):
    old = server.path.with_name(server.path.name + ".old")
    server.path.rename(old)
    manager.cleanup_temporary_files()
    assert (server.path / "server.properties").is_file()
    assert not old.exists()
