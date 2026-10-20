"""`cagent-platform serve`: the hub, the HTTP interface, and RunService in one process.

One process and one web worker: Runner connections and running executions live in this
process's memory, so a second worker would not see them.

Startup: open the database, reconcile persisted runs (no graph is advanced and no Runner is
waited for), start the hub, then accept HTTP requests. Shutdown: stop accepting requests,
stop the executions (each records why), close the hub, then the database.
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

import uvicorn
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from cagent.api import create_app
from cagent.auth import Sessions
from cagent.catalog import load_catalog
from cagent.config import PlatformConfig, load_config
from cagent.dispatch import RecordingCaller
from cagent.hub import RunnerHub
from cagent.hub import start as start_hub
from cagent.llm import LiteLLMCompleter, Router, load_model_config
from cagent.orchestrator import Orchestrator, Settings
from cagent.runs import RunService
from cagent.store import Store


class Platform:
    """Everything but the HTTP server, opened and closed in the documented order."""

    def __init__(self, config: PlatformConfig, settings: Settings | None = None):
        self.config, self.settings = config, settings
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "Platform":
        c = self.config
        c.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = await Store.open(str(c.db_path))
        self._stack.push_async_callback(self.store.close)
        saver = await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(c.db_path))
        )
        router = Router(load_model_config(c.models), LiteLLMCompleter()) if c.models else None
        self.hub = RunnerHub(c.runner_tokens)
        orch = Orchestrator(
            RecordingCaller(self.hub, self.store),
            load_catalog(*c.catalog),
            saver,
            self.settings or c.settings,
            router=router,
        )
        self.service = RunService(self.store, orch, self.hub, c.user_runners)
        await self.service.recover_on_startup()
        self.hub.add_listener(self.service.runner_changed)
        hub_server = await start_hub(self.hub, c.hub_host, c.hub_port)
        self.hub_port = hub_server.sockets[0].getsockname()[1]

        async def close_hub() -> None:
            hub_server.close()
            await hub_server.wait_closed()

        self._stack.push_async_callback(close_hub)
        self._stack.push_async_callback(self.service.close)  # runs first
        self.sessions = Sessions(c.user_tokens, c.session_ttl_s, c.idle_ttl_s)
        self.app = create_app(self.service, self.sessions, c.origin, c.secure_cookies)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()


def web_server(platform: Platform, port: int | None = None, graceful_s: int = 5) -> uvicorn.Server:
    """The HTTP server. Open event streams never end by themselves, so shutdown waits for
    them at most graceful_s seconds before cancelling them; otherwise SIGTERM would wait for
    every browser tab to close."""
    c = platform.config
    return uvicorn.Server(
        uvicorn.Config(
            platform.app,
            host=c.web_host,
            port=c.web_port if port is None else port,
            workers=1,
            lifespan="off",
            proxy_headers=False,  # never trust forwarded headers
            log_level="info",
            timeout_graceful_shutdown=graceful_s,
        )
    )


async def serve(config: PlatformConfig) -> None:
    async with Platform(config) as platform:
        await web_server(platform).serve()  # returns after SIGINT or SIGTERM


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cagent-platform")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve", help="run the hub and the web interface")
    run.add_argument("--config", required=True, help="path to platform.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(load_config(args.config)))
