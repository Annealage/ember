"""Tests for ``codex_direct.client.send`` against a loopback stand-in for the endpoint.

Each test states what the client sent, how many requests the stand-in received
and what the journal holds. "Exactly one request" is asserted for every way an
attempt can end, because a second request is a second charge.
"""

import base64
import dataclasses
import json
import pathlib
import re
import subprocess
import sys
import threading
import time
import uuid

import pytest
from codex_direct_fakes import (
    JPEG,
    PNG,
    TOKEN,
    USAGE,
    FakeCodex,
    FakeSource,
    RawServer,
    closed_port,
    completed_event,
    drain,
    event,
    make_lease,
    ok_stream,
    respond_json,
    respond_sse,
    unavailable,
)

import annealage_ember
from annealage_ember.codex_direct import (
    AttemptLog,
    AttemptResult,
    ClientIdentity,
    CodexRequest,
    Journal,
    ReplayRefused,
    image_item,
    send,
    text_item,
    transport,
)

IDENTITY = ClientIdentity("test-client", "9.9.9")
ITEMS = [
    image_item(PNG, detail="original"),
    text_item("context"),
    image_item(JPEG, detail="high"),
    text_item("question"),
]
REQUEST = CodexRequest(
    "gpt-x",
    "You read schematics.",
    ITEMS,
    effort="low",
    include=("reasoning.encrypted_content",),
    prompt_cache_key="cache-key-1",
)
NOT_BILLED = "not" + "_billed"  # spelled in two parts so this file does not contain the word


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal")


def run(url, journal, *, source=None, request=REQUEST, **kwargs):
    kwargs.setdefault("connect_timeout", 5)
    kwargs.setdefault("read_timeout", 5)
    return send(
        request,
        source or FakeSource(),
        journal,
        identity=IDENTITY,
        base_url=url,
        **kwargs,
    )


def kinds(journal, attempt_id=None):
    return [r["kind"] for r in journal.records() if attempt_id in (None, r["attempt_id"])]


def sse(*chunks):
    return lambda received, handler: respond_sse(handler, list(chunks))


def status(code, body=b'{"error":{"message":"nope"}}', headers=()):
    return lambda received, handler: respond_json(handler, code, body, headers)


def hang_up(received, handler):
    handler.close_connection = True  # read the request, answer nothing, close


def all_text(directory):
    return b"\n".join(p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file())


# One request, whatever the answer ---------------------------------------------------------

FAILED_WITH_USAGE = event(
    "response.failed",
    response={"id": "r", "status": "failed", "error": {"code": "x"}, "usage": USAGE},
)
CASES = {
    "ok": (sse(*ok_stream("hello world")), "completed", 200, USAGE),
    "http_401": (status(401), "http_rejected", 401, None),
    "http_403": (status(403), "http_rejected", 403, None),
    "http_429": (status(429, headers=[("Retry-After", "1")]), "http_rejected", 429, None),
    "http_500": (status(500), "http_rejected", 500, None),
    "http_503": (status(503, headers=[("Retry-After", "1")]), "http_rejected", 503, None),
    "sse_error_event": (
        sse(event("response.created"), event("error", error={"message": "boom"})),
        "failed_event",
        200,
        None,
    ),
    "response_failed": (
        sse(event("response.failed", response={"status": "failed", "error": {"code": "x"}})),
        "failed_event",
        200,
        None,
    ),
    "response_failed_with_usage": (sse(FAILED_WITH_USAGE), "failed_event", 200, USAGE),
    "response_cancelled": (
        sse(event("response.cancelled", response={"status": "cancelled"})),
        "failed_event",
        200,
        None,
    ),
    "response_incomplete": (
        sse(event("response.incomplete", response={"status": "incomplete", "usage": USAGE})),
        "stream_incomplete",
        200,
        USAGE,
    ),
    "mid_stream_close": (
        sse(event("response.created"), event("response.output_text.delta", delta="par")),
        "stream_incomplete",
        200,
        None,
    ),
    "empty_stream": (sse(), "stream_incomplete", 200, None),
    "empty_completion": (sse(completed_event("", usage=USAGE)), "completed", 200, USAGE),
    "empty_completion_without_usage": (
        sse(completed_event("", usage=None)),
        "completed",
        200,
        None,
    ),
    "completed_but_status_failed": (
        sse(completed_event("x", status="failed", usage=None)),
        "failed_event",
        200,
        None,
    ),
    "no_answer": (hang_up, "sent_no_response", None, None),
}


