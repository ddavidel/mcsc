import asyncio
import subprocess
import sys

import pytest

from mcsc import (
    EulaNotAcceptedError,
    ForgeServer,
    ServerConfig,
    ServerNotRunningError,
    ServerRunningError,
    ServerState,
    ServerType,
    UnsupportedVersionError,
)


async def wait_for_state(server, state, timeout=10):
    async def _poll():
        while server.state is not state:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout)


async def test_start_stop_lifecycle(fake_server):
    states, lines = [], []
    fake_server.add_state_listener(lambda srv, old, new: states.append(new))
    fake_server.add_output_listener(lambda srv, line: lines.append(line))

    await fake_server.start()
    assert fake_server.pid is not None
    await wait_for_state(fake_server, ServerState.RUNNING)

    with pytest.raises(ServerRunningError):
        await fake_server.start()

    await fake_server.send_command("say hi")
    returncode = await fake_server.stop(timeout=10)

    assert returncode == 0
    assert fake_server.state is ServerState.STOPPED
    assert fake_server.pid is None
    assert states == [
        ServerState.STARTING,
        ServerState.RUNNING,
        ServerState.STOPPING,
        ServerState.STOPPED,
    ]
    assert "> say hi" in lines
    assert list(fake_server.output) == lines


async def test_async_listener_is_scheduled(fake_server):
    seen = asyncio.Event()

    async def listener(srv, line):
        seen.set()

    fake_server.add_output_listener(listener)
    await fake_server.start()
    await asyncio.wait_for(seen.wait(), 10)
    await fake_server.stop(timeout=10)


async def test_stop_kills_hung_server(fake_server):
    fake_server.extra_args = ["--ignore-stop"]
    await fake_server.start()
    await wait_for_state(fake_server, ServerState.RUNNING)
    await fake_server.stop(timeout=0.5)
    assert fake_server.state is ServerState.STOPPED
    assert fake_server.returncode not in (0, None)


async def test_stop_when_not_running(fake_server):
    with pytest.raises(ServerNotRunningError):
        await fake_server.stop()
    with pytest.raises(ServerNotRunningError):
        await fake_server.send_command("list")


async def test_start_requires_eula(fake_server):
    fake_server.eula_path.unlink()
    with pytest.raises(EulaNotAcceptedError):
        await fake_server.start()
    assert fake_server.state is ServerState.STOPPED


def test_properties_roundtrip(fake_server):
    path = fake_server.server_dir / "server.properties"
    path.write_text("#Minecraft server properties\n#date\nmotd=Hello\nmax-players=20\n")
    props = fake_server.read_properties()
    assert props == {"motd": "Hello", "max-players": "20"}

    props["motd"] = "Changed"
    fake_server.write_properties(props)
    text = path.read_text()
    assert text.startswith("#Minecraft server properties\n#date\n")
    assert "motd=Changed" in text
    assert fake_server.available_config_files() == ["server.properties"]


def test_config_path_cannot_escape(fake_server):
    with pytest.raises(ValueError):
        fake_server.config_file_path("../outside.txt")


def make_forge(tmp_path, version="1.20.6-50.1.39"):
    config = ServerConfig(
        name="Forge", version=version, server_type=ServerType.FORGE, ram_gb=6, path=tmp_path / "f"
    )
    return ForgeServer(config)


def test_forge_command_uses_args_file(tmp_path):
    server = make_forge(tmp_path)
    lib = server.server_dir / "libraries/net/minecraftforge/forge/1.20.6-50.1.39"
    lib.mkdir(parents=True)
    (lib / "win_args.txt").touch()
    (lib / "unix_args.txt").touch()
    (server.server_dir / "user_jvm_args.txt").touch()

    command = server.build_command()
    assert command[:4] == ["java", "@user_jvm_args.txt", "-Xmx6G", "-Xms6G"]
    assert command[-2].startswith("@libraries/net/minecraftforge/forge/1.20.6-50.1.39/")
    assert command[-1] == "nogui"


def test_forge_unsupported_layout(tmp_path):
    server = make_forge(tmp_path, "1.12.2-14.23.5.2860")
    server.server_dir.mkdir(parents=True)
    with pytest.raises(UnsupportedVersionError):
        server.build_command()


async def test_java_runs_without_console_window(fake_server, monkeypatch):
    from mcsc.servers import base
    from mcsc.system import NO_WINDOW_FLAGS

    seen = {}
    original = base.asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(base.asyncio, "create_subprocess_exec", spy)
    await fake_server.start()
    await wait_for_state(fake_server, ServerState.RUNNING)
    await fake_server.stop(timeout=10)

    assert seen["creationflags"] == NO_WINDOW_FLAGS
    if sys.platform == "win32":
        assert NO_WINDOW_FLAGS == subprocess.CREATE_NO_WINDOW
    else:
        assert NO_WINDOW_FLAGS == 0
