"""``send``: one attempt to call the Codex Responses endpoint, journalled.

One call to ``send`` is one attempt and makes at most one HTTP POST. There is
no loop, no retry, no waiting on ``Retry-After`` and no second request after a
rejection. A caller that wants another try calls ``send`` again, passing the
earlier ``attempt_id`` as ``retry_of`` so the journal links the two. That
keeps the decision to spend a second request, which can be a second charge,
with the caller.

The flow is: lease a credential, run the replay gate, write ``started``,
connect, write ``sent`` and the request, read the reply, classify it and
write ``finished``. The journal orders those steps so that after any crash
``started`` and ``sent`` say how far the attempt got (see ``journal``).

Outcomes, as ``AttemptResult.outcome``:

``not_sent``
    Nothing was written to a socket: no credential could be leased, the
    credential cannot be carried in a header, or the connection failed (DNS,
    TCP or TLS). ``reason`` and ``detail`` say which.
``http_rejected``
    The server answered with a non-2xx status. A redirect is not followed and
    counts here.
``sent_no_response``
    The request was sent and no status line came back (reset, timeout or
    close).
``stream_incomplete``
    A 2xx stream ended or broke without a terminal event, or ended with
    ``response.incomplete``.
``failed_event``
    The stream ended with ``error``, ``response.failed`` or
    ``response.cancelled``, or with a completion event whose response is not
    status ``completed`` or carries an error.
``completed``
    The stream ended with ``response.completed`` (or ``response.done``) and a
    response in status ``completed``. The text may still be empty; judging it
    is the caller's business.

Billing is ``usage_reported`` when the provider returned a non-empty usage
object in any event, as ``response.usage`` or an event-level ``usage`` (taken
verbatim, from the last event that carried one), and ``unattested``
otherwise, for every outcome. An HTTP rejection, a failed event, a cut stream
and a request that got no answer are all unattested unless they carry usage:
the client has no other evidence and does not supply any. A missing usage
object stays ``None`` and is never reported as zero.

Everything in an ``AttemptResult`` that came from the provider (text, usage
with its nested values, served model, terminal event, error text) has the
access token replaced by ``[redacted]`` before it is journalled or returned.
A stream that breaks a transport size limit (see ``transport``) ends as
``stream_incomplete`` with ``error_phase`` ``limit``.

``send`` returns an ``AttemptResult`` for every attempt that has an outcome,
``not_sent`` included, so a caller reads one field rather than choosing
between a return value and an exception. It raises ``ReplayRefused`` (the
refusal of an attempt, not an outcome of one) and ``ValueError`` or
``TypeError`` for a malformed request, URL or header, before any credential is
leased. A failure to write the journal propagates: nothing is sent that the
journal could not record first.

The replay gate refuses a retry whose ``retry_of`` attempt was rejected with
401 or 403 while the new lease holds the same token. Sending the same
rejected token again cannot change the answer and only risks a lockout, so
the refusal is written to the journal as ``replay_refused`` before anything
is started. A different token, or a previous outcome that was not an
authentication rejection, passes.
"""

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .credentials import CredentialUnavailable
from .journal import UNATTESTED, Billing
from .request import build_headers, validate_extra_headers
from .transport import NotSent, parse_url, post_sse

DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
RESPONSES_PATH = "/codex/responses"

_COMPLETED_TYPES = ("response.completed", "response.done")


class ReplayRefused(Exception):
    """A retry was refused because it would resend a token the server rejected."""

    def __init__(self, attempt_id, retry_of, status, token_digest):
        super().__init__(
            "attempt %s refused: %s was rejected with HTTP %s using this same token"
            % (attempt_id, retry_of, status)
        )
        self.attempt_id = attempt_id
        self.retry_of = retry_of
        self.status = status
        self.token_digest = token_digest


@dataclass(frozen=True)
class AttemptResult:
    """What one attempt did. Holds no credential and no request body.

    ``reason`` and ``detail`` are set for ``not_sent``; ``error`` and
    ``error_phase`` describe a failure after the request was sent. ``text`` is
    the response text for convenience; the raw stream at ``raw_path`` is the
    evidence. ``usage`` is ``billing.usage``.
    """

    attempt_id: str
    outcome: str
    billing: Billing
    status: int | None = None
    text: str = ""
    served_model: str | None = None
    terminal_event: str | None = None
    retry_of: str | None = None
    reason: str | None = None
    detail: str | None = None
    error: str | None = None
    error_phase: str | None = None
    journal_path: Path | None = None
    raw_path: Path | None = None
    chunks_path: Path | None = None

    @property
    def usage(self):
        return self.billing.usage