@pytest.mark.parametrize("case", CASES)
def test_every_way_an_attempt_can_end_makes_exactly_one_request(case, journal):
    script, outcome, code, usage = CASES[case]
    with FakeCodex(script) as server:
        result = run(server.url, journal)
    assert len(server.requests) == 1
    assert (result.outcome, result.status) == (outcome, code)
    assert result.usage == usage
    assert result.billing.state == ("usage_reported" if usage else "unattested")
    assert result.billing.usage == usage
    # started, then sent (written before the request), then finished with the same facts.
    assert kinds(journal) == ["started", "sent", "finished"]
    finished = journal.records()[-1]
    assert finished["outcome"] == outcome and finished["status"] == code
    assert finished["billing"] == {
        "state": "usage_reported" if usage else "unattested",
        "usage": usage,
    }
    assert journal.unfinished() == []
    assert NOT_BILLED.encode() not in all_text(journal.directory)


def test_absent_usage_stays_none_and_is_never_zero(journal):
    with FakeCodex(sse(completed_event("hi", usage=None))) as server:
        result = run(server.url, journal)
    assert result.outcome == "completed" and result.text == "hi"
    assert result.usage is None and result.billing.usage is None
    assert result.billing.state == "unattested"
    assert journal.records()[-1]["billing"] == {"state": "unattested", "usage": None}


def test_usage_is_the_provider_object_verbatim(journal):
    odd = {"input_tokens": 5, "extra_vendor_field": {"nested": [1, 2, 3]}, "total_tokens": None}
    with FakeCodex(sse(completed_event("hi", usage=odd))) as server:
        result = run(server.url, journal)
    assert result.usage == odd
    assert journal.records()[-1]["billing"]["usage"] == odd


def test_a_failed_event_with_no_usage_is_unattested_not_billed(journal):
    with FakeCodex(sse(event("response.failed", response={"status": "failed"}))) as server:
        result = run(server.url, journal)
    assert result.billing.state == "unattested" and result.usage is None


# What goes out ----------------------------------------------------------------------------


def expected_body():
    def image(raw, mime, detail):
        uri = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
        return {
            "role": "user",
            "content": [{"type": "input_image", "detail": detail, "image_url": uri}],
        }

    def text(value):
        return {"role": "user", "content": [{"type": "input_text", "text": value}]}

    return {
        "model": "gpt-x",
        "instructions": "You read schematics.",
        "input": [
            image(PNG, "image/png", "original"),
            text("context"),
            image(JPEG, "image/jpeg", "high"),
            text("question"),
        ],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "low"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "cache-key-1",
    }


def test_ordered_items_and_detail_arrive_byte_exact_with_no_resize_or_reorder(journal):
    with FakeCodex(sse(*ok_stream())) as server:
        run(server.url, journal)
    [received] = server.requests
    assert received.body == json.dumps(expected_body()).encode("utf-8")
    sent = json.loads(received.body)
    assert [part["content"][0]["type"] for part in sent["input"]] == [
        "input_image",
        "input_text",
        "input_image",
        "input_text",
    ]
    assert [sent["input"][i]["content"][0]["detail"] for i in (0, 2)] == ["original", "high"]
    # The image bytes that arrive are the bytes that were given: no re-encode, no resize.
    for index, raw in ((0, PNG), (2, JPEG)):
        uri = sent["input"][index]["content"][0]["image_url"]
        assert base64.b64decode(uri.split(",", 1)[1]) == raw
    assert not {"temperature", "top_p", "max_output_tokens", "max_tokens"} & set(sent)


def test_the_headers_on_the_wire(journal):
    with FakeCodex(sse(*ok_stream())) as server:
        result = run(
            server.url,
            journal,
            extra_headers={"x-codex-routing-hint": "model=gpt-x"},
            session_id="session-1",
            thread_id="thread-1",
        )
    [received] = server.requests
    assert received.method == "POST" and received.path == "/codex/responses"
    assert received.headers["authorization"] == "Bearer " + TOKEN
    assert received.headers["chatgpt-account-id"] == "acct-00000000-test"
    assert received.headers["openai-beta"] == "responses=experimental"
    assert received.headers["originator"] == "test-client"
    assert received.headers["version"] == "9.9.9"
    assert received.headers["session-id"] == "session-1"
    assert received.headers["thread-id"] == "thread-1"
    assert received.headers["x-client-request-id"] == result.attempt_id
    assert received.headers["x-codex-routing-hint"] == "model=gpt-x"
    assert received.headers["accept"] == "text/event-stream"
    assert received.headers["content-type"] == "application/json"
    assert (
        received.headers["accept-encoding"] == "identity" and received.count("accept-encoding") == 1
    )
    assert received.headers["content-length"] == str(len(received.body))


