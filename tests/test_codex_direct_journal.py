"""Tests for ``codex_direct.journal``: durable, ordered, secret-free, crash-tolerant records."""

import base64
import hashlib
import json
import os
import stat

import pytest
from codex_direct_fakes import PNG, TOKEN, USAGE

from annealage_ember.codex_direct import CodexRequest, Journal, image_item, text_item
from annealage_ember.codex_direct import journal as journal_module
from annealage_ember.codex_direct.journal import UNATTESTED, Billing


def kinds_of(journal):
    return [r["kind"] for r in journal.records()]


CREDENTIAL = {
    "credential_id": 7,
    "identity_key": "email:tester@example.com",
    "token_digest": hashlib.sha256(TOKEN.encode()).hexdigest(),
    "generation": 3,
    "account_id": "acct-1",
}
HEADERS = {
    "Authorization": "Bearer " + TOKEN,
    "chatgpt-account-id": "acct-1",
    "session-id": "s",
    "x-api-key": "k-123",
}


def body_of(*items, **extra):
    return CodexRequest("gpt-x", "inst", list(items) or [text_item("q")], **extra).body_bytes()


def start(journal, attempt_id="a1", **overrides):
    args = {
        "attempt_id": attempt_id,
        "retry_of": None,
        "url": "http://127.0.0.1:1/codex/responses",
        "body": body_of(),
        "headers": HEADERS,
        "credential": CREDENTIAL,
        "secrets": (TOKEN,),
    }
    args.update(overrides)
    return journal.start(**args)


def lines(journal):
    return [json.loads(line) for line in journal.path.read_text().splitlines()]


def all_file_bytes(directory):
    return b"".join(path.read_bytes() for path in directory.rglob("*") if path.is_file())


def test_records_are_appended_in_order_as_one_json_object_per_line(tmp_path):
    journal = Journal(tmp_path / "j")
    log = start(journal)
    log.mark_sent()
    log.finish(outcome="completed", billing=Billing.from_usage(USAGE), status=200)
    assert [r["kind"] for r in lines(journal)] == ["started", "sent", "finished"]
    assert {r["attempt_id"] for r in lines(journal)} == {"a1"}
    assert all(r["ts"].endswith("Z") for r in lines(journal))


