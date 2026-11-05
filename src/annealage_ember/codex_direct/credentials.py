"""Read-only leases of a ChatGPT Codex access token from omp's auth broker.

The direct Codex client needs one access token and the account id that goes
with it. This module obtains both by reading the broker's redacted snapshot
and by nothing else. It never refreshes a token, never sees a refresh token
(the broker replaces each one with a sentinel before the snapshot leaves it),
never writes to omp's credential vault, and never calls a broker route that
mutates state. ``BrokerCredentialSource`` enforces that itself rather than by
convention: the only code path that opens a socket accepts ``GET /v1/healthz``
and ``GET /v1/snapshot`` (the latter with a ``wait`` query and an
``If-None-Match`` header), and any other method or path raises
``CredentialUnavailable("forbidden_route")`` before a connection exists.

Token freshness belongs to omp. Its broker runs a refresher that sweeps on an
interval and rotates credentials whose expiry falls inside a skew window; the
60 second sweep and 5 minute skew are omp's defaults. Those figures describe
omp's behaviour and are not a contract with the provider, so nothing here
treats them as a guarantee that a token is accepted or refused. Expiry is used
for one decision only: whether the broker's own clock says the token has at
least the margin the caller asked for. The remaining time is measured against
the broker's ``serverNowMs`` plus the monotonic time elapsed since the snapshot
arrived, never against this machine's wall clock, which may disagree with the
broker's by more than a token lifetime. The anchor is the moment the reply was
received, while the broker stamped ``serverNowMs`` earlier, so the remaining
time is optimistic by the response latency (negligible on loopback, not zero).

A token that is short on time is answered by waiting and never by refreshing.
``lease`` uses the broker's conditional long poll: ``GET /v1/snapshot?wait=<ms>``
with the last generation in ``If-None-Match``. The broker holds the request
until the generation changes (``200`` with the new snapshot), until the wait
lapses unchanged (an empty ``304``), or until the caller disconnects (an empty
``499``, which this client never causes and treats as an error if it sees one).
The wait is capped at the broker's own 30 second clamp per request and at the
caller's ``wait_s`` overall. When the budget runs out the call raises
``expiring`` (the token is still valid but inside the margin) or ``expired``.

Every broker exchange runs under an absolute deadline covering connect, headers
and the whole body, enforced by a watchdog that shuts the socket, because a
per-read socket timeout is never tripped by a broker that drips bytes. The
first snapshot fetch gets ``request_timeout``; each long poll gets whatever is
left of ``wait_s`` plus a quarter second of slack. ``lease`` therefore returns
or raises within ``max(request_timeout, wait_s + 0.25)`` seconds, DNS
resolution of a hostname aside.

A credential is selected by exact equality with the configured broker
``identityKey``. No match and more than one match both fail closed: the first
of several is never chosen, because picking between two accounts would send a
request on an account the caller did not name. When the access token is a JWT,
its ``chatgpt_account_id`` claim must equal the snapshot's ``accountId``; the
payload is decoded without verifying the signature, which is a consistency
check against a mislabelled row and not authentication. Tokens that are not
JWTs are accepted unchecked.

Secrets stay out of every observable surface. The access token is a
``repr=False`` field, the bearer is never stored in a repr, exception details
are built from fixed text, integers and exception class names only (never from
a response body or a header), and exceptions are raised ``from None`` so a
chained cause cannot carry text through a traceback. Only the SHA-256 digest of
the access token is exposed for the journal. Nothing here logs.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable
from urllib.parse import urlsplit

__all__ = [
    "BrokerCredentialSource",
    "CredentialLease",
    "CredentialSource",
    "CredentialUnavailable",
]

_HEALTHZ = "/v1/healthz"
_SNAPSHOT = "/v1/snapshot"
# The complete set of broker routes this client may contact. Anything else is
# refused before a socket is opened; extending this set is the one edit that
# widens what the client can do to the broker.
_ALLOWED_ROUTES = frozenset({("GET", _HEALTHZ), ("GET", _SNAPSHOT)})
# Healthz is unauthenticated in the broker, so the bearer is not sent to it.
_BEARER_ROUTES = frozenset({_SNAPSHOT})

# The broker clamps `wait` to this many milliseconds.
_MAX_WAIT_MS = 30_000
# A long poll may finish this long after the caller's wait budget, so that the
# broker's own 304 or 200 at the end of its wait has time to arrive. It is the
# only slack beyond wait_s that lease() allows itself.
_POLL_SLACK_S = 0.25
_OUT_OF_TIME = "the broker did not reply within the time budget"
# A snapshot carries one redacted row per stored credential; this bounds a
# misbehaving or hostile listener, not a legitimate one.
_MAX_BODY_BYTES = 8 * 1024 * 1024

_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"

_REASONS = frozenset(
    {
        "no_match",
        "ambiguous",
        "expiring",
        "expired",
        "account_mismatch",
        "broker_unreachable",
        "broker_error",
        "forbidden_route",
    }
)


@dataclass(frozen=True)
class CredentialLease:
    """One access token with the broker facts needed to judge and journal it."""

    access_token: str = field(repr=False)
    account_id: str  # sent as chatgpt-account-id
    credential_id: int  # broker-local row id; journal only, never used to call a route
    identity_key: str  # e.g. "email:a@example.com"
    expires_at_ms: int  # broker credential.expires, epoch ms on the broker's clock
    generation: int  # broker snapshot generation
    token_digest: str  # sha256 hex of access_token
    obtained_monotonic: float  # time.monotonic() when the snapshot was received
    server_now_ms: int  # broker serverNowMs in that snapshot

    def remaining_ms(self, now_monotonic: float | None = None) -> int:
        """Milliseconds left on the broker's clock, rounded down.

        The broker's time now is its ``serverNowMs`` plus the monotonic time
        elapsed since the snapshot arrived. The local wall clock plays no part.
        A negative result means the token has expired.
        """
        now = time.monotonic() if now_monotonic is None else now_monotonic
        elapsed_ms = max(0, math.ceil((now - self.obtained_monotonic) * 1000))
        return self.expires_at_ms - (self.server_now_ms + elapsed_ms)


class CredentialUnavailable(Exception):
    """No usable credential could be leased.

    ``reason`` is one of ``no_match``, ``ambiguous``, ``expiring``, ``expired``,
    ``account_mismatch``, ``broker_unreachable``, ``broker_error`` or
    ``forbidden_route``. ``detail`` is scrubbed text and never holds a token.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in _REASONS:
            raise ValueError("unknown CredentialUnavailable reason")
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:
        return "%s: %s" % (self.reason, self.detail) if self.detail else self.reason


