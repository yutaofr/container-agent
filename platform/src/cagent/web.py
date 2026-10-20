"""The browser interface: server-rendered pages and fragments over the same RunService and
access rules as the JSON API.

All HTML comes from Jinja2 templates with autoescaping on; user and model text is never
marked safe, never reaches a script or an event handler, and never chooses an hx-* attribute,
a template, or a link. A strict Content-Security-Policy allows only this origin's scripts and
styles; HTMX is configured through a meta tag before it starts (no eval, no script tags, no
injected styles, no history cache). Requests the page makes by itself never count as use of
the session; only what the user does refreshes it.
"""

import difflib
import json
import re
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
from cagent.record import mask_published
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


CARDS = {
    "question": "cards/question.html",
    "save_descriptors": "cards/save_descriptors.html",
    "approval": "cards/approval.html",
    "patch_approval": "cards/patch_approval.html",
    "escalation": "cards/escalation.html",
    "recovery": "cards/recovery.html",
}

# What each escalation means for the person deciding; the backend's reason is a fixed set.
ESCALATIONS = {
    "init_failed": (
        "An init script failed. Retrying validates and builds again and may run init again: "
        "check what a partial run left in the database before you retry."
    ),
    "runner_error": "The Runner reported an error. Check the Runner, then retry.",
    "build_failed": "A build failed. Fix the project or its descriptor, then retry.",
    "up_failed": "docker compose up failed. Check the details below, then retry.",
    "verify_failed": "The cluster started but did not pass acceptance.",
    "startup_timeout": "The cluster did not settle in time.",
    "budget": "The fix budget is spent: automatic fixes stopped.",
    "no_rule": "No rule or model could fix this.",
    "oscillation": "Fixes went back to a state already tried.",
    "no_fix": "The model found no fix inside the environment.",
    "model_unavailable": "No model answered.",
    "patch_rejected": "You rejected the proposed patch.",
    "clarify": "The spec has problems no question can settle.",
    "unsupported_source": "A service uses a source kind the POC cannot build.",
}


def diff(old: str | None, new: str) -> list[tuple[str, str]]:
    """A unified diff as (kind, line) pairs; the template escapes each line."""
    if old is None:
        return []
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "approved", "now", lineterm="")
    out = []
    for line in lines:
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "same")
        if line.startswith(("+++", "---")):
            kind = "file"
        out.append((kind, line))
    return out