def test_session_and_thread_ids_default_to_a_fresh_uuid4_per_attempt(journal):
    seen = []
    with FakeCodex(sse(*ok_stream())) as server:
        for _ in range(2):
            result = run(server.url, journal)
            received = server.requests[-1]
            seen.append(
                (
                    received.headers["session-id"],
                    received.headers["thread-id"],
                    received.headers["x-client-request-id"],
                    result.attempt_id,
                )
            )
    flat = [value for row in seen for value in (row[0], row[1], row[3])]
    assert len(set(flat)) == len(flat) == 6
    for value in flat:
        assert uuid.UUID(value).version == 4
    assert all(row[2] == row[3] for row in seen)


def test_a_caller_supplied_attempt_id_is_the_one_on_the_wire_and_in_the_journal(journal):
    chosen = str(uuid.uuid4())
    with FakeCodex(sse(*ok_stream())) as server:
        result = run(server.url, journal, attempt_id=chosen, thread_id=chosen)
        received = server.requests[-1]
    assert result.attempt_id == chosen
    assert received.headers["x-client-request-id"] == chosen
    assert received.headers["thread-id"] == chosen
    assert {r["attempt_id"] for r in journal.records()} == {chosen}


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-uuid",
        str(uuid.uuid4()).upper(),
        str(uuid.uuid1()),
        uuid.uuid4().hex,
        "",
        123,
    ],
)
def test_an_attempt_id_that_is_not_a_canonical_uuid4_is_refused_before_any_request(bad, journal):
    with FakeCodex(sse(*ok_stream())) as server:
        with pytest.raises(ValueError):
            run(server.url, journal, attempt_id=bad)
        assert server.requests == []
    assert journal.records() == []


def test_a_reused_attempt_id_is_refused_and_sends_nothing(journal):
    chosen = str(uuid.uuid4())
    with FakeCodex(sse(*ok_stream())) as server:
        run(server.url, journal, attempt_id=chosen)
        before = len(journal.records())
        with pytest.raises(ValueError):
            run(server.url, journal, attempt_id=chosen)
        assert len(server.requests) == 1
    assert len(journal.records()) == before


def test_the_started_record_matches_what_was_sent(journal):
    with FakeCodex(sse(*ok_stream())) as server:
        result = run(server.url, journal)
    [received] = server.requests
    started = journal.history(result.attempt_id)[0]
    assert started["url"] == server.url + "/codex/responses"
    assert started["model"] == "gpt-x"
    assert started["request_bytes"] == len(received.body)
    assert started["headers"]["x-client-request-id"] == result.attempt_id
    assert "Authorization" not in started["headers"]
    assert started["headers_omitted"] == ["Authorization"]
    assert started["credential"] == {
        "credential_id": 7,
        "identity_key": "email:tester@example.com",
        "token_digest": make_lease().token_digest,
        "generation": 3,
        "account_id": "acct-00000000-test",
    }
    image = started["request"]["input"][0]["content"][0]["image_url"]
    assert image["mime_type"] == "image/png" and image["bytes"] == len(PNG)


def test_the_result_names_the_journal_files_and_the_served_model(journal):
    with FakeCodex(sse(*ok_stream("answer"))) as server:
        result = run(server.url, journal)
    assert isinstance(result, AttemptResult)
    assert result.text == "answer" and result.served_model == "gpt-served"
    assert result.terminal_event == "response.completed"
    assert result.journal_path == journal.path
    assert result.raw_path == journal.directory / (result.attempt_id + ".sse")
    assert result.chunks_path == journal.directory / (result.attempt_id + ".chunks.jsonl")
    assert result.raw_path.read_bytes() == b"".join(ok_stream("answer"))
    raw = journal.records()[-1]["raw"]
    assert raw["bytes"] == result.raw_path.stat().st_size


def test_text_comes_from_the_deltas_when_the_completion_carries_no_output(journal):
    chunks = [
        event("response.output_text.delta", delta="foo"),
        event("response.output_text.delta", delta="bar"),
        completed_event("", usage=USAGE),
    ]
    with FakeCodex(sse(*chunks)) as server:
        result = run(server.url, journal)
    assert result.outcome == "completed" and result.text == "foobar"


# Never a second request -------------------------------------------------------------------


def test_a_redirect_is_not_followed_and_is_an_http_rejection(journal):
    with FakeCodex(status(200, b"{}")) as elsewhere:

        def redirect(received, handler):
            respond_json(handler, 302, b"", [("Location", elsewhere.url + "/codex/responses")])

        with FakeCodex(redirect) as origin:
            result = run(origin.url, journal)
    assert (result.outcome, result.status) == ("http_rejected", 302)
    assert len(origin.requests) == 1 and elsewhere.requests == []
    assert ["location", elsewhere.url + "/codex/responses"] in [
        [k.lower(), v] for k, v in journal.records()[-1]["response_headers"]
    ]