def test_every_record_is_fsynced_before_the_call_returns(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    synced = []
    real = os.fsync

    def spy(fd):
        # Whatever is in the journal at the instant of the fsync is what became durable.
        synced.append(len(journal.path.read_bytes().splitlines()))
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    log = start(journal)
    assert synced[-1] == 1
    log.mark_sent()
    assert synced[-1] == 2
    log.finish(outcome="http_rejected", billing=UNATTESTED, status=401)
    assert synced[-1] == 3


def test_files_are_private_to_the_owner(tmp_path):
    journal = Journal(tmp_path / "j")
    log = start(journal)
    log.chunk(1.0, b"data: x\n\n")
    log.finish(outcome="stream_incomplete", billing=UNATTESTED, status=200)
    for path in (journal.path, log.raw_path, log.chunks_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.directory.stat().st_mode) == 0o700


def test_started_records_the_request_credential_and_headers_but_not_the_token(tmp_path):
    journal = Journal(tmp_path)
    body = body_of(text_item("q"), effort="low", include=["reasoning.encrypted_content"])
    start(journal, body=body, retry_of="earlier")
    [started] = lines(journal)
    assert started["retry_of"] == "earlier" and started["model"] == "gpt-x"
    assert started["request"] == json.loads(body)
    assert started["request_bytes"] == len(body)
    assert started["request_sha256"] == hashlib.sha256(body).hexdigest()
    assert started["credential"] == CREDENTIAL
    assert started["headers"] == {"chatgpt-account-id": "acct-1", "session-id": "s"}
    assert sorted(started["headers_omitted"]) == ["Authorization", "x-api-key"]
    assert TOKEN.encode() not in all_file_bytes(tmp_path)


def test_images_are_recorded_as_mime_bytes_and_digest_and_blobs_are_opt_in(tmp_path):
    body = body_of(image_item(PNG, detail="original"), text_item("q"))
    plain = Journal(tmp_path / "plain")
    start(plain, body=body)
    [record] = lines(plain)
    assert record["request"]["input"][0]["content"][0]["image_url"] == {
        "mime_type": "image/png",
        "bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert not (plain.directory / "blobs").exists()

    assert base64.b64encode(PNG) not in all_file_bytes(plain.directory)

    stored = Journal(tmp_path / "stored", store_images=True)
    start(stored, body=body)
    start(stored, attempt_id="a2", body=body)  # the same image again: stored once
    blob = stored.directory / "blobs" / hashlib.sha256(PNG).hexdigest()
    assert blob.read_bytes() == PNG
    assert len(list((stored.directory / "blobs").iterdir())) == 1
    assert base64.b64encode(PNG) not in all_file_bytes(stored.directory)


def test_finished_carries_the_outcome_headers_body_and_billing(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.mark_sent()
    log.finish(
        outcome="http_rejected",
        billing=UNATTESTED,
        status=429,
        headers=[("Retry-After", "30"), ("Set-Cookie", "a=b"), ("set-cookie", "c=d"), ("X-A", "1")],
        body=b'{"error":"slow down"}',
        body_truncated=False,
        elapsed_ms=12.5,
    )
    finished = lines(journal)[-1]
    assert finished["outcome"] == "http_rejected" and finished["status"] == 429
    assert finished["response_headers"] == [["Retry-After", "30"], ["X-A", "1"]]
    assert finished["response_body"] == '{"error":"slow down"}'
    assert finished["billing"] == {"state": "unattested", "usage": None}
    assert finished["raw"] is None and finished["elapsed_ms"] == 12.5


HOLDBACK = len(TOKEN) - 1


def log_entries(log):
    return [json.loads(x) for x in log.chunks_path.read_text().splitlines()]


def test_without_a_credential_to_hide_the_raw_stream_is_stored_verbatim_as_it_arrives(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal, secrets=())
    pieces = [b"event: a\r\ndata: {}\r\n\r\n", b"\xc3", b"\xa9 tail"]  # a split UTF-8 sequence
    for index, piece in enumerate(pieces):
        log.chunk(index * 1.5, piece)
        assert log.raw_path.read_bytes() == b"".join(pieces[: index + 1])  # no holdback, no finish
    record = log.finish(outcome="stream_incomplete", billing=UNATTESTED, status=200)
    raw = b"".join(pieces)
    assert log.raw_path.read_bytes() == raw and log.raw_path.name == "a1.sse"
    assert log_entries(log) == [
        {"t_ms": 0.0, "nbytes": len(pieces[0]), "stored": len(pieces[0])},
        {"t_ms": 1.5, "nbytes": 1, "stored": 1},
        {"t_ms": 3.0, "nbytes": len(pieces[2]), "stored": len(pieces[2])},
    ]
    assert record["raw"] == {
        "file": "a1.sse",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "received_bytes": len(raw),
        "chunks_file": "a1.chunks.jsonl",
        "chunks": 3,
        "redacted": False,
    }
    assert lines(journal)[-1]["raw"] == record["raw"]


def test_the_chunk_log_accounts_for_what_was_received_and_what_was_stored(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)  # a credential to hide: the last HOLDBACK bytes wait for the next chunk
    pieces = [b"a" * 200, b"b" * 10, b"c" * 90]
    for index, piece in enumerate(pieces):
        log.chunk(float(index), piece)
    on_disk_before_finish = log.raw_path.read_bytes()
    assert on_disk_before_finish == b"".join(pieces)[:-HOLDBACK]
    record = log.finish(outcome="stream_incomplete", billing=UNATTESTED, status=200)
    raw = b"".join(pieces)
    entries = log_entries(log)
    assert [e["nbytes"] for e in entries] == [200, 10, 90, 0]
    assert entries[-1]["flush"] is True and entries[-1]["stored"] == HOLDBACK
    assert sum(e["stored"] for e in entries) == len(raw) == log.raw_path.stat().st_size
    assert record["raw"]["bytes"] == len(raw) and record["raw"]["received_bytes"] == len(raw)
    assert record["raw"]["sha256"] == hashlib.sha256(log.raw_path.read_bytes()).hexdigest()
    assert record["raw"]["redacted"] is False and record["raw"]["chunks"] == 3


def test_abort_flushes_the_held_back_bytes_and_writes_no_finished(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.mark_sent()
    log.chunk(0.5, b"x" * 300)
    assert log.raw_path.stat().st_size == 300 - HOLDBACK
    log.abort()
    assert log.raw_path.read_bytes() == b"x" * 300
    assert kinds_of(journal) == ["started", "sent"]
    assert journal.unfinished()[0]["raw"]["bytes"] == 300
    log.abort()  # idempotent


def test_a_token_split_across_chunks_never_reaches_the_file_even_if_the_process_dies(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.mark_sent()
    half = len(TOKEN) // 2
    log.chunk(0.0, b"data: you sent " + TOKEN[:half].encode())
    log.chunk(1.0, TOKEN[half:].encode() + b"\n\n")
    # No finish and no abort: the state a kill leaves behind.
    on_disk = all_file_bytes(tmp_path)
    assert TOKEN.encode()[:12] not in on_disk and TOKEN.encode()[half:][:12] not in on_disk
    assert log.raw_path.read_bytes() == b""  # all of it is still inside the holdback window
    record = log.finish(outcome="stream_incomplete", billing=UNATTESTED, status=200)
    assert record["raw"]["redacted"] is True
    assert log.raw_path.read_bytes() == b"data: you sent [redacted]\n\n"
    assert TOKEN.encode() not in all_file_bytes(tmp_path)


def test_a_stream_that_ends_partway_through_the_token_does_not_store_the_prefix(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.chunk(0.0, b"data: you sent " + TOKEN[: len(TOKEN) // 2].encode())
    log.abort()  # the held-back tail is flushed, with the token prefix hidden
    stored = log.raw_path.read_bytes()
    assert stored == b"data: you sent [redacted]"
    assert TOKEN.encode()[:12] not in all_file_bytes(tmp_path)


def test_a_token_echoed_by_the_server_is_redacted_everywhere_including_across_chunks(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.mark_sent()
    half = len(TOKEN) // 2
    log.chunk(0.0, b"data: you sent " + TOKEN[:half].encode())
    log.chunk(1.0, TOKEN[half:].encode() + b"\n\n")
    record = log.finish(
        outcome="stream_incomplete",
        billing=UNATTESTED,
        status=200,
        headers=[("x-echo", TOKEN)],
        text="the token was " + TOKEN,
        error="oops " + TOKEN,
    )
    assert record["raw"]["redacted"] is True
    assert log.raw_path.read_bytes() == b"data: you sent [redacted]\n\n"
    assert record["raw"]["sha256"] == hashlib.sha256(log.raw_path.read_bytes()).hexdigest()
    assert record["raw"]["bytes"] == log.raw_path.stat().st_size
    assert record["raw"]["received_bytes"] == len(b"data: you sent " + TOKEN.encode() + b"\n\n")
    assert TOKEN.encode() not in all_file_bytes(tmp_path)
    finished = lines(journal)[-1]
    assert finished["response_headers"] == [["x-echo", "[redacted]"]]
    assert finished["text"] == "the token was [redacted]"


def test_the_json_escaped_form_of_the_token_is_redacted_too(tmp_path):
    odd = 'tok"en\\with/odd' + "Q" * 30
    journal = Journal(tmp_path)
    log = start(journal, secrets=(odd,))
    escaped = json.dumps(odd)[1:-1]
    log.chunk(0.0, ("data: " + escaped + " and raw " + odd + "\n").encode())
    log.finish(outcome="stream_incomplete", billing=UNATTESTED, status=200)
    assert log.raw_path.read_bytes() == b"data: [redacted] and raw [redacted]\n"


def test_the_directory_is_fsynced_when_the_evidence_files_are_created(tmp_path, monkeypatch):
    synced = []
    real = journal_module._fsync_dir
    monkeypatch.setattr(journal_module, "_fsync_dir", lambda d: (synced.append(d), real(d)))
    journal = Journal(tmp_path)
    log = start(journal)
    synced.clear()
    log.chunk(0.0, b"data: x\n\n")
    assert synced == [journal.directory] and log.raw_path.exists() and log.chunks_path.exists()
    log.chunk(1.0, b"more")
    assert synced == [journal.directory]  # only on creation


def test_chunks_are_fsynced_when_the_attempt_finishes_not_before(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    log = start(journal, secrets=())
    log.chunk(0.0, b"data: x\n\n")
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    log.chunk(1.0, b"data: y\n\n")
    assert synced == []
    log.finish(outcome="stream_incomplete", billing=UNATTESTED, status=200)
    assert len(synced) >= 3  # the raw file, the chunk log and the finished record


def test_claim_gives_an_attempt_id_to_exactly_one_caller(tmp_path):
    journal = Journal(tmp_path)
    attempt = "0b2f1c3e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
    journal.claim(attempt)
    claim = journal.directory / "claims" / attempt
    assert claim.exists() and stat.S_IMODE(claim.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        journal.claim(attempt)
    with pytest.raises(FileExistsError):
        Journal(tmp_path).claim(attempt)  # another handle, as another process would have
    for bad in ("../x", "a1", attempt.upper(), ""):
        with pytest.raises(ValueError):
            journal.claim(bad)


def test_claim_treats_an_id_with_records_but_no_claim_as_taken(tmp_path):
    journal = Journal(tmp_path)
    attempt = "0b2f1c3e-4d5a-4b6c-8d7e-9f0a1b2c3d4f"
    start(journal, attempt_id=attempt)  # a journal written before claims existed
    with pytest.raises(FileExistsError):
        journal.claim(attempt)


def test_claim_fsyncs_the_file_and_its_directory(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    synced, dirs = [], []
    real_fsync, real_dir = os.fsync, journal_module._fsync_dir
    monkeypatch.setattr(journal_module, "_fsync_dir", lambda d: (dirs.append(d), real_dir(d)))
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    journal.claim("0b2f1c3e-4d5a-4b6c-8d7e-9f0a1b2c3d50")
    assert dirs and dirs[0] == journal.directory / "claims"


def test_a_token_in_a_request_field_is_redacted_from_the_record(tmp_path):
    journal = Journal(tmp_path)
    start(journal, headers={**HEADERS, "x-note": "hello " + TOKEN})
    assert TOKEN.encode() not in all_file_bytes(tmp_path)
    assert lines(journal)[0]["headers"]["x-note"] == "hello [redacted]"


def test_unfinished_reports_sent_unknown_when_sent_was_written(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.mark_sent()
    log.chunk(1.0, b"data: " * 30)
    [entry] = journal.unfinished()
    assert entry["attempt_id"] == "a1" and entry["outcome"] == "sent_unknown"
    assert entry["billing"] == {"state": "unattested", "usage": None}
    assert entry["started"]["kind"] == "started"
    # What reached disk: the chunk less the bytes held back in case a token straddles chunks.
    assert entry["raw"] == {"file": "a1.sse", "bytes": 180 - HOLDBACK}


def test_unfinished_reports_abandoned_before_send_when_sent_was_not_written(tmp_path):
    journal = Journal(tmp_path)
    start(journal)
    [entry] = journal.unfinished()
    assert entry["outcome"] == "abandoned_before_send" and entry["raw"] is None
    assert entry["billing"] == {"state": "unattested", "usage": None}


def test_unfinished_ignores_finished_and_never_started_attempts(tmp_path):
    journal = Journal(tmp_path)
    done = start(journal, attempt_id="done")
    done.mark_sent()
    done.finish(outcome="completed", billing=Billing.from_usage(USAGE), status=200)
    closed = start(journal, attempt_id="closed")
    closed.finish(outcome="not_sent", billing=UNATTESTED, reason="connect_failed", detail="x")
    journal.not_sent(
        attempt_id="ns", retry_of=None, url="u", body=body_of(), reason="no_match", detail="d"
    )
    journal.replay_refused(
        attempt_id="rr", retry_of="done", credential=CREDENTIAL, prior_status=401
    )
    start(journal, attempt_id="open")
    assert [e["attempt_id"] for e in journal.unfinished()] == ["open"]


def test_a_truncated_last_line_is_skipped_and_later_appends_stay_readable(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.mark_sent()
    with open(journal.path, "ab") as handle:  # a crash mid-write: no newline, invalid JSON
        handle.write(b'{"kind":"finished","attempt_id":"a1","ts":"2026-')
    assert [r["kind"] for r in journal.records()] == ["started", "sent"]
    assert [e["outcome"] for e in journal.unfinished()] == ["sent_unknown"]

    reopened = Journal(tmp_path)
    again = start(reopened, attempt_id="a2")
    again.mark_sent()
    assert [(r["kind"], r["attempt_id"]) for r in reopened.records()] == [
        ("started", "a1"),
        ("sent", "a1"),
        ("started", "a2"),
        ("sent", "a2"),
    ]


def test_a_truncated_final_line_cut_at_every_byte_never_breaks_reading(tmp_path):
    source = Journal(tmp_path / "src")
    log = start(source)
    log.mark_sent()
    log.finish(outcome="completed", billing=Billing.from_usage(USAGE), status=200, text="é")
    full = source.path.read_bytes()
    last_start = full.rstrip(b"\n").rfind(b"\n") + 1
    for cut in range(last_start, len(full) - 1):
        cut_dir = tmp_path / ("cut%d" % cut)
        cut_dir.mkdir()
        (cut_dir / "attempts.jsonl").write_bytes(full[:cut])
        journal = Journal(cut_dir)
        assert [r["kind"] for r in journal.records()] == ["started", "sent"], cut
        assert [e["outcome"] for e in journal.unfinished()] == ["sent_unknown"], cut


def test_garbage_between_records_is_skipped(tmp_path):
    journal = Journal(tmp_path)
    start(journal)
    with open(journal.path, "ab") as handle:
        handle.write(b"not json\n[1,2]\n{}\n")
    start(journal, attempt_id="a2")
    assert [r["attempt_id"] for r in journal.records()] == ["a1", "a2"]


def test_history_returns_one_attempts_records(tmp_path):
    journal = Journal(tmp_path)
    one, two = start(journal, attempt_id="one"), start(journal, attempt_id="two")
    one.mark_sent()
    two.finish(outcome="not_sent", billing=UNATTESTED, reason="connect_failed")
    assert [r["kind"] for r in journal.history("one")] == ["started", "sent"]
    assert [r["kind"] for r in journal.history("two")] == ["started", "finished"]
    assert journal.history("missing") == []


def test_not_sent_and_replay_refused_records(tmp_path):
    journal = Journal(tmp_path)
    journal.not_sent(
        attempt_id="x",
        retry_of="y",
        url="u",
        body=body_of(image_item(PNG, detail="low")),
        reason="no_match",
        detail="no credential",
    )
    journal.replay_refused(attempt_id="z", retry_of="y", credential=CREDENTIAL, prior_status=403)
    not_sent, refused = lines(journal)
    assert not_sent["kind"] == "not_sent" and not_sent["outcome"] == "not_sent"
    assert not_sent["reason"] == "no_match" and not_sent["retry_of"] == "y"
    assert not_sent["billing"] == {"state": "unattested", "usage": None}
    assert not_sent["request"]["input"][0]["content"][0]["image_url"]["mime_type"] == "image/png"
    assert refused["kind"] == "replay_refused" and refused["prior_status"] == 403
    assert refused["credential"]["token_digest"] == CREDENTIAL["token_digest"]


def test_billing_is_usage_reported_with_usage_or_unattested_without_and_nothing_else():
    assert Billing.from_usage(USAGE) == Billing("usage_reported", USAGE)
    for absent in (None, {}, [], "x", 0):
        assert Billing.from_usage(absent) == Billing("unattested", None)
    assert Billing("unattested").as_dict() == {"state": "unattested", "usage": None}
    for bad in (
        ("unattested", {"input_tokens": 1}),
        ("usage_reported", None),
        ("usage_reported", {}),
        ("billed", {"a": 1}),
        ("not" + "_billed", None),
        ("", None),
    ):
        with pytest.raises(ValueError):
            Billing(*bad)


def test_the_journal_directory_is_created_with_parents(tmp_path):
    journal = Journal(tmp_path / "a" / "b" / "c")
    assert journal.path.exists() and journal.records() == []


def test_a_whole_token_in_a_chunk_is_gone_after_abort_with_no_finish(tmp_path):
    journal = Journal(tmp_path)
    log = start(journal)
    log.chunk(0.0, b"data: " + TOKEN.encode() + b" and more")
    log.abort()
    assert log.raw_path.read_bytes() == b"data: [redacted] and more"
    assert TOKEN.encode() not in all_file_bytes(tmp_path)