def _output_text(response):
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    parts = []
    for item in output:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(item, dict) and item.get("type") == "message" and isinstance(content, list):
            parts.extend(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            )
    return "".join(parts)


class _Digest:
    """The text, usage object and served model the events carry, retaining no event.

    Usage is read from ``response.usage`` or an event-level ``usage`` object,
    whichever an event carries; the last non-empty one in event order wins.
    """

    def __init__(self):
        self.deltas, self.completed, self.usage, self.model = [], "", None, None

    def add(self, event):
        payload = event.payload or {}
        response = payload.get("response")
        candidates = [payload.get("usage")]
        if isinstance(response, dict):
            candidates.append(response.get("usage"))
            if isinstance(response.get("model"), str):
                self.model = response["model"]
            self.completed = _output_text(response) or self.completed
        for usage in candidates:
            if isinstance(usage, dict) and usage:
                self.usage = usage
        if event.type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
            self.deltas.append(payload["delta"])

    def result(self):
        return self.completed or "".join(self.deltas), self.usage, self.model


def _clean_completion(event):
    response = (event.payload or {}).get("response")
    return (
        event.type in _COMPLETED_TYPES
        and isinstance(response, dict)
        and response.get("status") == "completed"
        and not response.get("error")
        and not response.get("incomplete_details")
    )


def _classify(result):
    if result.status is None:
        return "sent_no_response"
    if not 200 <= result.status < 300:
        return "http_rejected"
    if result.terminal is None or result.terminal.type == "response.incomplete":
        return "stream_incomplete"
    return "completed" if _clean_completion(result.terminal) else "failed_event"