@pytest.mark.parametrize("code", [301, 307, 308])
def test_no_redirect_status_is_followed(code, journal):
    with FakeCodex(status(200, b"{}")) as elsewhere:
        with FakeCodex(status(code, b"", [("Location", elsewhere.url)])) as origin:
            result = run(origin.url, journal)
    assert result.outcome == "http_rejected" and elsewhere.requests == []
    assert len(origin.requests) == 1


def test_proxy_environment_variables_are_not_used(journal, monkeypatch):
    with RawServer(drain) as proxy, FakeCodex(sse(*ok_stream())) as server:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy.url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        result = run(server.url, journal)
        assert result.outcome == "completed" and len(server.requests) == 1
        proxy.settle()
        assert proxy.connections == []


def test_one_lease_and_one_connection_per_send(journal):
    source = FakeSource()
    with FakeCodex(status(500)) as server:
        run(server.url, journal, source=source, min_remaining_ms=123_000, lease_wait_s=2.5)
    assert source.calls == [(123_000, 2.5)]
    assert len(server.requests) == 1


# Not sent ---------------------------------------------------------------------------------


def test_a_refused_connection_is_not_sent_and_leaves_no_sent_record(journal):
    result = run("http://127.0.0.1:%d" % closed_port(), journal)
    assert result.outcome == "not_sent" and result.status is None
    assert result.reason == "connect_failed" and "ConnectionRefusedError" in result.detail
    assert result.billing.state == "unattested" and result.usage is None
    assert result.raw_path is None
    assert kinds(journal) == ["started", "finished"]
    assert journal.records()[-1]["outcome"] == "not_sent"
    assert journal.unfinished() == []


def test_a_credential_that_cannot_be_leased_is_not_sent_and_nothing_is_started(journal):
    source = FakeSource(unavailable("expiring", "token has 5000 ms left"))
    with FakeCodex(status(200, b"{}")) as server:
        result = run(server.url, journal, source=source, retry_of="earlier")
    assert server.requests == []
    assert (result.outcome, result.reason) == ("not_sent", "expiring")
    assert result.detail == "token has 5000 ms left" and result.retry_of == "earlier"
    assert result.billing.state == "unattested"
    [record] = journal.records()
    assert record["kind"] == "not_sent" and record["reason"] == "expiring"
    assert record["retry_of"] == "earlier" and record["outcome"] == "not_sent"
    assert record["request"]["input"][0]["content"][0]["image_url"]["mime_type"] == "image/png"
    assert journal.unfinished() == []


def test_a_token_that_cannot_be_a_header_is_not_sent_and_is_never_recorded(journal):
    bad = "bad\nline-" + "Z" * 40
    with FakeCodex(status(200, b"{}")) as server:
        result = run(server.url, journal, source=FakeSource(make_lease(bad)))
    assert server.requests == [] and result.outcome == "not_sent"
    assert result.reason == "invalid_credential"
    assert kinds(journal) == ["not_sent"]
    assert b"ZZZZZZZZZZ" not in all_text(journal.directory)
    assert "ZZZZZZZZZZ" not in repr(result) + str(result.detail)


def test_malformed_caller_input_raises_before_a_credential_is_leased(journal):
    source = FakeSource()
    with pytest.raises(ValueError):
        run("http://127.0.0.1:1", journal, source=source, extra_headers={"Authorization": "x"})
    with pytest.raises(ValueError) as caught:
        run("http://127.0.0.1:1", journal, source=source, extra_headers={"x-a": "p\r\nq: " + TOKEN})
    assert TOKEN not in str(caught.value)
    with pytest.raises(ValueError):
        run("ftp://127.0.0.1", journal, source=source)
    assert source.calls == [] and journal.records() == []


# Crash and recovery -----------------------------------------------------------------------


def test_a_failure_to_record_sent_means_nothing_is_sent(tmp_path):
    class SentFails(Journal):
        def _append(self, record, secrets=()):
            if record["kind"] == "sent":
                raise OSError(28, "No space left on device")
            super()._append(record, secrets)

    journal = SentFails(tmp_path)
    with FakeCodex(status(200, b"{}")) as server:
        with pytest.raises(OSError, match="No space"):
            run(server.url, journal)
    assert server.requests == []
    [entry] = journal.unfinished()
    assert entry["outcome"] == "abandoned_before_send"


