"""Sessions: login through a pre-session, expiry, logout, CSRF, and rate limits."""

import pytest

from cagent.auth import LoginError, RateLimited, Sessions

ALICE = "a" * 32


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def sessions(clock, **kw):
    return Sessions({"alice": ALICE, "bob": "b" * 32}, ttl_s=100, idle_ttl_s=10, clock=clock, **kw)


def login(s, client="1.2.3.4", token=ALICE):
    pre = s.new_pre(client)
    return s.login(pre.id, pre.csrf, token, client)


def test_login_exchanges_a_pre_session_for_a_new_session(clock):
    s = sessions(clock)
    pre = s.new_pre("c")
    session = s.login(pre.id, pre.csrf, ALICE, "c")
    assert session.user == "alice" and session.id not in (pre.id, ALICE)
    assert session.csrf != pre.csrf and s.get(session.id) is session
    assert pre.id not in s.pre
    with pytest.raises(LoginError):  # a pre-session is used once
        s.login(pre.id, pre.csrf, ALICE, "c")


@pytest.mark.parametrize("case", ["no pre", "wrong csrf", "wrong token", "expired pre"])
def test_every_login_failure_looks_the_same(clock, case):
    s = sessions(clock)
    pre = s.new_pre("c")
    args = {
        "no pre": (None, pre.csrf, ALICE),
        "wrong csrf": (pre.id, "x", ALICE),
        "wrong token": (pre.id, pre.csrf, "x" * 32),
        "expired pre": (pre.id, pre.csrf, ALICE),
    }[case]
    if case == "expired pre":
        clock.now = 601
    with pytest.raises(LoginError, match="^login failed$"):
        s.login(*args, "c")


def test_sessions_expire_when_idle_or_too_old(clock):
    s = sessions(clock)
    session = login(s)
    clock.now = 9
    s.touch(session)
    clock.now = 18
    assert s.get(session.id) is session  # 9 s since last use
    clock.now = 19.5
    assert s.get(session.id) is None and session.revoked.is_set()

    session = login(s)  # at 19.5, so it ends at 119.5 however busy it is
    for t in range(25, 120, 5):
        clock.now = t
        assert s.get(session.id) is session
        s.touch(session)
    clock.now = 120
    assert s.get(session.id) is None


def test_reading_does_not_keep_a_session_alive(clock):
    s = sessions(clock)
    session = login(s)
    for t in range(1, 10):
        clock.now = t
        assert s.get(session.id) is session  # get never touches
    clock.now = 10
    assert s.get(session.id) is None


def test_logout_revokes_at_once(clock):
    s = sessions(clock)
    session = login(s)
    s.logout(session.id)
    assert session.revoked.is_set() and s.get(session.id) is None


def test_csrf_tokens_are_compared_exactly(clock):
    s = sessions(clock)
    session = login(s)
    assert Sessions.csrf_ok(session, session.csrf)
    assert not Sessions.csrf_ok(session, None) and not Sessions.csrf_ok(session, "x")


def test_failed_logins_are_rate_limited_per_client(clock):
    s = sessions(clock, max_failures=2)
    for _ in range(2):
        with pytest.raises(LoginError):
            login(s, token="x" * 32)
    with pytest.raises(RateLimited):
        login(s)  # even the right token, for a while
    assert login(s, client="5.6.7.8").user == "alice"
    clock.now = 61
    assert login(s).user == "alice"


def test_pre_sessions_are_bounded(clock):
    s = sessions(clock, max_pre_per_client=2, max_pre=3)
    s.new_pre("c1")
    s.new_pre("c1")
    with pytest.raises(RateLimited):
        s.new_pre("c1")
    s.new_pre("c2")
    with pytest.raises(RateLimited):
        s.new_pre("c3")  # the table is full
    clock.now = 601  # expired pre-sessions make room
    s.new_pre("c3")
    assert len(s.pre) == 1
