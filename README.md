# mcsc

Python library to create, run and manage Minecraft servers (Vanilla, Paper, Forge 1.17+).
It is the server-management core of [Minecraft Server Creator](https://github.com/ddavidel/minecraft-server-creator)

Requires Python 3.12+ and Java on the machine that runs the servers.

## Install

```bash
pip install pymcsc
```

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
| `ServerManager` | Registry of servers: `load`, `save`, `create` / `create_async`, `register`, `get`, `find`, `delete`, `stop_all`; backups, restore, export and import (see below) |
| `MinecraftServer` (`VanillaServer`, `PaperServer`, `ForgeServer`) | One server: `install`, `start`, `stop`, `kill`, `restart`, `wait`, `send_command`, `state`, `output`, `monitor`, config files |
| `ServerConfig` | Persistent settings (dataclass); `to_dict` / `from_dict` (also reads the legacy `servers.json`) |
| `ServerType`, `ServerState` | Enums. `ServerType` values match the legacy `jar_type` |
| `VersionCatalog` | Available versions and download URLs per server type |
| `ProcessMonitor` / `ResourceUsage` | RAM/CPU/disk usage of the server process tree (`server.monitor.usage`) |
| `mcsc.backup` | Backup archive format: `BackupKind`, `Backup`, `BackupManifest`, `create_archive`, `extract_archive`, `read_manifest` |
| `mcsc.folders` | `detect_server_folder`: type/version of an existing server folder |
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

### Backups, export and import

Backups are `.zip` archives stored in `backups_dir/<server id>/` (default: `backups` next to
`servers_dir`, override with `ServerManager(..., backups_dir=...)`), named
`<YYYYmmdd-HHMMSS>_<kind>.zip` (UTC). Each archive holds the server folder under `server/`
(without `logs/`, `crash-reports/`, `cache/` and `*.log`) plus a `mcsc-backup.json` manifest
with the server config (no local paths). Exports use the same format.

```python
from mcsc import BackupKind, detect_server_folder

manager = ServerManager("data/servers", app_version="1.4.0")  # app_version goes in manifests
manager.load()
manager.cleanup_temporary_files()            # leftovers of interrupted operations

backup = manager.backup(server)              # blocking
backup = await manager.backup_async(server, BackupKind.AUTOMATIC)   # in a worker thread
manager.list_backups(server)                 # newest first
manager.prune_backups(server, keep=5)        # only AUTOMATIC backups are pruned
manager.delete_backup(backup)
manager.restore_backup(server, backup)       # returns the "before_restore" backup

await manager.export_async(server, "Survival.zip")
info = manager.inspect_archive("Survival.zip")        # manifest, eula_accepted, suggested_name
new = manager.import_archive("Survival.zip", name=info.suggested_name, accept_eula=True)

found = detect_server_folder("C:/old-server")         # type, version, jar (None if unknown)
manager.import_folder("C:/old-server", found.server_type, found.version, name="Old")
```

- **Stopped servers only**: backup, restore and export raise `ServerRunningError` unless the
  server is stopped, so the world on disk is always consistent.
- **One operation per server**: while a backup, restore, export or delete runs,
  `server.operation` is set and `start()` and other operations raise `ServerBusyError`.
- **Safe writes**: archives are written to `*.partial` and renamed when complete; restore
  extracts next to the server folder (`<folder>.restore`) and swaps it in only when complete;
  free disk space is checked first (`InsufficientDiskSpaceError`).
- **Validation**: archives with entries outside the target folder, absolute paths or symlinks,
  without a manifest or corrupted raise `InvalidArchiveError`; archives from a newer mcsc
  raise `UnsupportedArchiveVersionError`; restoring a backup of another server type raises
  `IncompatibleBackupError`.
- **Import**: new id and folder, never overwrites a server. The EULA is set to `accept_eula`
  regardless of the archive. A `java_path` that doesn't exist on this machine becomes `java`.
- `progress=(done, total)` callbacks are called from the worker thread.

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