def test_an_exception_after_sent_leaves_the_attempt_unfinished_as_sent_unknown(
    tmp_path, monkeypatch
):
    def disk_full(self, t_ms, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(AttemptLog, "chunk", disk_full)
    journal = Journal(tmp_path)
    with FakeCodex(sse(*ok_stream())) as server:
        with pytest.raises(OSError, match="No space"):
            run(server.url, journal)
    assert len(server.requests) == 1
    assert kinds(journal) == ["started", "sent"]
    assert [e["outcome"] for e in journal.unfinished()] == ["sent_unknown"]


CHILD = """
import sys
sys.path[:0] = sys.argv[1:3]
from codex_direct_fakes import FakeSource
from annealage_ember.codex_direct import ClientIdentity, CodexRequest, Journal, send, text_item
send(
    CodexRequest("gpt-x", "inst", [text_item("q")]),
    FakeSource(),
    Journal(sys.argv[4]),
    identity=ClientIdentity("c", "1"),
    base_url=sys.argv[3],
)
"""


def test_a_process_killed_mid_stream_leaves_sent_unknown_with_the_bytes_received(tmp_path):
    hold = threading.Event()
    first = event("response.created", response={"id": "r", "pad": "p" * 400})
    held = len(TOKEN) - 1

    def script(received, handler):
        respond_sse(handler, [first])
        hold.wait(30)

    src = str(pathlib.Path(annealage_ember.__file__).parent.parent)
    tests = str(pathlib.Path(__file__).parent)
    with FakeCodex(script) as server:
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, src, tests, server.url, str(tmp_path)]
        )
        try:
            deadline = time.monotonic() + 20
            raw = None
            while time.monotonic() < deadline:
                files = list(tmp_path.glob("*.sse"))
                if files and files[0].stat().st_size >= len(first) - held:
                    raw = files[0]
                    break
                time.sleep(0.02)
            assert raw is not None, "the child never reached the stream"
            child.kill()
            child.wait(10)
        finally:
            hold.set()
            child.kill()
    assert len(server.requests) == 1
    journal = Journal(tmp_path)
    assert kinds(journal) == ["started", "sent"]
    [entry] = journal.unfinished()
    assert entry["outcome"] == "sent_unknown"
    assert entry["billing"] == {"state": "unattested", "usage": None}
    # Everything but the last len(token) - 1 bytes (held back against a split token) is on disk.
    assert entry["raw"]["bytes"] == len(first) - held and raw.read_bytes() == first[:-held]
    assert TOKEN.encode() not in raw.read_bytes()


# The replay gate --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_replaying_a_token_that_was_just_rejected_is_refused_with_no_request(code, journal):
    with FakeCodex(status(code)) as server:
        first = run(server.url, journal)
        assert first.outcome == "http_rejected" and first.status == code
        with pytest.raises(ReplayRefused) as caught:
            run(server.url, journal, retry_of=first.attempt_id)
        assert len(server.requests) == 1
    refused = caught.value
    assert refused.retry_of == first.attempt_id and refused.status == code
    assert refused.token_digest == make_lease().token_digest
    assert TOKEN not in str(refused) + repr(refused)
    # The refusal is journalled; no second `started` was written.
    assert kinds(journal) == ["started", "sent", "finished", "replay_refused"]
    record = journal.records()[-1]
    assert record["attempt_id"] == refused.attempt_id != first.attempt_id
    assert record["retry_of"] == first.attempt_id and record["prior_status"] == code
    assert record["credential"]["token_digest"] == make_lease().token_digest
    assert journal.unfinished() == []


def test_a_different_token_after_a_401_is_allowed_and_linked_by_retry_of(journal):
    new_token = "eyJhbGciOiJub25lIn0.rotated-token-" + "B" * 40
    source = FakeSource(make_lease(TOKEN), make_lease(new_token, credential_id=8))
    answers = [status(401), sse(*ok_stream("second try"))]

    def script(received, handler):
        answers[len(server.requests) - 1](received, handler)

    with FakeCodex(script) as server:
        first = run(server.url, journal, source=source)
        second = run(server.url, journal, source=source, retry_of=first.attempt_id)
    assert len(server.requests) == 2
    assert first.outcome == "http_rejected" and second.outcome == "completed"
    assert second.retry_of == first.attempt_id and second.text == "second try"
    assert server.requests[1].headers["authorization"] == "Bearer " + new_token
    started = journal.history(second.attempt_id)[0]
    assert started["retry_of"] == first.attempt_id
    assert started["credential"]["credential_id"] == 8
    assert "replay_refused" not in kinds(journal)


