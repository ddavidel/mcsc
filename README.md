# mcsc

Python library to create, run and manage Minecraft servers (Vanilla, Paper, Forge 1.17+).
It is the server-management core of [Minecraft Server Creator](https://github.com/ddavidel/minecraft-server-creator),
with no GUI dependency: no NiceGUI, no translations, no telemetry. Hook those up in the app
through listeners and exceptions.

Requires Python 3.12+ and Java on the machine that runs the servers.

## Install

```bash
pip install pymcsc
```

The package is published on PyPI as `pymcsc`, but it is imported as `mcsc`.

For development:

```bash
pip install -e ".[dev]"
```

## Usage

```python
import asyncio
from mcsc import ServerManager, ServerType

manager = ServerManager("data/servers")   # registry: data/servers/servers.json
manager.load()

server = manager.create(
    "Survival", "1.21.4", ServerType.PAPER,
    accept_eula=True, ram_gb=4,
)

async def main():
    server.add_output_listener(lambda srv, line: print(line))
    server.add_state_listener(lambda srv, old, new: print(f"{old} -> {new}"))

    await server.start()          # returns once the process is spawned
    await server.send_command("say hello")
    await server.stop()           # "stop", then kill after the timeout

asyncio.run(main())
```

### Main pieces

| Module | What it does |
|---|---|
| `ServerManager` | Registry of servers: `load`, `save`, `create` / `create_async`, `register`, `get`, `find`, `delete`, `stop_all` |
| `MinecraftServer` (`VanillaServer`, `PaperServer`, `ForgeServer`) | One server: `install`, `start`, `stop`, `kill`, `restart`, `wait`, `send_command`, `state`, `output`, `monitor`, config files |
| `ServerConfig` | Persistent settings (dataclass); `to_dict` / `from_dict` (also reads the legacy `servers.json`) |
| `ServerType`, `ServerState` | Enums. `ServerType` values match the legacy `jar_type` |
| `VersionCatalog` | Available versions and download URLs per server type |
| `ProcessMonitor` / `ResourceUsage` | RAM/CPU/disk usage of the server process tree (`server.monitor.usage`) |
| `mcsc.files` | Read/write `.properties`, `.yml`, `.json` |
| `mcsc.system` | `total_ram_gb`, `suggested_ram_gb` |
| `mcsc.exceptions` | `MCSCError` and subclasses |

### Server lifecycle

`STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`. `RUNNING` is reached when the
server prints its `Done (...)!` line. `start()` refuses to run until the EULA is accepted
(`EulaNotAcceptedError`).

Forge servers are launched with Java directly using the installer's argument files
(the same thing `run.bat`/`run.sh` do), so they work on every OS and `ram_gb` is always applied.

### Config files

```python
server.available_config_files()        # e.g. ["server.properties", "bukkit.yml", "spigot.yml"]
props = server.read_properties()
props["motd"] = "Hello"
server.write_properties(props)
data = server.read_config("spigot.yml")
server.write_config("spigot.yml", data)
```

### Logging

The library logs on the `mcsc` logger and installs no handlers. Configure it from the app:

```python
import logging
logging.getLogger("mcsc").setLevel(logging.INFO)
```

## Migrating from minecraft-server-creator

| Old (`mcsc.modules...`) | New |
|---|---|
| `servers.models.MinecraftServer` | `mcsc.MinecraftServer` |
| `servers.java.JavaServer` | `mcsc.VanillaServer` |
| `servers.utils.TYPE_TO_CLASS` | `mcsc.servers.SERVER_CLASSES` / `server_from_config` |
| `server_list`, `global_settings`, `get_server_list()` | `ServerManager` instance |
| `servers.utils.load_servers()` | `ServerManager(SERVER_DIR, SERVERS_JSON_PATH).load()` |
| `servers.utils.create_server(settings)` | `manager.create(...)` / `await manager.create_async(...)` |
| `get_server_by_uuid(uuid)` | `manager.get(id)` / `manager.find(id)` |
| `full_stop()` | `await manager.stop_all()` |
| `server.delete(delete_dir)` | `manager.delete(server, delete_files=...)` |
| `server.save()` | `manager.save()` after editing `server.config` |
| `server.settings["dedicated_ram"]`, `["jar_type"]`, `["uuid"]`, `["folder_path"]` | `server.config.ram_gb`, `.server_type`, `.id`, `.path` |
| `server.running` / `starting` / `stopping`, `status` | `server.state` (`ServerState`), `server.is_active` |
| `server.log = ui.log` | `server.add_output_listener(lambda s, line: log.push(line))`; history in `server.output` |
| `server.console_writer(cmd)` | `await server.send_command(cmd)` |
| `await server.start()` (returned at exit) | `await server.start()` (returns at spawn) + `await server.wait()` |
| `server.monitor.ram_usage` | `server.monitor.usage.ram_mb` (also `cpu_percent`, `disk_read_mb`, `disk_write_mb`) |
| `load_server_properties()` / `save_server_properties(editor)` | `read_properties()` / `write_properties(dict)` |
| `PaperServer.load_yml` / `save_yml(filename, editor)` | `read_config(name)` / `write_config(name, data)` |
| `modules.utils.JarUrl`, `load_*_versions()` | `VersionCatalog` (`versions`, `latest`, `download_url`) |
| `get_system_total_ram`, `get_suggested_ram` | `mcsc.total_ram_gb`, `mcsc.suggested_ram_gb` |
| `modules.classes.ProcessMonitor` | `mcsc.ProcessMonitor` |

Telemetry (`server_start`, `server_stop`, ...) moves to the app: send it from a state listener.
