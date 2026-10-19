"""The platform's HTTP interface: login, runs, commands, and (cagent.events) event streams.

Every route checks the session; every state-changing route also checks the X-CSRF-Token
header against the session and the Origin header against the configured origin, exactly.
Login has its own CSRF check through a pre-session. Proxy headers are never consulted.
Routes only translate HTTP to RunService calls and back.
"""

from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cagent.auth import LoginError, RateLimited, Session, Sessions
from cagent.runs import Accepted, RunError, RunService

SESSION_COOKIE = "cagent_session"
PRE_COOKIE = "cagent_pre"


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=128)
    runner: str
    cluster: str
    purpose: str | None = None
    spec: dict[str, Any] | None = None


class Denied(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def _error(status: int, code: str, detail: str = "", snapshot: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if detail:
        body["detail"] = detail
    if snapshot is not None:
        body["snapshot"] = snapshot
    return JSONResponse(body, status_code=status)


def _accepted(acc: Accepted) -> JSONResponse:
    headers = {"Cagent-Replay": "true"} if acc.replay else {}
    return JSONResponse(acc.body, status_code=202, headers=headers)


def create_app(
    service: RunService, sessions: Sessions, origin: str, secure_cookies: bool
) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.service, app.state.sessions, app.state.origin = service, sessions, origin

    @app.exception_handler(RunError)
    async def run_error(_request: Request, e: RunError) -> JSONResponse:
        return _error(e.status, e.code, e.detail, e.snapshot)

    @app.exception_handler(Denied)
    async def denied(_request: Request, e: Denied) -> JSONResponse:
        return _error(e.status, e.code)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, e: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_command", str(e.errors()))

    def cookie(response: JSONResponse, name: str, value: str, max_age: float) -> None:
        response.set_cookie(
            name,
            value,
            max_age=int(max_age),
            httponly=True,
            samesite="strict",
            secure=secure_cookies,
            path="/",
        )

    def same_origin(request: Request) -> None:
        if request.headers.get("origin") != origin:  # missing and "null" never match
            raise Denied(403, "bad_origin")

    def session(request: Request) -> Session:
        s = sessions.get(request.cookies.get(SESSION_COOKIE))
        if s is None:
            raise Denied(401, "login_required")
        return s

    Live = Annotated[Session, Depends(session)]

    def user(request: Request, s: Live) -> str:
        sessions.touch(s)
        return s.user

    def changing(request: Request, s: Live) -> str:
        same_origin(request)
        if not Sessions.csrf_ok(s, request.headers.get("x-csrf-token")):
            raise Denied(403, "bad_csrf")
        sessions.touch(s)
        return s.user

    User = Annotated[str, Depends(user)]
    Changing = Annotated[str, Depends(changing)]

    def client(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # --- login -------------------------------------------------------------------------------

    @app.get("/login")
    async def login_form(request: Request) -> JSONResponse:
        try:
            pre = sessions.new_pre(client(request))
        except RateLimited:
            return _error(429, "rate_limited")
        response = JSONResponse({"csrf": pre.csrf})
        cookie(response, PRE_COOKIE, pre.id, sessions.pre_ttl)
        return response

    @app.post("/login")
    async def login(request: Request) -> JSONResponse:
        same_origin(request)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        token = (form.get("token") or [""])[0]
        csrf = (form.get("csrf") or [""])[0]
        try:
            s = sessions.login(request.cookies.get(PRE_COOKIE), csrf, token, client(request))
        except RateLimited:
            return _error(429, "rate_limited")
        except LoginError:
            return _error(401, "login_failed")
        response = JSONResponse({"user": s.user, "csrf": s.csrf})
        cookie(response, SESSION_COOKIE, s.id, sessions.ttl)
        response.delete_cookie(PRE_COOKIE, path="/")
        return response

    @app.post("/logout")
    async def logout(request: Request, _user: Changing) -> JSONResponse:
        sessions.logout(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # --- runs --------------------------------------------------------------------------------

    @app.get("/api/runs")
    async def list_runs(who: User) -> JSONResponse:
        return JSONResponse(await service.list_runs(who))

    @app.post("/api/runs")
    async def create_run(body: CreateRun, who: Changing) -> JSONResponse:
        acc = await service.create_run(
            who, body.command_id, body.runner, body.cluster, purpose=body.purpose, spec=body.spec
        )
        return _accepted(acc)

    @app.get("/api/runs/{run_id}")
    async def snapshot(run_id: str, who: User) -> JSONResponse:
        return JSONResponse(await service.snapshot(who, run_id))

    @app.post("/api/runs/{run_id}/commands")
    async def submit(run_id: str, request: Request, who: Changing) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return _error(422, "invalid_command", "the body is not JSON")
        if not isinstance(body, dict):
            return _error(422, "invalid_command", "the body is not an object")
        return _accepted(await service.submit(who, run_id, body))

    @app.get("/api/runs/{run_id}/commands/{command_id}")
    async def command(run_id: str, command_id: str, who: User) -> JSONResponse:
        return JSONResponse(await service.command(who, run_id, command_id))

    return app