@pytest.mark.parametrize("code", [429, 500, 503, 404])
def test_a_retry_after_a_non_auth_rejection_is_allowed_even_with_the_same_token(code, journal):
    answers = [status(code), sse(*ok_stream())]

    def script(received, handler):
        answers[len(server.requests) - 1](received, handler)

    with FakeCodex(script) as server:
        first = run(server.url, journal)
        second = run(server.url, journal, retry_of=first.attempt_id)
    assert len(server.requests) == 2 and second.outcome == "completed"
    assert second.retry_of == first.attempt_id


def test_a_retry_after_a_stream_failure_or_an_unknown_attempt_is_allowed(journal):
    answers = [sse(event("error", error={})), sse(*ok_stream()), sse(*ok_stream())]

    def script(received, handler):
        answers[len(server.requests) - 1](received, handler)

    with FakeCodex(script) as server:
        first = run(server.url, journal)
        second = run(server.url, journal, retry_of=first.attempt_id)
        third = run(server.url, journal, retry_of="never-journalled")
    assert (first.outcome, second.outcome, third.outcome) == (
        "failed_event",
        "completed",
        "completed",
    )
    assert len(server.requests) == 3


def test_the_gate_checks_the_token_not_the_attempt_so_a_later_good_token_passes(journal):
    # 401 with token A, then a retry with token A is refused; a retry with token B passes.
    other = "eyJhbGciOiJub25lIn0.other-token-" + "C" * 40
    source = FakeSource(make_lease(TOKEN), make_lease(TOKEN), make_lease(other))
    with FakeCodex(status(401)) as server:
        first = run(server.url, journal, source=source)
        with pytest.raises(ReplayRefused):
            run(server.url, journal, source=source, retry_of=first.attempt_id)
        third = run(server.url, journal, source=source, retry_of=first.attempt_id)
    assert len(server.requests) == 2 and third.outcome == "http_rejected"


# Secrets ----------------------------------------------------------------------------------


def scan(directory, *needles):
    blob = all_text(directory)
    return [needle for needle in needles if needle.encode() in blob]


def test_no_token_or_image_is_in_any_journal_file_repr_or_exception(tmp_path):
    journal = Journal(tmp_path / "j", store_images=False)
    images = [base64.b64encode(PNG).decode(), base64.b64encode(JPEG).decode()]
    echo = json.dumps({"error": "bad credential " + TOKEN}).encode()
    results, errors = [], []

    def stream_echo(received, handler):
        respond_sse(
            handler,
            [
                event("response.output_text.delta", delta="your token is " + TOKEN),
                completed_event("your token is " + TOKEN, usage=USAGE),
            ],
        )

    scripts = [
        sse(*ok_stream()),
        status(401, echo, [("x-echo-auth", TOKEN)]),
        stream_echo,
        status(500),
        sse(event("error", error={"message": TOKEN})),
    ]
    for script in scripts:
        with FakeCodex(script) as server:
            results.append(run(server.url, journal))
    results.append(run("http://127.0.0.1:%d" % closed_port(), journal))
    results.append(run("http://127.0.0.1:1", journal, source=FakeSource(unavailable())))
    with FakeCodex(status(401)) as server:
        first = run(server.url, journal)
        try:
            run(server.url, journal, retry_of=first.attempt_id)
        except ReplayRefused as refused:
            errors.append(refused)
    assert scan(journal.directory, TOKEN, "Bearer", *images) == []
    for item in results + errors:
        assert TOKEN not in repr(item) + str(item)
    assert TOKEN not in repr(make_lease()) + str(make_lease())
    # The token's digest is recorded instead; every record that names a credential agrees.
    digests = {r["credential"]["token_digest"] for r in journal.records() if r.get("credential")}
    assert digests == {make_lease().token_digest}


def test_a_server_that_echoes_the_token_has_it_redacted_in_the_raw_file_and_the_text(tmp_path):
    journal = Journal(tmp_path)
    chunks = [
        event("response.output_text.delta", delta="echo: " + TOKEN),
        completed_event("echo: " + TOKEN, usage=USAGE),
    ]
    with FakeCodex(sse(*chunks)) as server:
        result = run(server.url, journal)
    assert TOKEN not in result.text and "[redacted]" in result.text
    assert journal.records()[-1]["raw"]["redacted"] is True
    assert scan(tmp_path, TOKEN) == []
    assert re.search(rb"echo: \[redacted\]", result.raw_path.read_bytes())


