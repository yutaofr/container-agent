"""Host helpers: held ports, log timestamps, and killing a whole process group."""

import asyncio
import socket
import subprocess
import sys

from cagent.eval.host import free_port, hold_port, log_time
from cagent.eval.station import group_members, is_runner, kill_group


def test_a_held_port_stays_taken_on_every_interface_until_released():
    held = hold_port()
    s = socket.socket()
    try:
        assert s.connect_ex(("127.0.0.1", held.port)) == 0
    finally:
        s.close()
    held.release()
    probe = socket.socket()
    probe.bind(("", held.port))
    probe.close()
    assert 1024 < free_port() < 65536


def test_log_times_parse_docker_timestamps():
    assert log_time("1970-01-01T00:16:40.500000000Z text") == 1000.5
    assert log_time("2026-10-20T20:57:53Z x") == 1792529873.0
    assert log_time("no timestamp here") is None


async def test_killing_a_group_takes_its_children_and_reports_when_none_is_left():
    child = "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
    await asyncio.sleep(0.3)
    assert not is_runner(proc.pid)  # only a `cagent-runner serve` process counts
    assert any("sleep 60" in m for m in group_members(proc.pid))
    assert await kill_group(proc.pid) is True
    proc.wait(timeout=5)
    out = subprocess.run(["pgrep", "-g", str(proc.pid)], capture_output=True, text=True)
    assert out.stdout == "" and group_members(proc.pid) == []
    assert await kill_group(proc.pid) is True  # an empty group is already gone


async def test_a_group_left_with_only_a_zombie_counts_as_gone():
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    proc.kill()
    await asyncio.sleep(0.3)  # killed but not reaped: a zombie leads the group
    assert await kill_group(proc.pid) is True
    proc.wait(timeout=5)
