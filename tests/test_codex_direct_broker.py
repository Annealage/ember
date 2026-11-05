"""Tests for ``BrokerCredentialSource`` against a loopback stand-in for omp's broker.

The stand-in implements the broker's documented snapshot contract (bearer
check, quoted generation ``ETag``, ``If-None-Match`` and ``wait`` long polling
with ``200`` on change and ``304`` on timeout, the ``wait`` clamp) and records
every request it receives. Two invariants run through the whole suite. Every
test that uses the stand-in ends by checking that the only requests it ever
saw were ``GET /v1/healthz`` and ``GET /v1/snapshot``, so a client that sent a
refresh, an upsert or any other mutation would fail whichever test provoked it.
And no test text, repr or exception may contain the bearer or an access token.

Nothing here touches the network beyond 127.0.0.1, a real credential, or omp.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.client
import json
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, NamedTuple
from urllib.parse import parse_qs, urlsplit

import pytest

from annealage_ember.codex_direct.credentials import (
    BrokerCredentialSource,
    CredentialLease,
    CredentialSource,
    CredentialUnavailable,
)

BEARER = "bearer-SECRET-7c1d"
WRONG_BEARER = "wrong-bearer-SECRET-90ab"
ACCESS_A = "access-A-SECRET-4411"
ACCESS_B = "access-B-SECRET-5522"
ACCESS_NEW = "access-NEW-SECRET-6633"
ACCESS_OLD = "access-OLD-SECRET-7744"

IDENTITY_A = "email:a@example.com"
IDENTITY_B = "email:b@example.com"
PROVIDER = "openai-codex"

ALLOWED_ROUTES = {("GET", "/v1/healthz"), ("GET", "/v1/snapshot")}
# Every string that must never surface in a repr, str, detail or traceback.
SECRETS = [BEARER, WRONG_BEARER, ACCESS_A, ACCESS_B, ACCESS_NEW, ACCESS_OLD]

HOUR_MS = 3_600_000


def make_jwt(account_id: str | None, *, claim: str = "https://api.openai.com/auth") -> str:
    def b64(obj: object) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    payload: dict[str, object] = {"sub": "user-1"}
    if account_id is not None:
        payload[claim] = {"chatgpt_account_id": account_id}
    token = "%s.%s.sig-SECRET-%d" % (b64({"alg": "none", "typ": "JWT"}), b64(payload), len(SECRETS))
    SECRETS.append(token)
    return token


class Request(NamedTuple):
    method: str
    path: str
    query: str
    headers: dict[str, str]  # lower-cased names


def parse_generation_tag(header: str | None) -> int | None:
    """The broker's tag grammar: bare, quoted or weak-quoted non-negative integer."""
    if not header:
        return None
    value = header.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    try:
        tag = int(value)
    except ValueError:
        return None
    return tag if tag >= 0 else None


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client that timed out is expected
        pass


