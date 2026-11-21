import asyncio
import subprocess
import sys

from mcsc import ServerState
from mcsc.monitor import ProcessMonitor, ResourceUsage

from test_server import wait_for_state


def spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code])


SLEEPER = "import time; time.sleep(30)"


async def test_restart_within_interval_keeps_monitoring_new_process():
    """A stale run() waking up after a quick restart must not stop the new run."""
    monitor = ProcessMonitor(1.0)
    a = spawn("import time; time.sleep(0.3)")
    task_a = asyncio.ensure_future(monitor.run(a.pid))
    await asyncio.sleep(0.1)
    a.wait()
    monitor.stop()  # what MinecraftServer._finish() does

    b = spawn("x = bytearray(50_000_000); import time; time.sleep(30)")
    try:
        task_b = asyncio.ensure_future(monitor.run(b.pid))
        await asyncio.wait_for(task_a, 2)  # the stale run wakes up and exits...
        await asyncio.sleep(1.5)  # ...and the new one keeps sampling

        assert monitor.active
        assert not task_b.done()
        assert monitor.usage.ram_mb > 40
        assert b.pid in monitor._procs and a.pid not in monitor._procs
    finally:
        b.kill()
        b.wait()
    await asyncio.wait_for(task_b, 3)
    assert not monitor.active
    assert monitor.usage == ResourceUsage()


async def test_new_run_takes_over_without_stop():
    """run() is never silently ignored, even while a previous run is still active."""
    monitor = ProcessMonitor(0.2)
    a, b = spawn(SLEEPER), spawn(SLEEPER)
    try:
        task_a = asyncio.ensure_future(monitor.run(a.pid))
        await asyncio.sleep(0.3)
        assert monitor.active

        task_b = asyncio.ensure_future(monitor.run(b.pid))
        await asyncio.wait_for(task_a, 2)  # superseded
        await asyncio.sleep(0.3)
        assert monitor.active
        assert not task_b.done()
        assert b.pid in monitor._procs and a.pid not in monitor._procs
    finally:
        for p in (a, b):
            p.kill()
            p.wait()
    await asyncio.wait_for(task_b, 2)


async def test_stop_halts_sampling_and_resets_usage():
    monitor = ProcessMonitor(0.2)
    p = spawn(SLEEPER)
    try:
        task = asyncio.ensure_future(monitor.run(p.pid))
        await asyncio.sleep(0.3)
        assert monitor.active and monitor.usage.ram_mb > 0

        monitor.stop()
        assert not monitor.active
        assert monitor.usage == ResourceUsage()
        await asyncio.wait_for(task, 2)
        assert monitor.usage == ResourceUsage()  # the exiting run doesn't sample again
    finally:
        p.kill()
        p.wait()


async def test_run_on_missing_process_stays_inactive():
    p = spawn("pass")
    p.wait()
    monitor = ProcessMonitor(0.2)
    await asyncio.wait_for(monitor.run(p.pid), 2)
    assert not monitor.active
    assert monitor.usage == ResourceUsage()


async def test_server_quick_restart_keeps_monitor_active(fake_server):
    """start(), process exits, start() again right away: the monitor tracks the new process."""
    await fake_server.start()
    await wait_for_state(fake_server, ServerState.RUNNING)
    await asyncio.sleep(0.2)  # the first run() is now inside its sleep
    await fake_server.stop(timeout=10)
    assert not fake_server.monitor.active

    await fake_server.start()
    try:
        await wait_for_state(fake_server, ServerState.RUNNING)
        await asyncio.sleep(fake_server.monitor.interval * 2.5)  # let the stale run wake up
        assert fake_server.monitor.active
        assert fake_server.monitor.usage.ram_mb > 0
        assert fake_server.pid in fake_server.monitor._procs
    finally:
        await fake_server.stop(timeout=10)
    assert not fake_server.monitor.active
    assert fake_server.monitor.usage == ResourceUsage()