def _scrub_deep(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, dict):
        return {_scrub_deep(k, secret): _scrub_deep(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_deep(v, secret) for v in value]
    return value


def _scrubber(secret):
    """A function that replaces ``secret`` in any provider-derived value, nested ones included."""
    return lambda value: _scrub_deep(value, secret) if secret else value


def _as_result(journal, record, retry_of):
    # Every provider-derived field of `record` was scrubbed before it was
    # written, so the result is built from the same values as the journal.
    raw = record["raw"]
    return AttemptResult(
        attempt_id=record["attempt_id"],
        outcome=record["outcome"],
        billing=Billing(**record["billing"]),
        status=record.get("status"),
        text=record.get("text") or "",
        served_model=record.get("served_model"),
        terminal_event=record.get("terminal_event"),
        retry_of=retry_of,
        reason=record.get("reason"),
        detail=record.get("detail"),
        error=record.get("error"),
        error_phase=record.get("error_phase"),
        journal_path=journal.path,
        raw_path=journal.raw_path(record["attempt_id"]) if raw else None,
        chunks_path=journal.chunks_path(record["attempt_id"]) if raw else None,
    )


def _not_sent(journal, attempt_id, retry_of, url, body, reason, detail, credential=None):
    journal.not_sent(
        attempt_id=attempt_id,
        retry_of=retry_of,
        url=url,
        body=body,
        reason=reason,
        detail=detail,
        credential=credential,
    )
    return AttemptResult(
        attempt_id=attempt_id,
        outcome="not_sent",
        billing=UNATTESTED,
        retry_of=retry_of,
        reason=reason,
        detail=detail,
        journal_path=journal.path,
    )


def _refuse_replay(journal, retry_of, attempt_id, lease, credential):
    if retry_of is None:
        return
    records = journal.history(retry_of)
    started = next((r for r in records if r["kind"] == "started"), None)
    finished = next((r for r in records if r["kind"] == "finished"), None)
    if (
        started is None
        or finished is None
        or finished["outcome"] != "http_rejected"
        or finished["status"] not in (401, 403)
        or started["credential"]["token_digest"] != lease.token_digest
    ):
        return
    journal.replay_refused(
        attempt_id=attempt_id,
        retry_of=retry_of,
        credential=credential,
        prior_status=finished["status"],
    )
    raise ReplayRefused(attempt_id, retry_of, finished["status"], lease.token_digest)


def _finish(log, digest, outcome, scrub, started):
    text, usage, model = digest.result()
    return log.finish(
        outcome=_classify(outcome),
        billing=Billing.from_usage(scrub(usage)),
        status=outcome.status,
        headers=outcome.headers,
        body=outcome.body,
        body_truncated=outcome.body_truncated,
        served_model=scrub(model),
        text=scrub(text),
        terminal_event=scrub(outcome.terminal.type) if outcome.terminal else None,
        error=scrub(outcome.error),
        error_phase=outcome.error_phase,
        elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
    )


def _check_attempt_id(attempt_id):
    try:
        valid = (
            isinstance(attempt_id, str)
            and uuid.UUID(attempt_id).version == 4
            and str(uuid.UUID(attempt_id)) == attempt_id
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("attempt_id must be a canonical lowercase uuid4 string")


def send(
    request,
    source,
    journal,
    *,
    identity,
    base_url=DEFAULT_BASE_URL,
    retry_of=None,
    min_remaining_ms=60_000,
    lease_wait_s=5.0,
    connect_timeout=15,
    read_timeout=280,
    extra_headers=None,
    session_id=None,
    thread_id=None,
    attempt_id=None,
):
    """Make one attempt to POST ``request`` to ``<base_url>/codex/responses``.

    ``source`` is a ``CredentialSource``; ``journal`` a ``Journal``;
    ``identity`` a ``ClientIdentity``. ``retry_of`` names an earlier attempt
    this one follows. ``session_id`` and ``thread_id`` default to a fresh
    uuid4 each; ``x-client-request-id`` is always this attempt's id.
    ``attempt_id`` is normally generated here. A caller that must put the id
    into other headers before sending (``thread-id`` equal to
    ``x-client-request-id``, say) supplies a canonical lowercase uuid4
    string. Either way the id is claimed in the journal (``Journal.claim``,
    an exclusive file create) before any lease or request, so two sends,
    threads or processes, cannot share one: the second raises ``ValueError``,
    as does a malformed id or one the journal already holds records for.
    ``read_timeout`` bounds each wait for response bytes, not the whole
    stream. Returns an ``AttemptResult``; see the module docstring.
    """
    if attempt_id is None:
        attempt_id = str(uuid.uuid4())
    else:
        _check_attempt_id(attempt_id)
    url = base_url.rstrip("/") + RESPONSES_PATH
    parse_url(url)
    extra = validate_extra_headers(extra_headers)
    body = request.body_bytes()
    try:
        journal.claim(attempt_id)
    except FileExistsError:
        raise ValueError("attempt_id is already in the journal") from None
    try:
        lease = source.lease(min_remaining_ms=min_remaining_ms, wait_s=lease_wait_s)
    except CredentialUnavailable as unavailable:
        return _not_sent(
            journal, attempt_id, retry_of, url, body, unavailable.reason, unavailable.detail
        )
    credential = {
        "credential_id": lease.credential_id,
        "identity_key": lease.identity_key,
        "token_digest": lease.token_digest,
        "generation": lease.generation,
        "account_id": lease.account_id,
    }
    token = lease.access_token
    _refuse_replay(journal, retry_of, attempt_id, lease, credential)
    try:
        headers = build_headers(
            access_token=token,
            account_id=lease.account_id,
            identity=identity,
            attempt_id=attempt_id,
            session_id=session_id or str(uuid.uuid4()),
            thread_id=thread_id or str(uuid.uuid4()),
            extra_headers=extra,
        )
    except ValueError:
        return _not_sent(
            journal,
            attempt_id,
            retry_of,
            url,
            body,
            "invalid_credential",
            "the access token or account id cannot be sent as an HTTP header",
            credential,
        )
    scrub = _scrubber(token)
    log = journal.start(
        attempt_id=attempt_id,
        retry_of=retry_of,
        url=url,
        body=body,
        headers=headers,
        credential=credential,
        secrets=(token,),
    )
    started = time.monotonic()
    digest = _Digest()
    try:
        outcome = post_sse(
            url,
            headers,
            body,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            on_sent=log.mark_sent,
            on_chunk=log.chunk,
            on_event=digest.add,
        )
        record = _finish(log, digest, outcome, scrub, started)
    except NotSent as failure:
        record = log.finish(
            outcome="not_sent",
            billing=UNATTESTED,
            reason="connect_failed",
            detail=scrub(failure.detail),
            elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
        )
    except BaseException:
        # Whatever stopped here, the journal keeps `started` (and `sent`, if
        # the request went out) with no `finished`: an honest unfinished attempt.
        log.abort()
        raise
    return _as_result(journal, record, retry_of)
