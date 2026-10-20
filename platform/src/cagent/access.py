"""Request checks shared by the JSON API and the HTML pages: the session cookie, the exact
Origin, the CSRF header, cookie attributes, and the client address used for rate limits.
Proxy headers are never consulted."""

from fastapi import Request
from fastapi.responses import Response

from cagent.auth import Session, Sessions

SESSION_COOKIE = "cagent_session"
PRE_COOKIE = "cagent_pre"


class Denied(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


class Access:
    def __init__(self, sessions: Sessions, origin: str, secure_cookies: bool):
        self.sessions, self.origin, self.secure = sessions, origin, secure_cookies

    def cookie(self, response: Response, name: str, value: str, max_age: float) -> None:
        response.set_cookie(
            name,
            value,
            max_age=int(max_age),
            httponly=True,
            samesite="strict",
            secure=self.secure,
            path="/",
        )

    def same_origin(self, request: Request) -> None:
        if request.headers.get("origin") != self.origin:  # missing and "null" never match
            raise Denied(403, "bad_origin")

    def session(self, request: Request) -> Session | None:
        """The live session, without counting the request as use."""
        return self.sessions.get(request.cookies.get(SESSION_COOKIE))

    def require(self, request: Request) -> Session:
        s = self.session(request)
        if s is None:
            raise Denied(401, "login_required")
        return s

    def changing(self, request: Request) -> Session:
        """A state-changing request: session, exact Origin, CSRF header. Counts as use."""
        s = self.require(request)
        self.same_origin(request)
        if not Sessions.csrf_ok(s, request.headers.get("x-csrf-token")):
            raise Denied(403, "bad_csrf")
        self.sessions.touch(s)
        return s

    @staticmethod
    def client(request: Request) -> str:
        return request.client.host if request.client else "unknown"