def card(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    """A pending item as the page shows it: masked like the record, so a secret default
    never reaches the HTML; has_default says whether "use the default" can be offered."""
    if pending is None or pending["kind"] not in CARDS:
        return None
    payload = pending["payload"]
    shown = mask_published(pending["kind"], payload)
    extra: dict[str, Any] = {}
    if pending["kind"] in ("approval", "recovery"):
        previous = (payload.get("previous_approved") or {}).get("compose")
        extra["diff"] = diff(previous, payload["compose"]) if payload.get("compose") else []
    if pending["kind"] == "approval":
        allowed = set(payload.get("allow") or [])
        rules = []
        for f in payload.get("findings") or []:
            rule = f"{f['service']}:{f['rule']}"
            if rule not in {r["rule"] for r in rules}:
                rules.append({**f, "rule_id": rule, "checked": rule in allowed})
        extra["rules"] = rules
    if pending["kind"] == "escalation":
        extra["guidance"] = ESCALATIONS.get(payload.get("reason"), "")
    return {
        **pending,
        "payload": shown,
        "template": CARDS[pending["kind"]],
        "has_default": payload.get("default") is not None,
        "command_id": uuid.uuid4().hex,  # made when rendered: a retry of this form reuses it
        **extra,
    }


def regions(run: dict[str, Any]) -> dict[str, Any]:
    """Template context for the status, summary, and pending regions of one snapshot."""
    return {"run": run, "card": card(run["pending"]), "panel_command_id": uuid.uuid4().hex}


def json_value(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


templates.filters["json_value"] = json_value

KIND_TITLES = {
    "question": "a question",
    "save_descriptors": "a descriptor to save",
    "approval": "the compose file to approve",
    "patch_approval": "a model's patch to approve",
    "escalation": "an escalation",
    "recovery": "a recovery plan",
}
templates.globals["kind_titles"] = KIND_TITLES

CHOICES = {
    "question": {"answer", "default", "abort"},
    "save_descriptors": {"saved", "abort"},
    "approval": {"approve", "reject"},
    "patch_approval": {"approve", "reject"},
    "escalation": {"retry", "abort"},
    "recovery": {"approve", "decline"},
}


class FormError(ValueError):
    pass


def decision_from_form(kind: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    """Turn a card's form into a decision, explicitly; RunService then validates it strictly."""
    choice = first(fields, "choice")
    if choice not in CHOICES.get(kind, set()):
        raise FormError("Choose one of the card's buttons.")
    if choice == "abort":
        return {"action": "abort"}
    if kind == "question":
        return {"answer": None} if choice == "default" else {"answer": first(fields, "answer")}
    if kind == "save_descriptors":
        return {"saved": True}
    if kind == "approval":
        if choice == "reject":
            return {"approve": False, "allow": []}
        return {"approve": True, "allow": fields.get("allow", [])}
    if kind == "patch_approval":
        return {"approve": choice == "approve"}
    if kind == "escalation":
        return {"action": "retry"}
    if choice == "decline":
        return {"approve": False}
    return {"approve": True, "acknowledged": fields.get("acknowledged", [])}


# 409 reasons as the card explains them; the card is then out of date.
CONFLICTS = {
    "stale": "This item changed since the page showed it.",
    "not_waiting": "This item was already answered, here or in another tab.",
    "not_reconcile": "This run is no longer waiting for reconciliation.",
    "plan_stale": "The Runner changed since the plan was made; review a new plan.",
    "check_needed": "Refresh while the Runner is idle first.",
    "runner_offline": "The Runner is offline.",
    "runner_unavailable": "The Runner did not answer.",
    "no_compose": "This run has no compose to resume; abandon it and start a new one.",
    "command_id_reused": "This form was already sent with other values. Reload the page.",
}


def safe_name(path: str, index: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", path.rsplit("/", 1)[-1]) or "file"
    return f"{index}-{base}"


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
                    # same-origin, not no-referrer: under no-referrer the browser sends
                    # "Origin: null" on its own POSTs, and the exact Origin check refuses them.
                    (b"referrer-policy", b"same-origin"),
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

    # after /runs/new, so "new" is never taken for a run id
    @app.get("/runs/{run_id}")
    async def run_page(request: Request, run_id: str) -> Response:
        s = page_session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        try:
            run = await service.snapshot(s.user, run_id)
        except RunError:
            return render("not_found.html", 404, user=s.user, csrf=s.csrf)
        return render("run.html", user=s.user, csrf=s.csrf, **regions(run))

    @app.get("/runs/{run_id}/fragments/state")
    async def state_fragment(request: Request, run_id: str) -> Response:
        """Status, summary, and pending regions from one read, with the seq they reflect.
        The page asks for it by itself, so it does not count as use of the session."""
        s = access.require(request)
        run = await service.snapshot(s.user, run_id)
        response = render("state.html", **regions(run))
        response.headers["Cagent-Event-Seq"] = str(run["event_seq"])
        response.headers["Cagent-Revision"] = str(run["revision"])
        return response

    @app.get("/runs/{run_id}/fragments/record")
    async def record_fragment(request: Request, run_id: str, after: int = 0) -> Response:
        """The next record entries after a cursor, with the cursor they reach."""
        s = access.require(request)
        rec = await service.record(s.user, run_id, after)
        response = render("record.html", rec=rec, after=after)
        response.headers["Cagent-Record-After"] = str(rec["after"])
        response.headers["Cagent-Record-More"] = "true" if rec["more"] else "false"
        return response

    @app.get("/runs/{run_id}/fragments/command/{command_id}")
    async def command_fragment(request: Request, run_id: str, command_id: str) -> Response:
        """One command's current result, for a decision the page still shows as open."""
        s = access.require(request)
        return render("command.html", c=await service.command(s.user, run_id, command_id))

    @app.post("/runs/{run_id}/commands")
    async def command(request: Request, run_id: str) -> Response:
        """A card or panel button. Success replaces the card with "accepted"; 409 and 422
        are shown in the card's error area so its input stays."""
        s = access.changing(request)
        fields = await form(request)
        action = first(fields, "action")
        body: dict[str, Any] = {"command_id": first(fields, "command_id"), "action": action}
        revision = first(fields, "expected_revision")
        if revision.isdigit():
            body["expected_revision"] = int(revision)
        error_target = f"#error-{first(fields, 'target') or 'panel'}"
        if action == "answer":
            body["pending_id"] = first(fields, "pending_id")
            try:
                body["decision"] = decision_from_form(first(fields, "kind"), fields)
            except FormError as e:
                return card_error(422, str(e), error_target)
        try:
            await service.submit(s.user, run_id, body)
        except RunError as e:
            if e.status == 409:
                return card_error(409, CONFLICTS.get(e.code, e.code), error_target, stale=True)
            if e.status == 422:
                return card_error(422, e.detail or e.code, error_target)
            raise
        return render("accepted.html", 202, command_id=body["command_id"], action=action)

    def card_error(status: int, message: str, target: str, stale: bool = False) -> Response:
        response = render("card_error.html", status, error=message, stale=stale)
        response.headers["HX-Retarget"] = target
        response.headers["HX-Reswap"] = "innerHTML"
        if stale:  # after the swap, so the message is in place before the card is retired
            response.headers["HX-Trigger-After-Swap"] = "cagent:stale"
        return response

    @app.get("/runs/{run_id}/pendings/{pending_id}/files/{index}")
    async def download(request: Request, run_id: str, pending_id: str, index: int) -> Response:
        """A file of a save_descriptors card, from what that card published; never a path."""
        s = access.require(request)
        path, text = await service.published_file(s.user, run_id, pending_id, index)
        return Response(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe_name(path, index)}"'},
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
