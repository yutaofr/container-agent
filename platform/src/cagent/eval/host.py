"""The host: held ports, and read-only views of the Docker daemon the Runner uses.

The harness reads container logs, lists a project's leftovers, and reads Docker events
itself, through the docker CLI with the same environment as the Runner, so it looks at the
same daemon. Every call has a time limit; logs also have a byte limit. Nothing here starts,
changes, or removes anything except `force_down`, the abnormal cleanup's bounded `down`.
"""

import asyncio
import json
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"


class HostError(Exception):
    """A host command failed or timed out: what it reports cannot be trusted."""


@dataclass
class HeldPort:
    """A port bound on all interfaces and listening, as a busy service would hold it."""

    sock: socket.socket
    port: int

    def release(self) -> None:
        self.sock.close()


@dataclass
class LogCapture:
    container: str
    service: str
    lines: list[str] = field(default_factory=list)  # "<RFC 3339 timestamp> <text>"
    truncated: bool = False
    error: str | None = None


def hold_port() -> HeldPort:
    s = socket.socket()
    s.bind(("", 0))
    s.listen()
    return HeldPort(s, s.getsockname()[1])


def free_port() -> int:
    """A port free right now; it is released, so a later bind may still collide."""
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def log_time(line: str) -> float | None:
    """The epoch time of a `docker logs --timestamps` line."""
    stamp = line.split(" ", 1)[0]
    try:
        whole, _, frac = stamp.rstrip("Z").partition(".")
        base = datetime.fromisoformat(whole + "+00:00")
        return base.timestamp() + (float("0." + frac) if frac else 0.0)
    except ValueError:
        return None


class DockerHost:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        timeout_s: float = 30.0,
        log_lines: int = 2000,
        log_bytes: int = 256 * 1024,
        ollama: str = "http://localhost:11434",
    ):
        self.env, self.timeout_s = env, timeout_s
        self.log_lines, self.log_bytes, self.ollama = log_lines, log_bytes, ollama

    async def _docker(self, *args: str, timeout: float | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            env=self.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout or self.timeout_s)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise HostError(f"docker {args[0]} timed out") from e
        if proc.returncode != 0:
            raise HostError(f"docker {' '.join(args[:2])}: {out.decode(errors='replace')[-300:]}")
        return out.decode(errors="replace")

    async def platform(self) -> str:
        """The daemon's OS/architecture, e.g. linux/arm64."""
        out = await self._docker("version", "--format", "{{.Server.Os}}/{{.Server.Arch}}")
        return out.strip()

    async def versions(self) -> dict[str, str]:
        out = await self._docker("version", "--format", "{{json .}}")
        doc = json.loads(out)
        return {"client": doc["Client"]["Version"], "server": doc["Server"]["Version"]}

    async def image_ids(self, images: list[str]) -> dict[str, str | None]:
        """Each image's local ID, or None when it is not present."""
        out: dict[str, str | None] = {}
        for image in images:
            try:
                out[image] = (
                    await self._docker("image", "inspect", "--format", "{{.Id}}", image)
                ).strip()
            except HostError:
                out[image] = None
        return out

    async def containers(self, project: str) -> list[dict[str, str]]:
        out = await self._docker(
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            f"label={LABEL_PROJECT}={project}",
            "--format",
            '{{.ID}}\t{{.Label "' + LABEL_SERVICE + '"}}',
        )
        rows = [ln.split("\t") for ln in out.splitlines() if ln.strip()]
        return [{"id": r[0], "service": r[1] if len(r) > 1 else ""} for r in rows]

    async def logs(self, project: str) -> list[LogCapture]:
        """Each of the project's containers' recent output, bounded in lines and bytes."""
        caps = []
        for c in await self.containers(project):
            cap = LogCapture(c["id"], c["service"])
            try:
                out = await self._docker(
                    "logs", "--timestamps", "--tail", str(self.log_lines), c["id"]
                )
            except HostError as e:
                cap.error = str(e)
            else:
                data = out.encode()
                if len(data) > self.log_bytes:
                    data, cap.truncated = data[-self.log_bytes :], True
                lines = data.decode(errors="replace").splitlines()
                cap.truncated = cap.truncated or len(lines) >= self.log_lines
                cap.lines = lines
            caps.append(cap)
        return caps

    async def resources(self, project: str) -> dict[str, list[str]]:
        """What the project still has on the daemon: containers, networks, volumes."""
        flt = f"label={LABEL_PROJECT}={project}"
        containers = [c["id"] for c in await self.containers(project)]
        networks = (await self._docker("network", "ls", "-q", "--filter", flt)).split()
        volumes = (await self._docker("volume", "ls", "-q", "--filter", flt)).split()
        return {"containers": containers, "networks": networks, "volumes": volumes}

    async def created_since(self, project: str, since: float) -> list[str]:
        """Container create and start events of the project from since until now."""
        now = time.time()
        out = await self._docker(
            "events",
            "--since",
            f"{since:.3f}",
            "--until",
            f"{now:.3f}",
            "--filter",
            f"label={LABEL_PROJECT}={project}",
            "--filter",
            "type=container",
            "--filter",
            "event=create",
            "--filter",
            "event=start",
            "--format",
            "{{json .}}",
        )
        return [ln for ln in out.splitlines() if ln.strip()]

    async def force_down(self, project: str) -> None:
        """The abnormal cleanup's bounded down, by project name, after the Runner is gone."""
        await self._docker("compose", "-p", project, "down", "-v", "--remove-orphans", timeout=120)

    async def model_digests(self) -> dict[str, str]:
        """Ollama's model name -> digest."""

        def fetch() -> dict[str, Any]:
            with urllib.request.urlopen(f"{self.ollama}/api/tags", timeout=10) as r:
                return json.load(r)

        doc = await asyncio.to_thread(fetch)
        return {m["name"]: m["digest"] for m in doc.get("models", [])}

    @staticmethod
    def describe(caps: list[LogCapture]) -> list[dict[str, Any]]:
        return [vars(c) for c in caps]