def test_nothing_in_the_package_or_a_journal_names_a_not_billed_state(tmp_path):
    package = pathlib.Path(annealage_ember.__file__).parent / "codex_direct"
    for path in package.glob("*.py"):
        assert NOT_BILLED not in path.read_text(), path.name
    journal = Journal(tmp_path)
    for script in (status(401), sse(*ok_stream()), hang_up, sse(event("error", error={}))):
        with FakeCodex(script) as server:
            run(server.url, journal)
    run("http://127.0.0.1:%d" % closed_port(), journal)
    states = {r["billing"]["state"] for r in journal.records() if "billing" in r}
    assert states == {"usage_reported", "unattested"}
    assert NOT_BILLED.encode() not in all_text(tmp_path)


def test_an_early_hangup_while_the_body_is_going_out_is_a_result_not_a_crash(journal):
    def behaviour(sock, connection):
        sock.recv(1024)
        sock.sendall(b"HTTP/1.0 401 Unauthorized\r\ncontent-length: 2\r\n\r\n{}")

    big = CodexRequest("m", "i", [image_item(PNG + b"\x00" * (6 * 1024 * 1024), detail="high")])
    with RawServer(behaviour) as server:
        result = run(server.url, journal, request=big)
    server.settle()
    assert len(server.connections) == 1
    assert result.outcome in ("http_rejected", "sent_no_response")
    assert kinds(journal) == ["started", "sent", "finished"]


# Concurrency, scrubbing of the returned result, usage locations, limits -------------------


class SlowSource(FakeSource):
    def lease(self, **kwargs):
        time.sleep(0.3)
        return super().lease(**kwargs)


@pytest.mark.parametrize("shared_handle", [True, False])
def test_two_concurrent_sends_with_one_attempt_id_make_exactly_one_post(tmp_path, shared_handle):
    chosen = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    outcomes = []
    first = Journal(tmp_path / "j")
    # A second Journal on the same directory stands in for a second process.
    handles = [first, first if shared_handle else Journal(tmp_path / "j")]

    def attempt(handle):
        barrier.wait()
        try:
            outcomes.append(run(url, handle, source=SlowSource(), attempt_id=chosen))
        except ValueError as refusal:
            outcomes.append(refusal)

    with FakeCodex(sse(*ok_stream())) as server:
        url = server.url
        threads = [threading.Thread(target=attempt, args=(h,)) for h in handles]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert len(server.requests) == 1
    assert sorted(type(o).__name__ for o in outcomes) == ["AttemptResult", "ValueError"]
    assert kinds(first) == ["started", "sent", "finished"]
    assert [p.name for p in (first.directory / "claims").iterdir()] == [chosen]


def test_generated_attempt_ids_are_claimed_too(journal):
    with FakeCodex(sse(*ok_stream())) as server:
        result = run(server.url, journal)
    assert (journal.directory / "claims" / result.attempt_id).exists()


def test_a_refused_attempt_id_never_reaches_the_lease(journal):
    chosen = str(uuid.uuid4())
    journal.claim(chosen)
    source = FakeSource()
    with pytest.raises(ValueError, match="already"):
        run("http://127.0.0.1:1", journal, source=source, attempt_id=chosen)
    assert source.calls == []