@runtime_checkable
class CredentialSource(Protocol):
    """Anything that can lease one credential for a send."""

    def lease(self, *, min_remaining_ms: int, wait_s: float) -> CredentialLease:
        """Return a lease with at least ``min_remaining_ms`` left, waiting at most
        ``wait_s`` seconds for one; raise ``CredentialUnavailable`` otherwise."""
        ...


class _Reply(NamedTuple):
    status: int
    body: object


class _Snapshot(NamedTuple):
    generation: int
    server_now_ms: int
    received_monotonic: float
    entries: list[object]


def _ms(value: object) -> int | None:
    """An integral millisecond count from a JSON number, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return math.floor(value)
    return None


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _bearer_ok(value: str) -> bool:
    # A header value, so no whitespace or control characters: that also rules
    # out header injection from a hand-edited token file.
    return (
        bool(value)
        and value.isascii()
        and value.isprintable()
        and not any(ch.isspace() for ch in value)
    )


def _jwt_account_id(token: str) -> str | None:
    """The ``chatgpt_account_id`` claim of a JWT access token, if it has one.

    Only the payload is decoded; the signature is not checked. A token that is
    not a JWT, or carries no such claim, yields None.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError):
        return None
    if not isinstance(claims, dict):
        return None
    auth = claims.get(_OPENAI_AUTH_CLAIM)
    if not isinstance(auth, dict):
        return None
    return _nonempty_str(auth.get("chatgpt_account_id"))