class StandInBroker:
    """A loopback broker: documented snapshot semantics plus a request log."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.generation = 1
        self.credentials: list[dict] = []
        self.skew_ms = 0  # the broker's clock minus this machine's wall clock
        self.bearer = BEARER
        self.healthz_body: object = {"ok": True, "version": "stand-in-1"}
        # A hook that may answer a snapshot request itself: (status, body, headers).
        self.on_snapshot: Callable[[Request], tuple[int, bytes, dict[str, str]] | None] | None = (
            None
        )
        self.stall_snapshot = False
        # A hook that writes the reply itself (to stall or drip); True means it did.
        self.on_raw: Callable[[BaseHTTPRequestHandler, Request], bool] | None = None
        self._cond = threading.Condition()
        self._closing = False
        self._release = threading.Event()
        standin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_any(self):
                split = urlsplit(self.path)
                request = Request(
                    self.command,
                    split.path,
                    split.query,
                    {k.lower(): v for k, v in self.headers.items()},
                )
                standin.requests.append(request)
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if standin.on_raw is not None and standin.on_raw(self, request):
                    return
                status, body, headers = standin.answer(request)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def stop(self) -> None:
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        self._release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    # -- fixtures for the tests -----------------------------------------

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.skew_ms

    def oauth(
        self,
        entry_id: int,
        identity: str | None,
        access: str,
        account: str,
        expires_in_ms: int,
        *,
        provider: str = PROVIDER,
    ) -> dict:
        return {
            "id": entry_id,
            "provider": provider,
            "credential": {
                "type": "oauth",
                "access": access,
                "refresh": "__remote__",
                "expires": self.now_ms() + expires_in_ms,
                "accountId": account,
                "email": "someone@example.com",
            },
            "identityKey": identity,
            "rotatesInMs": None,
        }

    def publish(self, credentials: list[dict]) -> None:
        """Replace the credentials and bump the generation, waking long polls."""
        with self._cond:
            self.credentials = credentials
            self.generation += 1
            self._cond.notify_all()

    # -- the broker itself ---------------------------------------------

    def answer(self, request: Request) -> tuple[int, bytes, dict[str, str]]:
        if request.method == "GET" and request.path == "/v1/healthz":
            return self._json(200, self.healthz_body)
        if request.headers.get("authorization") != "Bearer " + self.bearer:
            return self._json(401, {"error": "unauthorized"})
        if request.method == "GET" and request.path == "/v1/snapshot":
            return self._snapshot(request)
        # Any mutating route that reaches here is a client bug; answer as the
        # real broker would, so a client that sent one would see it succeed.
        return self._json(200, {"ok": True, "entry": {}})

    def _json(self, status: int, body: object, headers: dict[str, str] | None = None):
        merged = {"Content-Type": "application/json", **(headers or {})}
        return status, json.dumps(body).encode(), merged

    def _snapshot_reply(self) -> tuple[int, bytes, dict[str, str]]:
        now = self.now_ms()
        body = {
            "generation": self.generation,
            "generatedAt": now,
            "serverNowMs": now,
            "refresher": {
                "enabled": True,
                "intervalMs": 60_000,
                "skewMs": 300_000,
                "nextSweepInMs": 1000,
            },
            "credentials": self.credentials,
        }
        return self._json(
            200, body, {"ETag": '"%d"' % self.generation, "Cache-Control": "no-store"}
        )

    def _snapshot(self, request: Request) -> tuple[int, bytes, dict[str, str]]:
        if self.on_snapshot is not None:
            hooked = self.on_snapshot(request)
            if hooked is not None:
                return hooked
        if self.stall_snapshot:
            self._release.wait(10)
        raw_wait = parse_qs(request.query).get("wait", [""])[0]
        try:
            wait_ms = max(0, min(30_000, int(float(raw_wait))))
        except ValueError:
            wait_ms = 0
        tag = parse_generation_tag(request.headers.get("if-none-match"))
        with self._cond:
            if tag is None or tag != self.generation or wait_ms <= 0:
                return self._snapshot_reply()
            self._cond.wait_for(
                lambda: self.generation != tag or self._closing, timeout=wait_ms / 1000
            )
            if self.generation != tag:
                return self._snapshot_reply()
            return 304, b"", {"ETag": '"%d"' % self.generation, "Cache-Control": "no-store"}

    def lease_requests(self) -> list[tuple[str, str]]:
        return [(r.method, r.path) for r in self.requests]


@pytest.fixture
def broker():
    standin = StandInBroker()
    yield standin
    standin.stop()
    seen = [(r.method, r.path) for r in standin.requests]
    stray = [route for route in seen if route not in ALLOWED_ROUTES]
    assert not stray, "the client reached beyond GET /v1/healthz and GET /v1/snapshot: %r" % stray


@pytest.fixture
def connects(monkeypatch):
    """Every socket connection attempt a connection object makes."""
    attempts: list[tuple[str, int]] = []
    real = http.client.HTTPConnection.connect

    def spy(self):
        attempts.append((self.host, self.port))
        return real(self)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", spy)
    return attempts


def source_for(
    broker: StandInBroker, identity: str = IDENTITY_A, **overrides
) -> BrokerCredentialSource:
    options = {"identity_key": identity, "bearer": BEARER, "request_timeout": 5.0}
    options.update(overrides)
    return BrokerCredentialSource(broker.url, **options)


def assert_no_secrets(*things: object) -> None:
    for thing in things:
        if isinstance(thing, BaseException):
            text = "".join(traceback.format_exception(type(thing), thing, thing.__traceback__))
            text += repr(thing) + str(thing) + repr(thing.args)
        else:
            text = str(thing) + repr(thing)
        for secret in SECRETS:
            assert secret not in text, "a secret leaked into %r" % type(thing).__name__


def unavailable(call: Callable[[], object], reason: str) -> CredentialUnavailable:
    with pytest.raises(CredentialUnavailable) as info:
        call()
    assert info.value.reason == reason, str(info.value)
    assert_no_secrets(info.value)
    return info.value


def lease_of(source: BrokerCredentialSource, **kwargs) -> CredentialLease:
    params = {"min_remaining_ms": 60_000, "wait_s": 0}
    params.update(kwargs)
    return source.lease(**params)


# -- selection ------------------------------------------------------------


def test_selects_each_account_by_identity_key(broker):
    broker.credentials = [
        broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS),
        broker.oauth(2, IDENTITY_B, ACCESS_B, "acct-b", HOUR_MS),
    ]
    before = time.monotonic()
    lease_a = lease_of(source_for(broker, IDENTITY_A))
    after = time.monotonic()
    lease_b = lease_of(source_for(broker, IDENTITY_B))

    assert lease_a.access_token == ACCESS_A
    assert lease_a.account_id == "acct-a"
    assert lease_a.credential_id == 1
    assert lease_a.identity_key == IDENTITY_A
    assert lease_a.generation == broker.generation
    assert lease_a.expires_at_ms == broker.credentials[0]["credential"]["expires"]
    assert lease_a.token_digest == hashlib.sha256(ACCESS_A.encode()).hexdigest()
    assert before <= lease_a.obtained_monotonic <= after
    assert abs(lease_a.server_now_ms - broker.now_ms()) < 5000
    assert abs(lease_a.remaining_ms() - HOUR_MS) < 5000

    assert (lease_b.access_token, lease_b.account_id, lease_b.credential_id) == (
        ACCESS_B,
        "acct-b",
        2,
    )
    assert lease_b.token_digest == hashlib.sha256(ACCESS_B.encode()).hexdigest()


def test_a_plain_lease_is_one_authenticated_snapshot_request(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    lease_of(source_for(broker))
    assert broker.lease_requests() == [("GET", "/v1/snapshot")]
    request = broker.requests[0]
    assert request.query == ""
    assert request.headers["authorization"] == "Bearer " + BEARER
    assert request.headers["accept-encoding"] == "identity"
    assert "if-none-match" not in request.headers


def test_no_match_when_the_identity_is_absent(broker):
    # A disabled or logged-out account is simply absent from the snapshot; its
    # tombstone is on a route this client may not call.
    broker.credentials = [broker.oauth(2, IDENTITY_B, ACCESS_B, "acct-b", HOUR_MS)]
    unavailable(lambda: lease_of(source_for(broker, IDENTITY_A)), "no_match")


def test_no_match_on_an_empty_snapshot(broker):
    unavailable(lambda: lease_of(source_for(broker)), "no_match")


def test_ambiguous_when_two_rows_share_the_identity_key(broker):
    broker.credentials = [
        broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS),
        broker.oauth(2, IDENTITY_A, ACCESS_B, "acct-b", HOUR_MS),
    ]
    error = unavailable(lambda: lease_of(source_for(broker)), "ambiguous")
    assert "2" in error.detail


def _other_provider(entry):
    entry["provider"] = "anthropic"


def _api_key(entry):
    entry["credential"] = {"type": "api_key", "key": ACCESS_B}


def _empty_access(entry):
    entry["credential"]["access"] = ""


def _no_access(entry):
    del entry["credential"]["access"]


def _no_account(entry):
    del entry["credential"]["accountId"]


def _empty_account(entry):
    entry["credential"]["accountId"] = ""


def _null_identity(entry):
    entry["identityKey"] = None


def _other_identity(entry):
    entry["identityKey"] = IDENTITY_B


def _not_a_row(entry):
    entry.clear()


IGNORED_ROWS = [
    _other_provider,
    _api_key,
    _empty_access,
    _no_access,
    _no_account,
    _empty_account,
    _null_identity,
    _other_identity,
    _not_a_row,
]


@pytest.mark.parametrize("mutate", IGNORED_ROWS, ids=lambda f: f.__name__.strip("_"))
def test_rows_that_do_not_qualify_are_ignored(broker, mutate):
    decoy = broker.oauth(9, IDENTITY_A, ACCESS_B, "acct-decoy", HOUR_MS)
    mutate(decoy)
    broker.credentials = [decoy]
    unavailable(lambda: lease_of(source_for(broker)), "no_match")

    # Alongside the one qualifying row, the decoy neither wins nor makes it ambiguous.
    broker.credentials = [decoy, broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    assert lease_of(source_for(broker)).access_token == ACCESS_A


@pytest.mark.parametrize(
    "identity",
    [
        "email:A@example.com",
        "email:a@example.com|org:x",
        " email:a@example.com",
        "email:a@example.com ",
    ],
)
def test_identity_key_must_match_exactly(broker, identity):
    broker.credentials = [broker.oauth(1, identity, ACCESS_A, "acct-a", HOUR_MS)]
    unavailable(lambda: lease_of(source_for(broker)), "no_match")


def test_the_provider_is_configurable(broker):
    broker.credentials = [
        broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS),
        broker.oauth(2, IDENTITY_A, ACCESS_B, "acct-b", HOUR_MS, provider="other-codex"),
    ]
    assert lease_of(source_for(broker)).access_token == ACCESS_A
    assert lease_of(source_for(broker, provider="other-codex")).access_token == ACCESS_B


# -- the JWT cross-check ---------------------------------------------------


def test_a_jwt_whose_account_claim_differs_is_a_mismatch(broker):
    token = make_jwt("acct-other")
    broker.credentials = [broker.oauth(1, IDENTITY_A, token, "acct-a", HOUR_MS)]
    error = unavailable(lambda: lease_of(source_for(broker)), "account_mismatch")
    assert "acct-other" not in error.detail


def test_a_jwt_whose_account_claim_agrees_is_accepted(broker):
    token = make_jwt("acct-a")
    broker.credentials = [broker.oauth(1, IDENTITY_A, token, "acct-a", HOUR_MS)]
    assert lease_of(source_for(broker)).access_token == token


@pytest.mark.parametrize(
    "token",
    [
        make_jwt(None),
        make_jwt("acct-other", claim="https://example.com/other"),
        "not.a.jwt",
        "only.two",
        "x." + base64.urlsafe_b64encode(b"[1, 2]").decode() + ".y",
        "x." + base64.urlsafe_b64encode(b'{"https://api.openai.com/auth": "text"}').decode() + ".y",
        ACCESS_A,
    ],
    ids=[
        "no-claim",
        "other-claim",
        "not-jwt",
        "two-parts",
        "array-payload",
        "claim-not-object",
        "opaque",
    ],
)
def test_tokens_without_a_checkable_claim_are_accepted(broker, token):
    broker.credentials = [broker.oauth(1, IDENTITY_A, token, "acct-a", HOUR_MS)]
    assert lease_of(source_for(broker)).access_token == token


# -- the broker's clock ----------------------------------------------------


def test_remaining_time_follows_the_broker_clock_when_it_runs_behind(broker):
    # The broker thinks it is a decade ago. Judged against this machine's wall
    # clock the token expired years back; against the broker's it has two minutes.
    broker.skew_ms = -10 * 365 * 24 * HOUR_MS
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", 120_000)]
    lease = lease_of(source_for(broker), min_remaining_ms=60_000)
    assert 100_000 < lease.remaining_ms() <= 120_000
    assert lease.server_now_ms < int(time.time() * 1000) - 9 * 365 * 24 * HOUR_MS


def test_remaining_time_follows_the_broker_clock_when_it_runs_ahead(broker):
    # The broker's clock is a decade ahead and its token expired a second ago.
    # Against this machine's wall clock that expiry is still years away.
    broker.skew_ms = 10 * 365 * 24 * HOUR_MS
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", -1000)]
    unavailable(lambda: lease_of(source_for(broker)), "expired")


def test_remaining_ms_counts_monotonic_time_since_receipt():
    lease = CredentialLease(
        access_token=ACCESS_A,
        account_id="acct-a",
        credential_id=1,
        identity_key=IDENTITY_A,
        expires_at_ms=1_000_000_000_000 + 120_000,
        generation=3,
        token_digest="d",
        obtained_monotonic=100.0,
        server_now_ms=1_000_000_000_000,
    )
    assert lease.remaining_ms(100.0) == 120_000
    assert lease.remaining_ms(105.0) == 115_000
    assert lease.remaining_ms(220.0) == 0
    assert lease.remaining_ms(221.5) == -1500
    # A reading from before receipt cannot add time the broker never granted.
    assert lease.remaining_ms(90.0) == 120_000
    # Elapsed time rounds up, so the remainder rounds down.
    assert lease.remaining_ms(100.0004) == 119_999

    default_now = dataclasses.replace(lease, obtained_monotonic=time.monotonic() - 10.0)
    assert 109_000 < default_now.remaining_ms() <= 110_000


def test_a_lease_is_immutable():
    lease = CredentialLease(ACCESS_A, "a", 1, IDENTITY_A, 2, 3, "d", 0.0, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lease.account_id = "other"  # type: ignore[misc]


# -- waiting for omp to rotate ---------------------------------------------


def _rotate_after(broker: StandInBroker, delay: float, credentials: Callable[[], list[dict]]):
    def run():
        time.sleep(delay)
        broker.publish(credentials())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_short_token_waits_for_a_newer_generation_and_returns_its_token(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 5_000)]
    rotator = _rotate_after(
        broker, 0.3, lambda: [broker.oauth(1, IDENTITY_A, ACCESS_NEW, "acct-a", HOUR_MS)]
    )
    started = time.monotonic()
    lease = lease_of(source_for(broker), min_remaining_ms=60_000, wait_s=5)
    rotator.join()

    assert time.monotonic() - started < 4
    assert lease.access_token == ACCESS_NEW
    assert lease.generation == 2
    assert lease.remaining_ms() > 3_000_000
    assert broker.lease_requests() == [("GET", "/v1/snapshot"), ("GET", "/v1/snapshot")]
    first, poll = broker.requests
    assert first.query == "" and "if-none-match" not in first.headers
    wait = parse_qs(poll.query)
    assert list(wait) == ["wait"] and 0 < int(wait["wait"][0]) <= 5000
    assert poll.headers["if-none-match"] == '"1"'
    assert poll.headers["authorization"] == "Bearer " + BEARER


def test_an_expired_token_also_waits_for_rotation(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", -5_000)]
    rotator = _rotate_after(
        broker, 0.2, lambda: [broker.oauth(1, IDENTITY_A, ACCESS_NEW, "acct-a", HOUR_MS)]
    )
    lease = lease_of(source_for(broker), wait_s=5)
    rotator.join()
    assert lease.access_token == ACCESS_NEW


def test_a_single_long_poll_never_asks_for_more_than_the_broker_clamp(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 1_000)]
    rotator = _rotate_after(
        broker, 0.1, lambda: [broker.oauth(1, IDENTITY_A, ACCESS_NEW, "acct-a", HOUR_MS)]
    )
    lease_of(source_for(broker), wait_s=100)
    rotator.join()
    assert parse_qs(broker.requests[1].query)["wait"] == ["30000"]


def test_the_wait_lapsing_unchanged_raises_expiring(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 20_000)]
    # The socket timeout is shorter than the wait: it must stretch to cover a long poll.
    source = source_for(broker, request_timeout=0.2)
    started = time.monotonic()
    error = unavailable(lambda: lease_of(source, min_remaining_ms=60_000, wait_s=0.6), "expiring")
    elapsed = time.monotonic() - started
    assert 0.5 <= elapsed < 4
    assert "ms left" in error.detail
    routes = broker.lease_requests()
    assert routes[0] == ("GET", "/v1/snapshot") and 2 <= len(routes) <= 4
    assert all(route == ("GET", "/v1/snapshot") for route in routes)
    assert all("wait" in parse_qs(r.query) for r in broker.requests[1:])


def test_no_wait_means_one_request_and_expiring(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 20_000)]
    unavailable(lambda: lease_of(source_for(broker), min_remaining_ms=60_000, wait_s=0), "expiring")
    assert broker.lease_requests() == [("GET", "/v1/snapshot")]


def test_a_token_already_expired_raises_expired(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", -1_000)]
    unavailable(lambda: lease_of(source_for(broker)), "expired")
    assert broker.lease_requests() == [("GET", "/v1/snapshot")]


def test_a_token_expiring_this_instant_is_expired_not_leased(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 0)]
    unavailable(lambda: lease_of(source_for(broker), min_remaining_ms=0), "expired")


def test_a_zero_margin_still_needs_time_on_the_token(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", 30_000)]
    assert lease_of(source_for(broker), min_remaining_ms=0).access_token == ACCESS_A


def test_a_waiting_lease_stays_expired_when_nothing_rotates(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", -1_000)]
    unavailable(lambda: lease_of(source_for(broker), wait_s=0.4), "expired")
    assert set(broker.lease_requests()) == {("GET", "/v1/snapshot")}


def test_the_account_vanishing_during_the_wait_is_no_match(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 5_000)]
    rotator = _rotate_after(broker, 0.2, list)
    unavailable(lambda: lease_of(source_for(broker), wait_s=5), "no_match")
    rotator.join()


def test_a_rotation_that_changes_the_account_is_a_mismatch(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 5_000)]
    token = make_jwt("acct-other")
    rotator = _rotate_after(
        broker, 0.2, lambda: [broker.oauth(1, IDENTITY_A, token, "acct-a", HOUR_MS)]
    )
    unavailable(lambda: lease_of(source_for(broker), wait_s=5), "account_mismatch")
    rotator.join()


def test_a_499_during_the_wait_is_a_broker_error(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 5_000)]

    def hook(request):
        if "if-none-match" in request.headers:
            return 499, b"", {"ETag": '"1"'}
        return None

    broker.on_snapshot = hook
    error = unavailable(lambda: lease_of(source_for(broker), wait_s=5), "broker_error")
    assert "499" in error.detail
    assert len(broker.requests) == 2  # no retry


def test_a_200_without_a_new_generation_during_the_wait_is_a_broker_error(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 5_000)]

    def hook(request):
        if "if-none-match" in request.headers:
            return broker._snapshot_reply()
        return None

    broker.on_snapshot = hook
    unavailable(lambda: lease_of(source_for(broker), wait_s=5), "broker_error")
    assert len(broker.requests) == 2


def test_a_lease_never_sends_anything_but_snapshot_gets_however_short_the_token(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", -1_000)]
    unavailable(lambda: lease_of(source_for(broker), wait_s=0.3), "expired")
    assert {r.method for r in broker.requests} == {"GET"}
    assert {r.path for r in broker.requests} == {"/v1/snapshot"}


# -- broker failures -------------------------------------------------------


def test_a_rejected_bearer_is_a_broker_error(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    error = unavailable(lambda: lease_of(source_for(broker, bearer=WRONG_BEARER)), "broker_error")
    assert "401" in error.detail
    assert len(broker.requests) == 1  # no retry


def test_a_server_error_whose_body_echoes_secrets_leaks_nothing(broker):
    body = json.dumps({"error": "boom", "bearer": BEARER, "access": ACCESS_A}).encode()
    broker.on_snapshot = lambda request: (500, body, {"Content-Type": "application/json"})
    error = unavailable(lambda: lease_of(source_for(broker)), "broker_error")
    assert "500" in error.detail
    assert len(broker.requests) == 1


def test_a_redirect_is_not_followed(broker):
    broker.on_snapshot = lambda request: (302, b"", {"Location": "/v1/credential/1/refresh"})
    error = unavailable(lambda: lease_of(source_for(broker)), "broker_error")
    assert "302" in error.detail
    assert broker.lease_requests() == [("GET", "/v1/snapshot")]


def test_a_304_to_an_unconditional_request_is_a_broker_error(broker):
    broker.on_snapshot = lambda request: (304, b"", {"ETag": '"1"'})
    unavailable(lambda: lease_of(source_for(broker)), "broker_error")


@pytest.mark.parametrize(
    "body",
    [
        b"not json {" + BEARER.encode(),
        b"[]",
        b"null",
        b'{"generation": 1, "credentials": []}',
        b'{"generation": "1", "serverNowMs": 5, "credentials": []}',
        b'{"generation": true, "serverNowMs": 5, "credentials": []}',
        b'{"generation": -1, "serverNowMs": 5, "credentials": []}',
        b'{"generation": 1, "serverNowMs": 5, "credentials": {}}',
        b'{"generation": 1, "serverNowMs": "5", "credentials": []}',
        b"\xff\xfe",
    ],
    ids=[
        "invalid-json",
        "array",
        "null",
        "no-server-now",
        "string-generation",
        "bool-generation",
        "negative-generation",
        "credentials-not-list",
        "string-server-now",
        "not-utf8",
    ],
)
def test_a_malformed_snapshot_is_a_broker_error(broker, body):
    broker.on_snapshot = lambda request: (200, body, {"Content-Type": "application/json"})
    unavailable(lambda: lease_of(source_for(broker)), "broker_error")


@pytest.mark.parametrize("field", ["expires", "id"])
@pytest.mark.parametrize("bad", ["soon", None, True])
def test_a_matching_row_with_a_malformed_field_is_a_broker_error(broker, field, bad):
    entry = broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)
    if field == "expires":
        entry["credential"]["expires"] = bad
    else:
        entry["id"] = bad
    broker.credentials = [entry]
    unavailable(lambda: lease_of(source_for(broker)), "broker_error")


def test_a_content_encoded_reply_is_refused(broker):
    broker.on_snapshot = lambda request: (200, b"{}", {"Content-Encoding": "gzip"})
    unavailable(lambda: lease_of(source_for(broker)), "broker_error")


def test_an_oversized_reply_is_refused(broker):
    broker.on_snapshot = lambda request: (200, b" " * (8 * 1024 * 1024 + 1), {})
    unavailable(lambda: lease_of(source_for(broker)), "broker_error")


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_broker_that_is_down_is_unreachable():
    source = BrokerCredentialSource(
        "http://127.0.0.1:%d" % _closed_port(), identity_key=IDENTITY_A, bearer=BEARER
    )
    unavailable(lambda: lease_of(source), "broker_unreachable")
    unavailable(source.healthz, "broker_unreachable")


def test_a_broker_that_never_answers_is_unreachable_after_the_timeout(broker):
    broker.stall_snapshot = True
    source = source_for(broker, request_timeout=0.3)
    started = time.monotonic()
    unavailable(lambda: lease_of(source), "broker_unreachable")
    assert time.monotonic() - started < 3
    assert len(broker.requests) == 1  # no retry


# -- the route allowlist ---------------------------------------------------

FORBIDDEN_ROUTES = [
    ("POST", "/v1/credential"),
    ("POST", "/v1/credential/1/refresh"),
    ("POST", "/v1/credential/1/refresh?reason=auth-recovery"),
    ("POST", "/v1/credential/1/disable"),
    ("POST", "/v1/credential/1/block"),
    ("DELETE", "/v1/credential/1/block"),
    ("DELETE", "/v1/credential/1/blocks"),
    ("DELETE", "/v1/credential"),
    ("PUT", "/v1/credential"),
    ("PATCH", "/v1/snapshot"),
    ("POST", "/v1/snapshot"),
    ("POST", "/v1/healthz"),
    ("DELETE", "/v1/snapshot"),
    ("HEAD", "/v1/snapshot"),
    ("get", "/v1/snapshot"),
    ("GET", "/v1/usage"),
    ("GET", "/v1/usage/history"),
    ("GET", "/v1/usage/clients"),
    ("POST", "/v1/usage/observed"),
    ("POST", "/v1/usage/stale"),
    ("GET", "/v1/credentials/disabled"),
    ("GET", "/v1/snapshot/stream"),
    ("GET", "/v1/snapshot/"),
    ("GET", "/v1/snapshot?wait=100"),
    ("GET", "/v1/snapshot#x"),
    ("GET", "/v1/snapshot/../credential"),
    ("GET", "//v1/snapshot"),
    ("GET", "/V1/snapshot"),
    ("GET", "/v1/healthz/"),
    ("GET", "v1/snapshot"),
    ("GET", "http://127.0.0.1/v1/snapshot"),
    ("GET", "/v1/snapshot%2f"),
    ("GET", "/v1/snap\r\nX-Injected: 1"),
    ("GET", ""),
    ("GET", "/"),
]


@pytest.mark.parametrize(
    ("method", "path"), FORBIDDEN_ROUTES, ids=lambda v: v.replace("\r\n", "CRLF") or "empty"
)
def test_a_forbidden_route_is_refused_before_any_socket_opens(broker, connects, method, path):
    source = source_for(broker)
    error = unavailable(lambda: source._send(method, path), "forbidden_route")
    # The refusal text is fixed, so a caller-supplied path is never echoed back.
    assert error.detail == "only GET /v1/healthz and GET /v1/snapshot are permitted"
    assert connects == []
    assert broker.requests == []


@pytest.mark.parametrize(
    ("method", "path"), [r for r in FORBIDDEN_ROUTES if r[0] == "GET"], ids=lambda v: v or "empty"
)
def test_the_get_helper_enforces_the_same_allowlist(broker, connects, method, path):
    source = source_for(broker)
    unavailable(lambda: source._get(path), "forbidden_route")
    assert connects == [] and broker.requests == []


def test_forbidden_routes_are_refused_even_when_the_broker_is_down(connects):
    # Were a connection attempted this would be broker_unreachable instead.
    source = BrokerCredentialSource(
        "http://127.0.0.1:%d" % _closed_port(), identity_key=IDENTITY_A, bearer=BEARER
    )
    unavailable(lambda: source._send("POST", "/v1/credential/1/refresh"), "forbidden_route")
    assert connects == []


@pytest.mark.parametrize("kwargs", [{"wait_ms": 5}, {"if_generation": 1}, {"wait_ms": 0}])
def test_a_wait_or_generation_is_allowed_only_on_the_snapshot(broker, connects, kwargs):
    source = source_for(broker)
    unavailable(lambda: source._send("GET", "/v1/healthz", **kwargs), "forbidden_route")
    assert connects == [] and broker.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wait_ms": -1},
        {"wait_ms": 30_001},
        {"wait_ms": True},
        {"wait_ms": 1.5},
        {"if_generation": -1},
    ],
)
def test_nonsense_wait_and_generation_values_never_reach_the_wire(broker, connects, kwargs):
    source = source_for(broker)
    with pytest.raises(ValueError):
        source._send("GET", "/v1/snapshot", **kwargs)
    assert connects == [] and broker.requests == []


def test_the_two_allowed_routes_work_through_the_choke_point(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    source = source_for(broker)
    assert source._get("/v1/healthz").status == 200
    assert source._get("/v1/snapshot").status == 200
    assert broker.lease_requests() == [("GET", "/v1/healthz"), ("GET", "/v1/snapshot")]


def test_the_environment_proxy_is_ignored(broker, monkeypatch):
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:%d" % _closed_port())
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    assert lease_of(source_for(broker)).access_token == ACCESS_A
    assert broker.lease_requests() == [("GET", "/v1/snapshot")]


# -- healthz ---------------------------------------------------------------


def test_healthz_reports_the_version_and_sends_no_bearer(broker):
    assert source_for(broker).healthz() == "stand-in-1"
    assert broker.lease_requests() == [("GET", "/v1/healthz")]
    assert "authorization" not in broker.requests[0].headers


@pytest.mark.parametrize("body", [{"ok": False}, {"version": "x"}, [], "ok"])
def test_healthz_that_is_not_ok_is_a_broker_error(broker, body):
    broker.healthz_body = body
    unavailable(source_for(broker).healthz, "broker_error")


def test_healthz_without_a_version_returns_none(broker):
    broker.healthz_body = {"ok": True}
    assert source_for(broker).healthz() is None


# -- the bearer ------------------------------------------------------------


def test_a_bearer_file_is_read_afresh_for_every_request(broker, tmp_path):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    token_file = tmp_path / "auth-broker.token"
    token_file.write_text(WRONG_BEARER + "\n")
    source = BrokerCredentialSource(broker.url, identity_key=IDENTITY_A, bearer_file=token_file)

    unavailable(lambda: lease_of(source), "broker_error")
    token_file.write_text(BEARER + "\n")  # a rotated token file, no restart
    assert lease_of(source).access_token == ACCESS_A
    assert broker.requests[-1].headers["authorization"] == "Bearer " + BEARER
    token_file.write_text(WRONG_BEARER)
    unavailable(lambda: lease_of(source), "broker_error")
    assert len(broker.requests) == 3


def test_a_bearer_file_accepts_a_path_string_and_never_shows_its_content(broker, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(BEARER)
    source = BrokerCredentialSource(
        broker.url, identity_key=IDENTITY_A, bearer_file=str(token_file)
    )
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    lease_of(source)
    assert_no_secrets(source)
    assert "bearer_file=" in repr(source)


@pytest.mark.parametrize("content", [None, "", "  \n", "two words", "tab\ttoken", "caf\u00e9"])
def test_an_unusable_bearer_file_fails_before_any_request(broker, tmp_path, connects, content):
    token_file = tmp_path / "token"
    if content is not None:
        token_file.write_text(content, encoding="utf-8")
    source = BrokerCredentialSource(broker.url, identity_key=IDENTITY_A, bearer_file=token_file)
    unavailable(lambda: lease_of(source), "broker_error")
    assert connects == [] and broker.requests == []


def test_a_bearer_file_that_is_not_utf8_fails_before_any_request(broker, tmp_path, connects):
    token_file = tmp_path / "token"
    token_file.write_bytes(b"\xff\xfe\xfd")
    source = BrokerCredentialSource(broker.url, identity_key=IDENTITY_A, bearer_file=token_file)
    unavailable(lambda: lease_of(source), "broker_error")
    assert connects == []


def test_exactly_one_bearer_source_is_required(tmp_path):
    with pytest.raises(ValueError):
        BrokerCredentialSource("http://127.0.0.1:1", identity_key=IDENTITY_A)
    with pytest.raises(ValueError):
        BrokerCredentialSource(
            "http://127.0.0.1:1", identity_key=IDENTITY_A, bearer=BEARER, bearer_file=tmp_path / "t"
        )


@pytest.mark.parametrize(
    "bearer", ["", "has space-SECRET", "line\nbreak-SECRET", "caf\u00e9-SECRET", "tab\t-SECRET"]
)
def test_an_unusable_inline_bearer_is_refused_without_echoing_it(bearer):
    with pytest.raises(ValueError) as info:
        BrokerCredentialSource("http://127.0.0.1:1", identity_key=IDENTITY_A, bearer=bearer)
    assert "SECRET" not in str(info.value)


# -- constructor and argument validation -----------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://127.0.0.1:8765",
        "127.0.0.1:8765",
        "http://",
        "http://127.0.0.1:8765/v1",
        "http://127.0.0.1:8765/?x=1",
        "http://127.0.0.1:8765/#frag",
        "http://user:PASSWORD-SECRET@127.0.0.1:8765",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:port",
        "",
    ],
)
def test_a_base_url_must_be_a_bare_http_origin(base_url):
    with pytest.raises(ValueError) as info:
        BrokerCredentialSource(base_url, identity_key=IDENTITY_A, bearer=BEARER)
    assert "SECRET" not in str(info.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"identity_key": ""},
        {"provider": ""},
        {"request_timeout": 0},
        {"request_timeout": float("nan")},
    ],
)
def test_constructor_arguments_are_validated(overrides):
    options = {"identity_key": IDENTITY_A, "bearer": BEARER, **overrides}
    with pytest.raises(ValueError):
        BrokerCredentialSource("http://127.0.0.1:8765", **options)


def test_lease_arguments_are_validated(broker):
    source = source_for(broker)
    for kwargs in (
        {"min_remaining_ms": -1, "wait_s": 0},
        {"min_remaining_ms": 0, "wait_s": -1},
        {"min_remaining_ms": 0, "wait_s": float("nan")},
        {"min_remaining_ms": 0, "wait_s": float("inf")},
    ):
        with pytest.raises(ValueError):
            source.lease(**kwargs)
    assert broker.requests == []


def test_origins_are_accepted_in_their_usual_forms():
    for url, shown in (
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
        ("http://[::1]:8765", "http://[::1]:8765"),
        ("https://broker.example", "https://broker.example:443"),
        ("http://broker.example", "http://broker.example:80"),
    ):
        source = BrokerCredentialSource(url, identity_key=IDENTITY_A, bearer=BEARER)
        assert shown in repr(source)


# -- secrets ---------------------------------------------------------------


def test_the_source_and_lease_reprs_hide_every_secret(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    source = source_for(broker)
    lease = lease_of(source)
    assert_no_secrets(source, lease)
    assert "<redacted>" in repr(source)
    assert lease.token_digest in repr(lease)
    assert "access_token" not in repr(lease)
    assert f"{lease}" == repr(lease) and f"{source}" == repr(source)
    assert lease.access_token == ACCESS_A  # the field is still there to be used


def test_every_failure_mode_keeps_secrets_out_of_the_exception(broker):
    """One sweep over the failure modes, each carrying secrets in the broker's reply."""
    echo = json.dumps({"bearer": BEARER, "access": ACCESS_A, "token": WRONG_BEARER}).encode()
    for status in (400, 401, 403, 404, 429, 500, 502, 503):
        broker.on_snapshot = lambda request, status=status: (status, echo, {})
        unavailable(lambda: lease_of(source_for(broker)), "broker_error")
    broker.on_snapshot = lambda request: (200, echo, {})
    unavailable(lambda: lease_of(source_for(broker)), "broker_error")
    broker.on_snapshot = None
    token = make_jwt("acct-other")
    broker.credentials = [
        broker.oauth(1, IDENTITY_A, token, "acct-a", HOUR_MS),
        broker.oauth(2, IDENTITY_B, ACCESS_B, "acct-b", 1_000),
    ]
    unavailable(lambda: lease_of(source_for(broker)), "account_mismatch")
    unavailable(lambda: lease_of(source_for(broker, IDENTITY_B)), "expiring")
    broker.credentials.append(broker.oauth(3, IDENTITY_B, ACCESS_A, "acct-a", HOUR_MS))
    unavailable(lambda: lease_of(source_for(broker, IDENTITY_B)), "ambiguous")


