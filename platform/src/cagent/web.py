"""The browser interface: server-rendered pages and fragments over the same RunService and
access rules as the JSON API.

All HTML comes from Jinja2 templates with autoescaping on; user and model text is never
marked safe, never reaches a script or an event handler, and never chooses an hx-* attribute,
a template, or a link. A strict Content-Security-Policy allows only this origin's scripts and
styles; HTMX is configured through a meta tag before it starts (no eval, no script tags, no
injected styles, no history cache). Requests the page makes by itself never count as use of
the session; only what the user does refreshes it.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, StrictUndefined

from cagent.access import PRE_COOKIE, SESSION_COOKIE, Access, Denied
from cagent.auth import LoginError, RateLimited
from cagent.runs import RunError, RunService

STATIC = Path(__file__).parent / "static"
HTMX_VERSION = "2.0.10"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)

# Read by htmx when it starts. 422 and 409 are swapped into the card that asked; 401 and
# other errors are not swapped (run.js handles them).
HTMX_CONFIG = {
    "allowEval": False,
    "allowScriptTags": False,
    "includeIndicatorStyles": False,
    "historyEnabled": False,
    "historyCacheSize": 0,
    "selfRequestsOnly": True,
    "responseHandling": [
        {"code": "204", "swap": False},
        {"code": "[23]..", "swap": True},
        {"code": "409", "swap": True, "error": False},
        {"code": "422", "swap": True, "error": False},
        {"code": "[45]..", "swap": False, "error": True},
    ],
}

templates = Environment(
    loader=PackageLoader("cagent", "templates"),
    autoescape=True,  # every template, whatever its name
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)

templates.filters["when"] = lambda t: (
    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) if t is not None else "—"
)


def render(name: str, status: int = 200, **context: Any) -> HTMLResponse:
    html = templates.get_template(name).render(htmx_config=json.dumps(HTMX_CONFIG), **context)
    return HTMLResponse(html, status_code=status)


async def form(request: Request) -> dict[str, list[str]]:
    return parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)


def first(fields: dict[str, list[str]], name: str) -> str:
    return (fields.get(name) or [""])[0]


class SecurityHeaders:
    """Adds the security headers to every response as it starts. A plain ASGI middleware:
    it never wraps a response body, so event streams and their disconnects pass untouched."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        static = scope["path"].startswith("/static/")

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"cache-control",) or static
                ]
                headers += [
                    (b"content-security-policy", CSP.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                if not static:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app: FastAPI, service: RunService, access: Access) -> None:
    sessions = access.sessions
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    app.add_middleware(SecurityHeaders)

    def login_page(request: Request, status: int = 200, error: str | None = None) -> Response:
        """The login form with a new pre-session: a failed login used up the old one."""
        try:
            pre = sessions.new_pre(access.client(request))
        except RateLimited:
            return render("login.html", 429, csrf=None, error="Too many attempts. Wait a minute.")
        response = render("login.html", status, csrf=pre.csrf, error=error)
        access.cookie(response, PRE_COOKIE, pre.id, sessions.pre_ttl)
        return response

    def page_session(request: Request):
        """The session for a page the user opened (counts as use), or None."""
        s = access.session(request)
        if s is not None:
            sessions.touch(s)
        return s

    @app.get("/login")
    async def login_get(request: Request) -> Response:
        return login_page(request)

    @app.post("/login")
    async def login_post(request: Request) -> Response:
        try:
            access.same_origin(request)
        except Denied:
            return login_page(request, 403, "This request did not come from the login page.")
        fields = await form(request)
        try:
            s = sessions.login(
                request.cookies.get(PRE_COOKIE),
                first(fields, "csrf"),
                first(fields, "token"),
                access.client(request),
            )
        except RateLimited:
            return render("login.html", 429, csrf=None, error="Too many attempts. Wait a minute.")
        except LoginError:
            return login_page(request, 401, "Login failed.")
        response = RedirectResponse("/", status_code=303)
        access.cookie(response, SESSION_COOKIE, s.id, sessions.ttl)
        response.delete_cookie(PRE_COOKIE, path="/")
        return response

    @app.get("/")
    async def runs_page(request: Request) -> Response:
        s = page_session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        runs = await service.list_runs(s.user)
        return render("runs.html", user=s.user, csrf=s.csrf, runs=runs)

    @app.get("/runs/new")
    async def new_run_page(request: Request) -> Response:
        s = page_session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        return render(
            "new_run.html",
            user=s.user,
            csrf=s.csrf,
            runners=await service.runners(s.user),
            command_id=uuid.uuid4().hex,  # a retry of this form reuses it
        )

    @app.post("/runs")
    async def create_run(request: Request) -> Response:
        s = access.changing(request)
        fields = await form(request)
        purpose, spec = None, None
        if first(fields, "input") == "spec":
            try:
                spec = yaml.safe_load(first(fields, "spec"))
            except yaml.YAMLError as e:
                return render("form_error.html", 422, error=f"The spec is not valid YAML: {e}")
            if not isinstance(spec, dict):
                return render("form_error.html", 422, error="The spec must be a YAML mapping.")
        else:
            purpose = first(fields, "purpose")
        try:
            acc = await service.create_run(
                s.user,
                first(fields, "command_id"),
                first(fields, "runner"),
                first(fields, "cluster"),
                purpose=purpose,
                spec=spec,
            )
        except RunError as e:
            status = e.status if e.status in (409, 422) else 422
            return render("form_error.html", status, error=CREATE_ERRORS.get(e.code, e.code))
        return Response(status_code=204, headers={"HX-Redirect": f"/runs/{acc.body['run_id']}"})


CREATE_ERRORS = {
    "cluster_busy": "This cluster already has an active run.",
    "cluster_name_taken": "Another user owns a cluster with this name on this Runner.",
    "invalid_cluster": "Use lowercase letters, digits, - and _ for the cluster name.",
    "invalid_input": "Give either a purpose or a spec.",
    "invalid_spec": "The spec does not describe a valid cluster.",
    "command_id_reused": "This form was already submitted with other values. Reload the page.",
    "not_found": "Unknown Runner.",
}
