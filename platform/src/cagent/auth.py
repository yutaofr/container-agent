"""Browser sessions for static users. The authentication step (a user token posted to the
login form) is replaceable; user ids, run ownership, and authorization stay the same when
SSO replaces it.

Sessions live in memory: a restart logs everyone out, and runs are unaffected. A session id
is random and never derived from the token; the cookie holds only the id. Login itself is
protected against CSRF with a short-lived pre-session, and both failures and pre-session
creation are rate-limited per client so the tables cannot grow without bound.
"""

import asyncio
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


class LoginError(Exception):
    """Login failed. The message is the same whatever the reason."""


class RateLimited(Exception):
    """Too many attempts from one client."""


@dataclass
class PreSession:
    id: str
    csrf: str
    created: float


@dataclass
class Session:
    id: str
    user: str
    csrf: str
    created: float
    last_seen: float
    # Set on logout or when found expired; open event streams close on it.
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class _Window:
    """At most limit events per client within window seconds."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = {}

    def full(self, client: str, now: float) -> bool:
        hits = self.hits.get(client)
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if hits is not None and not hits:
            del self.hits[client]
            return False
        return hits is not None and len(hits) >= self.limit

    def add(self, client: str, now: float) -> None:
        self.hits.setdefault(client, deque()).append(now)


class Sessions:
    def __init__(
        self,
        tokens: Mapping[str, str],
        ttl_s: float,
        idle_ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
        pre_ttl_s: float = 600,
        max_pre: int = 1000,
        max_failures: int = 5,
        max_pre_per_client: int = 30,
        window_s: float = 60,
    ):
        """tokens maps each user to their login token."""
        self.tokens = dict(tokens)
        self.ttl, self.idle, self.clock = ttl_s, idle_ttl_s, clock
        self.pre_ttl, self.max_pre = pre_ttl_s, max_pre
        self.pre: dict[str, PreSession] = {}
        self.sessions: dict[str, Session] = {}
        self._failures = _Window(max_failures, window_s)
        self._pre_rate = _Window(max_pre_per_client, window_s)

    # --- login -------------------------------------------------------------------------------

    def new_pre(self, client: str) -> PreSession:
        now = self.clock()
        if self._pre_rate.full(client, now):
            raise RateLimited(client)
        self._pre_rate.add(client, now)
        for pid in [p.id for p in self.pre.values() if now - p.created > self.pre_ttl]:
            del self.pre[pid]
        if len(self.pre) >= self.max_pre:
            raise RateLimited("too many pending logins")
        pre = PreSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now)
        self.pre[pre.id] = pre
        return pre

    def login(self, pre_id: str | None, csrf: str, token: str, client: str) -> Session:
        """Exchange a valid pre-session, its CSRF token, and a user token for a new session.
        The pre-session is used up either way."""
        now = self.clock()
        if self._failures.full(client, now):
            raise RateLimited(client)
        pre = self.pre.pop(pre_id, None) if pre_id else None
        valid_pre = (
            pre is not None
            and now - pre.created <= self.pre_ttl
            and hmac.compare_digest(pre.csrf.encode(), csrf.encode())
        )
        user = None
        for name, expected in self.tokens.items():  # compare against every user, always
            if hmac.compare_digest(expected.encode(), token.encode()):
                user = name
        if not valid_pre or user is None:
            self._failures.add(client, now)
            raise LoginError("login failed")
        session = Session(secrets.token_urlsafe(32), user, secrets.token_urlsafe(32), now, now)
        self.sessions[session.id] = session
        return session

    def logout(self, session_id: str) -> None:
        if session := self.sessions.pop(session_id, None):
            session.revoked.set()

    # --- use ---------------------------------------------------------------------------------

    def deadline(self, session: Session) -> float:
        """When the session expires unless it is used again."""
        return min(session.created + self.ttl, session.last_seen + self.idle)

    def get(self, session_id: str | None) -> Session | None:
        """The live session, without counting this as use. An expired one is revoked."""
        session = self.sessions.get(session_id) if session_id else None
        if session is None:
            return None
        if self.clock() >= self.deadline(session):
            self.logout(session.id)
            return None
        return session

    def touch(self, session: Session) -> None:
        """A request the user made counts as use; event-stream traffic does not."""
        session.last_seen = self.clock()

    @staticmethod
    def csrf_ok(session: Session, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(
            session.csrf.encode(), presented.encode()
        )