def test_credential_unavailable_carries_reason_and_detail():
    error = CredentialUnavailable("expiring", "12 ms left")
    assert (error.reason, error.detail) == ("expiring", "12 ms left")
    assert str(error) == "expiring: 12 ms left"
    assert str(CredentialUnavailable("no_match")) == "no_match"
    with pytest.raises(ValueError):
        CredentialUnavailable("not_billed", "x")


def test_the_source_satisfies_the_credential_source_protocol(broker):
    assert isinstance(source_for(broker), CredentialSource)


# -- time budgets ----------------------------------------------------------
#
# Every bound below has a wide margin over the 0.25 s slack so a loaded machine
# does not flake; the misbehaving broker would otherwise hold the call for 8 s
# or more, so the margins still separate a deadline from a per-read timeout.

MISBEHAVIOURS = ["stall_headers", "stall_body", "drip_headers", "drip_body"]


def _misbehave(broker: StandInBroker, how: str, *, conditional: bool) -> None:
    """Make the snapshot reply stall or drip, for plain or conditional requests only."""

    def drip(handler, chunk: bytes) -> None:
        for _ in range(200):  # at most ten seconds, then the handler gives up
            handler.wfile.write(chunk)
            if broker._release.wait(0.05):
                return

    def raw(handler, request) -> bool:
        if request.path != "/v1/snapshot" or conditional != ("if-none-match" in request.headers):
            return False
        if how == "stall_headers":
            broker._release.wait(10)
        elif how == "late_stall_body":
            broker._release.wait(1.0)
            handler.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n{")
            broker._release.wait(10)
        elif how == "stall_body":
            handler.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n{")
            broker._release.wait(10)
        elif how == "drip_headers":
            handler.wfile.write(b"HTTP/1.0 200 OK\r\n")
            drip(handler, b"X-Pad: a\r\n")
        else:
            handler.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n")
            drip(handler, b" ")
        return True

    broker.on_raw = raw


@pytest.mark.parametrize("wait_s", [0, 30])
@pytest.mark.parametrize("how", MISBEHAVIOURS)
def test_the_first_fetch_is_bounded_in_total_by_request_timeout(broker, how, wait_s):
    # Neither a stall nor a byte-at-a-time drip may outlast request_timeout, and
    # a large wait_s does not extend it: wait_s is for long polls.
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    _misbehave(broker, how, conditional=False)
    source = source_for(broker, request_timeout=0.4)
    started = time.monotonic()
    unavailable(lambda: lease_of(source, wait_s=wait_s), "broker_unreachable")
    assert 0.3 <= time.monotonic() - started < 2.0
    assert len(broker.requests) == 1  # no retry


@pytest.mark.parametrize("how", MISBEHAVIOURS)
def test_a_long_poll_ends_at_the_lease_deadline_not_at_request_timeout(broker, how):
    # request_timeout is 8 s, so only the lease deadline (0.3 s plus slack) can
    # end this in time. A per-read socket timeout of request_timeout + wait would not.
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_OLD, "acct-a", 20_000)]
    _misbehave(broker, how, conditional=True)
    source = source_for(broker, request_timeout=8.0)
    started = time.monotonic()
    unavailable(lambda: lease_of(source, min_remaining_ms=60_000, wait_s=0.3), "broker_unreachable")
    assert 0.25 <= time.monotonic() - started < 2.0
    assert len(broker.requests) == 2  # the first fetch and one long poll; no retry


