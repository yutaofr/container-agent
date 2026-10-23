"""A station: one hub and one Runner process for one attempt.

The Runner runs in its own process group, so the abnormal cleanup can stop it together with
the docker commands it started, and then confirm that none of them is left.
"""

import asyncio
import os
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from cagent.eval.host import free_port
from cagent.hub import RunnerHub, start
from cagent.hub import status as hub_status

RUNNER_ID = "eval"


class Station(Protocol):
    runner_id: str
    target: Any  # what EvidenceCaller wraps: call(runner_id, method, params, timeout, ...)
    instance: str | None
    pid: int | None  # the Runner's process, which leads its own process group

    async def status(self) -> dict[str, Any]: ...

    async def stop(self) -> None: ...

    async def kill(self) -> bool: ...


async def kill_group(pgid: int, timeout_s: float = 10.0) -> bool:
    """SIGKILL a process group; True once no process of it is left."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # macOS: only zombies are left (see below); confirm by polling as usual
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # macOS answers EPERM when only zombies are left: a live process of ours would
            # accept signal 0, so nothing in the group can act any more.
            return True
        await asyncio.sleep(0.1)
    return False


def group_members(pgid: int) -> list[str]:
    """The commands of every live process in a process group."""
    out = subprocess.run(
        ["ps", "-A", "-o", "pgid=,stat=,command="], capture_output=True, text=True
    ).stdout
    members = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] == str(pgid) and not parts[1].startswith("Z"):
            members.append(parts[2])
    return members


def is_runner(pid: int) -> bool:
    """True when pid is still a `cagent-runner serve` process (not a reused pid)."""
    out = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
    ).stdout
    return "cagent-runner" in out and " serve " in f"{out} "


class RunnerStation:
    def __init__(self, hub: RunnerHub, server: Any, proc: subprocess.Popen, log: Any, home: Path):
        self.hub, self.server, self.proc, self._log, self.home = hub, server, proc, log, home
        self.runner_id = RUNNER_ID
        self.target = hub
        self.pid = proc.pid
        self.instance = hub.instance(RUNNER_ID)
        self._closed = False

    @classmethod
    async def start(
        cls,
        runner: Path,
        home: Path,
        workdir_roots: list[Path],
        log_path: Path,
        env: dict[str, str] | None = None,
    ) -> "RunnerStation":
        token = secrets.token_urlsafe(24)
        hub = RunnerHub({token: RUNNER_ID})
        port = free_port()
        server = await start(hub, "127.0.0.1", port)
        roots = [arg for r in workdir_roots for arg in ("-workdir-root", str(r))]
        log = log_path.open("ab")
        proc = subprocess.Popen(
            [str(runner), "serve", "-hub", f"ws://127.0.0.1:{port}/runner", *roots],
            env={**(env or os.environ), "CAGENT_TOKEN": token, "CAGENT_HOME": str(home)},
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            await hub.wait_for_runner(RUNNER_ID, timeout=15)
        except TimeoutError:
            station = cls(hub, server, proc, log, home)
            await station.kill()
            raise
        return cls(hub, server, proc, log, home)

    async def status(self) -> dict[str, Any]:
        return await asyncio.wait_for(hub_status(self.hub, RUNNER_ID), 10)

    async def stop(self) -> None:
        """A normal stop: the Runner is idle, so SIGTERM ends it."""
        self.proc.terminate()
        try:
            await asyncio.to_thread(self.proc.wait, 10)
        except subprocess.TimeoutExpired:
            await self.kill()
        await self._close()

    async def kill(self) -> bool:
        """Kill the Runner's whole process group; True once no process of it is left."""
        gone = await kill_group(self.proc.pid)  # start_new_session made it the group leader
        await asyncio.to_thread(self.proc.wait)
        await self._close()
        return gone

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.server.close()
        await self.server.wait_closed()
        self._log.close()
        shutil.rmtree(self.home, ignore_errors=True)  # the Runner's workspace: compose files