class BrokerCredentialSource:
    """A ``CredentialSource`` that reads omp's auth broker and only reads it.

    ``base_url`` is the broker's origin (``http://127.0.0.1:8765`` by default
    in omp). Exactly one of ``bearer`` and ``bearer_file`` supplies the
    broker's bearer token; a file is read afresh for every authenticated
    request, so a rotated token file is picked up without restarting.
    ``request_timeout`` is the whole-exchange limit, in seconds, for a request
    that carries no lease deadline (healthz) and for the first snapshot fetch
    of ``lease``; long polls are limited by ``wait_s`` instead.
    """

    def __init__(
        self,
        base_url: str,
        *,
        identity_key: str,
        provider: str = "openai-codex",
        bearer: str | None = None,
        bearer_file: str | Path | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        if (bearer is None) == (bearer_file is None):
            raise ValueError("exactly one of bearer and bearer_file is required")
        if bearer is not None and not _bearer_ok(bearer):
            raise ValueError("bearer must be a non-empty printable ASCII token without whitespace")
        if not identity_key:
            raise ValueError("identity_key is required")
        if not provider:
            raise ValueError("provider is required")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be a positive number of seconds")

        parts = urlsplit(base_url)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("base_url has an invalid port") from None
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("base_url must be an http(s) origin with no path, query or userinfo")

        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = port if port is not None else (443 if self._https else 80)
        self._identity_key = identity_key
        self._provider = provider
        self._bearer = bearer if bearer is not None else ""
        self._bearer_file = Path(bearer_file) if bearer_file is not None else None
        self._timeout = float(request_timeout)

    def __repr__(self) -> str:
        credential = (
            "bearer_file=%r" % str(self._bearer_file) if self._bearer_file else "bearer=<redacted>"
        )
        host = "[%s]" % self._host if ":" in self._host else self._host
        return "BrokerCredentialSource(origin=%r, identity_key=%r, provider=%r, %s)" % (
            "%s://%s:%d" % ("https" if self._https else "http", host, self._port),
            self._identity_key,
            self._provider,
            credential,
        )

    # -- public API ------------------------------------------------------

    def healthz(self) -> str | None:
        """Probe ``GET /v1/healthz`` and return the broker's version, if it reports one.

        The route is unauthenticated, so no bearer is sent. Raises
        ``broker_unreachable`` or ``broker_error`` when the broker is not
        answering as a healthy broker.
        """
        body = self._get(_HEALTHZ).body
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise CredentialUnavailable("broker_error", "healthz did not report ok")
        return _nonempty_str(body.get("version"))

    def lease(self, *, min_remaining_ms: int, wait_s: float) -> CredentialLease:
        """Lease the configured account's access token.

        Returns a lease with at least ``min_remaining_ms`` (and always more
        than zero) left on the broker's clock. When the token is short, waits
        up to ``wait_s`` seconds in total for the broker to publish a newer
        generation, which is how a refresh by omp reaches this client; the
        wait is the only response to a short token. Raises
        ``CredentialUnavailable`` otherwise.

        Time is bounded by absolute deadlines handed to the socket layer. The
        first snapshot fetch may take up to ``request_timeout`` whatever
        ``wait_s`` is, so a token with enough margin is leased even at
        ``wait_s=0``. Each long poll must finish by ``wait_s`` after the call
        began plus ``_POLL_SLACK_S``, which lets the broker's own answer at the
        end of its wait arrive. The call therefore returns or raises within
        ``max(request_timeout, wait_s + _POLL_SLACK_S)`` of its start. A broker
        that has not replied by its deadline is ``broker_unreachable``.
        """
        if min_remaining_ms < 0:
            raise ValueError("min_remaining_ms must not be negative")
        if not math.isfinite(wait_s) or wait_s < 0:
            raise ValueError("wait_s must be a finite, non-negative number of seconds")

        started = time.monotonic()
        wait_deadline = started + wait_s
        snapshot = self._fetch_snapshot(started + self._timeout)
        while True:
            lease = self._select(snapshot)
            remaining = lease.remaining_ms()
            if remaining >= max(min_remaining_ms, 1):
                return lease
            left_s = wait_deadline - time.monotonic()
            if left_s <= 0:
                if remaining <= 0:
                    raise CredentialUnavailable(
                        "expired", "the access token had expired on the broker clock"
                    )
                raise CredentialUnavailable(
                    "expiring",
                    "the access token has %d ms left on the broker clock, "
                    "below the %d ms required" % (remaining, min_remaining_ms),
                )
            wait_ms = max(1, min(_MAX_WAIT_MS, math.ceil(left_s * 1000)))
            newer = self._await_newer_snapshot(
                snapshot.generation, wait_ms, wait_deadline + _POLL_SLACK_S
            )
            if newer is not None:
                snapshot = newer

    # -- selection -------------------------------------------------------

    def _matches(self, entry: object) -> bool:
        if not isinstance(entry, dict) or entry.get("provider") != self._provider:
            return False
        credential = entry.get("credential")
        return (
            isinstance(credential, dict)
            and credential.get("type") == "oauth"
            and _nonempty_str(credential.get("access")) is not None
            and _nonempty_str(credential.get("accountId")) is not None
            and entry.get("identityKey") == self._identity_key
        )

    def _select(self, snapshot: _Snapshot) -> CredentialLease:
        matches = [entry for entry in snapshot.entries if self._matches(entry)]
        if not matches:
            raise CredentialUnavailable(
                "no_match",
                "no active %s OAuth credential has the configured identity" % self._provider,
            )
        if len(matches) > 1:
            raise CredentialUnavailable(
                "ambiguous",
                "%d %s OAuth credentials share the configured identity"
                % (len(matches), self._provider),
            )
        entry = matches[0]
        credential = entry["credential"]
        access = credential["access"]
        account_id = credential["accountId"]

        claimed = _jwt_account_id(access)
        if claimed is not None and claimed != account_id:
            raise CredentialUnavailable(
                "account_mismatch",
                "the access token's account claim differs from the broker's accountId",
            )
        expires = _ms(credential.get("expires"))
        credential_id = entry.get("id")
        if expires is None or isinstance(credential_id, bool) or not isinstance(credential_id, int):
            raise CredentialUnavailable("broker_error", "the matching snapshot row is malformed")
        return CredentialLease(
            access_token=access,
            account_id=account_id,
            credential_id=credential_id,
            identity_key=self._identity_key,
            expires_at_ms=expires,
            generation=snapshot.generation,
            token_digest=hashlib.sha256(access.encode("utf-8")).hexdigest(),
            obtained_monotonic=snapshot.received_monotonic,
            server_now_ms=snapshot.server_now_ms,
        )

    # -- broker access ---------------------------------------------------

    def _fetch_snapshot(self, deadline: float) -> _Snapshot:
        """Fetch the current snapshot unconditionally."""
        reply = self._get(_SNAPSHOT, deadline=deadline)
        return self._parse_snapshot(reply.body, time.monotonic())

    def _await_newer_snapshot(
        self, generation: int, wait_ms: int, deadline: float
    ) -> _Snapshot | None:
        """Long-poll for a generation after ``generation``; None when the wait lapsed (304)."""
        reply = self._get(_SNAPSHOT, wait_ms=wait_ms, if_generation=generation, deadline=deadline)
        received = time.monotonic()
        if reply.status == 304:
            return None
        snapshot = self._parse_snapshot(reply.body, received)
        if snapshot.generation == generation:
            raise CredentialUnavailable(
                "broker_error", "the broker answered a long poll with 200 and no new generation"
            )
        return snapshot

    @staticmethod
    def _parse_snapshot(body: object, received_monotonic: float) -> _Snapshot:
        if not isinstance(body, dict):
            raise CredentialUnavailable("broker_error", "the snapshot is not a JSON object")
        generation = body.get("generation")
        server_now_ms = _ms(body.get("serverNowMs"))
        entries = body.get("credentials")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or server_now_ms is None
            or not isinstance(entries, list)
        ):
            raise CredentialUnavailable("broker_error", "the snapshot is missing required fields")
        return _Snapshot(generation, server_now_ms, received_monotonic, entries)

    def _get(
        self,
        path: str,
        *,
        wait_ms: int | None = None,
        if_generation: int | None = None,
        deadline: float | None = None,
    ) -> _Reply:
        return self._send(
            "GET", path, wait_ms=wait_ms, if_generation=if_generation, deadline=deadline
        )

    def _read_bearer(self) -> str:
        if self._bearer_file is None:
            return self._bearer
        try:
            value = self._bearer_file.read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:
            raise CredentialUnavailable(
                "broker_error", "the bearer file could not be read (%s)" % type(exc).__name__
            ) from None
        if not _bearer_ok(value):
            raise CredentialUnavailable("broker_error", "the bearer file is empty or malformed")
        return value

    def _send(
        self,
        method: str,
        path: str,
        *,
        wait_ms: int | None = None,
        if_generation: int | None = None,
        deadline: float | None = None,
    ) -> _Reply:
        """The only code that opens a socket: one allowlisted request, one reply.

        The allowlist is checked before anything else, so a refused route costs
        no connection. No redirect is followed, no proxy from the environment
        is used (``http.client`` consults none), and there is no retry: a
        failure is raised to the caller, who decides whether to ask again.

        ``deadline`` is an absolute ``time.monotonic()`` instant (default:
        ``request_timeout`` from now) by which the whole exchange, connect to
        last body byte, must be over; passing it is how the lease budget
        reaches the socket. A DNS lookup for a hostname cannot be interrupted,
        so the bound assumes the name resolves promptly (an address always does).
        """
        if (method, path) not in _ALLOWED_ROUTES:
            raise CredentialUnavailable(
                "forbidden_route", "only GET /v1/healthz and GET /v1/snapshot are permitted"
            )
        if (wait_ms is not None or if_generation is not None) and path != _SNAPSHOT:
            raise CredentialUnavailable(
                "forbidden_route", "only the snapshot route takes a wait or a generation"
            )
        if wait_ms is not None and (
            isinstance(wait_ms, bool)
            or not isinstance(wait_ms, int)
            or not 0 <= wait_ms <= _MAX_WAIT_MS
        ):
            raise ValueError("wait_ms must be an integer between 0 and %d" % _MAX_WAIT_MS)
        if if_generation is not None and (
            isinstance(if_generation, bool)
            or not isinstance(if_generation, int)
            or if_generation < 0
        ):
            raise ValueError("if_generation must be a non-negative integer")

        bearer = self._read_bearer() if path in _BEARER_ROUTES else None
        target = path if wait_ms is None else "%s?wait=%d" % (path, wait_ms)
        if deadline is None:
            deadline = time.monotonic() + self._timeout
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise CredentialUnavailable("broker_unreachable", _OUT_OF_TIME)
        conn: http.client.HTTPConnection
        if self._https:
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=budget, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=budget)

        # Socket timeouts are per read, so they cannot bound an exchange: a
        # broker that drips a byte at a time never trips one, and one that stalls
        # late gets a fresh timeout from its last byte. At the deadline a
        # watchdog therefore shuts the socket, which ends a blocked or dripping
        # read wherever the exchange has got to (the body loop below also
        # checks the clock between reads). The socket is held here because
        # http.client drops conn.sock once the reply starts.
        out_of_time = threading.Event()
        live: list[socket.socket] = []

        def cut() -> None:
            out_of_time.set()
            if live:
                try:
                    live[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # already closed: the exchange is over either way

        watchdog = threading.Timer(budget, cut)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                conn.connect()
                if conn.sock is not None:
                    live.append(conn.sock)
                if time.monotonic() >= deadline:
                    out_of_time.set()  # the watchdog may have fired before a socket existed
                    raise CredentialUnavailable("broker_unreachable", _OUT_OF_TIME)
                conn.putrequest(method, target, skip_accept_encoding=True)
                conn.putheader("Accept", "application/json")
                conn.putheader("Accept-Encoding", "identity")
                conn.putheader("Connection", "close")
                if bearer is not None:
                    conn.putheader("Authorization", "Bearer " + bearer)
                if if_generation is not None:
                    conn.putheader("If-None-Match", '"%d"' % if_generation)
                conn.endheaders()
                response = conn.getresponse()
                status = response.status
                encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
                chunks: list[bytes] = []
                size = 0
                while size <= _MAX_BODY_BYTES:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    # A broker that never stops sending still meets the deadline.
                    if time.monotonic() >= deadline:
                        out_of_time.set()
                        break
                if out_of_time.is_set():
                    raise CredentialUnavailable("broker_unreachable", _OUT_OF_TIME)
                body = b"".join(chunks)
            except OSError as exc:
                if out_of_time.is_set():
                    raise CredentialUnavailable("broker_unreachable", _OUT_OF_TIME) from None
                raise CredentialUnavailable(
                    "broker_unreachable", "the broker request failed (%s)" % type(exc).__name__
                ) from None
            except (http.client.HTTPException, ValueError) as exc:
                if out_of_time.is_set():
                    raise CredentialUnavailable("broker_unreachable", _OUT_OF_TIME) from None
                raise CredentialUnavailable(
                    "broker_error", "the broker reply was malformed (%s)" % type(exc).__name__
                ) from None
        finally:
            watchdog.cancel()
            conn.close()

        if 300 <= status < 400 and status != 304:
            raise CredentialUnavailable(
                "broker_error", "the broker redirected (status %d)" % status
            )
        if status == 304:
            if if_generation is None:
                raise CredentialUnavailable(
                    "broker_error", "the broker answered 304 to an unconditional request"
                )
            return _Reply(304, None)
        if status in (401, 403):
            raise CredentialUnavailable(
                "broker_error", "the broker refused the bearer (status %d)" % status
            )
        if status != 200:
            raise CredentialUnavailable("broker_error", "the broker answered status %d" % status)
        if encoding not in ("", "identity"):
            raise CredentialUnavailable("broker_error", "the broker reply was content-encoded")
        if len(body) > _MAX_BODY_BYTES:
            raise CredentialUnavailable("broker_error", "the broker reply was too large")
        try:
            return _Reply(200, json.loads(body))
        except (ValueError, RecursionError):
            raise CredentialUnavailable(
                "broker_error", "the broker reply was not valid JSON"
            ) from None