def test_a_stall_after_slow_headers_still_ends_at_the_deadline(broker):
    # The headers arrive 1.0 s into a 1.5 s budget and the body then stalls. A
    # per-read socket timeout restarts at the last byte and would allow 1.5 s more.
    _misbehave(broker, "late_stall_body", conditional=False)
    source = source_for(broker, request_timeout=1.5)
    started = time.monotonic()
    unavailable(lambda: lease_of(source), "broker_unreachable")
    assert 1.3 <= time.monotonic() - started < 2.2


def test_a_slow_first_fetch_still_completes_when_wait_s_is_zero(broker):
    # Enough margin on the token means no waiting is wanted, but the fetch itself
    # is allowed request_timeout rather than the (zero) wait budget.
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    broker.on_snapshot = lambda request: time.sleep(0.3)  # then answers normally
    assert lease_of(source_for(broker, request_timeout=3.0), wait_s=0).access_token == ACCESS_A


def test_a_request_whose_deadline_has_passed_never_connects(broker, connects):
    source = source_for(broker)
    unavailable(
        lambda: source._send("GET", "/v1/snapshot", deadline=time.monotonic() - 1),
        "broker_unreachable",
    )
    assert connects == [] and broker.requests == []


def test_watchdogs_do_not_outlive_their_requests(broker):
    broker.credentials = [broker.oauth(1, IDENTITY_A, ACCESS_A, "acct-a", HOUR_MS)]
    source = source_for(broker)
    for _ in range(5):
        lease_of(source)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if isinstance(t, threading.Timer)]:
            break
        time.sleep(0.02)
    assert [t for t in threading.enumerate() if isinstance(t, threading.Timer)] == []