def walk_strings(value, seen=None):
    """Every string reachable from ``value``: attributes, dataclass fields, keys and items."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(key, seen)
            yield from walk_strings(item, seen)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from walk_strings(item, seen)
    elif isinstance(value, pathlib.PurePath):
        yield str(value)
    elif hasattr(value, "__dict__") or dataclasses.is_dataclass(value):
        for item in vars(value).values():
            yield from walk_strings(item, seen)


def test_the_returned_result_carries_no_token_in_model_usage_text_or_errors(journal):
    leaky_usage = {
        "input_tokens": 5,
        "note": "billed to " + TOKEN,
        "nested": {"deep": [{"who": TOKEN}], TOKEN: 1},
    }
    leaky = completed_event("said " + TOKEN, usage=leaky_usage, model="model-" + TOKEN)
    with FakeCodex(sse(leaky)) as server:
        result = run(server.url, journal)
    assert result.outcome == "completed" and result.billing.state == "usage_reported"
    assert result.served_model == "model-[redacted]"
    assert result.usage["note"] == "billed to [redacted]"
    assert result.usage["nested"]["deep"] == [{"who": "[redacted]"}]
    assert result.usage["nested"]["[redacted]"] == 1 and result.usage["input_tokens"] == 5
    assert (
        TOKEN not in repr(result) and TOKEN not in str(result) and TOKEN not in repr(result.billing)
    )
    assert [s for s in walk_strings(result) if TOKEN in s] == []
    finished = journal.records()[-1]
    assert finished["served_model"] == "model-[redacted]"
    assert finished["billing"]["usage"]["note"] == "billed to [redacted]"
    assert TOKEN.encode() not in all_text(journal.directory)


def test_a_token_in_a_failed_event_type_and_error_text_is_scrubbed_from_the_result(journal):
    chunks = [event("response.failed", response={"status": "failed", "model": TOKEN})]
    with FakeCodex(sse(*chunks)) as server:
        result = run(server.url, journal)
    assert result.outcome == "failed_event" and result.served_model == "[redacted]"
    assert [s for s in walk_strings(result) if TOKEN in s] == []


USAGE_A = {"input_tokens": 1, "output_tokens": 2}
USAGE_B = {"input_tokens": 10, "output_tokens": 20}


def test_an_event_level_usage_object_is_reported(journal):
    chunks = [
        event("response.created"),
        event("usage", usage=USAGE_A),
        completed_event("x", usage=None),
    ]
    with FakeCodex(sse(*chunks)) as server:
        result = run(server.url, journal)
    assert result.outcome == "completed" and result.usage == USAGE_A
    assert result.billing.state == "usage_reported"
    assert journal.records()[-1]["billing"] == {"state": "usage_reported", "usage": USAGE_A}


def test_the_last_non_empty_usage_in_event_order_wins_wherever_it_sits(journal):
    chunks = [
        event("usage", usage=USAGE_A),
        event("usage", usage={}),  # empty: ignored
        event("usage", usage=None),
        event("response.created", response={"usage": USAGE_B}),
        event("usage", usage=USAGE_A),
        completed_event("x", usage=None),
    ]
    with FakeCodex(sse(*chunks)) as server:
        result = run(server.url, journal)
    assert result.usage == USAGE_A
    chunks = [event("usage", usage=USAGE_A), completed_event("x", usage=USAGE_B)]
    with FakeCodex(sse(*chunks)) as server:
        assert run(server.url, journal).usage == USAGE_B


def test_no_usage_anywhere_stays_none_even_with_other_event_objects(journal):
    chunks = [
        event("usage", usage={}),
        event("response.created", usage=None),
        completed_event("x", usage=None),
    ]
    with FakeCodex(sse(*chunks)) as server:
        result = run(server.url, journal)
    assert result.usage is None and result.billing.state == "unattested"


def test_a_leading_byte_order_mark_does_not_turn_a_completion_into_stream_incomplete(journal):
    # The only event has no `event:` line, so a BOM left on its `data` field name loses it.
    only = completed_event("bom ok").split(b"\n", 1)[1]
    chunks = [b"\xef", b"\xbb", b"\xbf", only]
    with FakeCodex(lambda r, h: respond_sse(h, chunks, delay=0.03)) as server:
        result = run(server.url, journal)
    assert result.outcome == "completed" and result.text == "bom ok" and result.usage == USAGE
    assert result.raw_path.read_bytes() == b"".join(chunks)  # the raw file keeps the BOM


def stream_limit_case(monkeypatch, name, value, chunks):
    monkeypatch.setattr(transport, name, value)
    return sse(*chunks)


@pytest.mark.parametrize(
    ("limit", "value", "chunks"),
    [
        (
            "MAX_LINE",
            1000,
            [event("response.created"), b"data: " + b"x" * 700, b"x" * 700, event("error")],
        ),
        ("MAX_EVENT", 1000, [b"data: " + b"y" * 100 + b"\n"] * 30 + [event("error")]),
        (
            "MAX_STREAM",
            400,
            [event("response.output_text.delta", delta="z" * 100)] * 8 + [event("error")],
        ),
    ],
)
def test_a_stream_over_a_limit_ends_stream_incomplete_keeps_its_raw_file_and_is_not_retried(
    limit, value, chunks, journal, monkeypatch
):
    script = stream_limit_case(monkeypatch, limit, value, chunks)
    with FakeCodex(script) as server:
        result = run(server.url, journal)
    assert len(server.requests) == 1
    assert result.outcome == "stream_incomplete" and result.status == 200
    assert result.terminal_event is None
    assert result.error_phase == "limit" and "SseLimitExceeded" in result.error
    assert result.billing.state == "unattested"
    assert kinds(journal) == ["started", "sent", "finished"]
    raw = result.raw_path.read_bytes()
    assert raw and b"".join(chunks).startswith(raw)  # a prefix of what the server sent
    finished = journal.records()[-1]
    assert finished["error_phase"] == "limit" and finished["raw"]["bytes"] == len(raw)
    assert finished["raw"]["received_bytes"] == len(raw)
