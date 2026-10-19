"""Consumer-visible explicit notification, real HTTP admission and durable replay."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from conftest import make_test_client
from test_review_routes import AGENT_TOKEN, FRONT, TOKEN, _app, _body

from annealage_ember import sessions
from annealage_ember.identity import TailscaleIdentity
from annealage_ember.session.base import InputState, TurnEnd, UserTurn
from annealage_ember.session.fake import FakeSession

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def running_app(served_dir):
    sid = sessions.create_session(served_dir)

    def factory(on_event, *, bus):
        return FakeSession(on_event, session_id=sid)

    app = _app(served_dir, session_id=sid, build_session=factory)
    yield app
    await app.agent_stop()


def _notify(app, ids, *, token=TOKEN, body=None, headers=None):
    return make_test_client(app).post(
        "/review/notify?t=" + token,
        headers={"Content-Type": "application/json", **(headers or {})},
        body=json.dumps({"comment_ids": ids} if body is None else body),
    )


def _add(app, text="Please check this", author="human"):
    return app.agent_review_store.add_comment(anchor=FRONT, text=text, author=author).comment.id


def _active(app):
    bus = app.agent_bus
    bus.begin_turn([])
    app.agent_event_log.append(UserTurn(turn=bus.turn, blocks=[{"type": "text", "text": "Existing work"}]))
    bus.mark_turn_submitted(bus.turn)


def _finish(app):
    app.agent_session.emit(TurnEnd(turn=app.agent_bus.turn, stop_reason="end_turn", cost_usd=0.0))


def _turns(app):
    return [wire for _seq, wire in app.agent_event_log.replay(0).events if wire["kind"] == "user_turn"]


async def test_idle_notify_is_an_ordinary_human_turn_with_durable_identity(running_app):
    app = running_app
    comment_id = _add(app)
    starts = []
    app.agent_bus.on_turn_start(lambda turn: starts.append((turn, app.agent_bus.turn_automation_source)))
    response = await _notify(app, [comment_id])
    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] and body["status"] == "sent"
    assert body["comment_ids"] == [comment_id]
    event = _turns(app)[0]
    assert event["review_notification_ids"] == body["notification_ids"]
    assert "automation_source" not in event
    assert starts == [(1, None)]
    text = app.agent_session.submitted_turns[0][0][-1]["text"]
    assert "list_comments" in text and "current text and state" in text
    assert "workflow acceptance" in text and "budget grant" in text
    metadata = json.loads(text.split("\n", 1)[1])
    assert metadata[0]["comment_id"] == comment_id
    assert metadata[0]["anchor"] == app.agent_review_store.get_comment(comment_id).anchor
    assert len(metadata[0]["current_review_revision"]) == 64
    assert app.agent_session.interrupted == 0


@pytest.mark.parametrize("token,headers", [
    (AGENT_TOKEN, {}), ("wrong", {}), (TOKEN, {"Origin": "https://evil.example"}),
])
async def test_notify_requires_human_authentication(running_app, token, headers):
    comment_id = _add(running_app)
    response = await _notify(running_app, [comment_id], token=token, headers=headers)
    assert response.status_code == 403
    assert running_app.agent_review_notifications.ledger.read() == {}
    assert running_app.agent_session.submitted_turns == []


@pytest.mark.parametrize("body", [
    {}, {"comment_ids": []}, {"comment_ids": [True]}, {"comment_ids": [0]},
    {"comment_ids": [1, 1]}, {"comment_ids": ["1"]}, {"comment_ids": list(range(1, 102))},
    {"comment_ids": [1], "text": "not accepted"},
])
async def test_notify_rejects_invalid_ids_without_saving_or_dispatching(running_app, body):
    response = await _notify(running_app, [], body=body)
    assert response.status_code == 400
    assert running_app.agent_review_notifications.ledger.read() == {}
    assert running_app.agent_review_store.list_comments().comments == ()


async def test_no_review_watcher_callout_status_or_anchor_change_creates_intent(running_app):
    app = running_app
    human_id = _add(app)
    model_id = _add(app, "Agent callout", author="model")
    app.agent_review_store.resolve_comment(human_id, "already addressed")
    app.agent_review_store.reopen_comment(human_id)
    app.agent_review_store.update_anchors(lambda comment: {**comment.anchor, "x": 77})
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
    assert app.agent_review_notifications.ledger.read() == {}
    assert (await _notify(app, [model_id])).status_code == 409
    assert (await _notify(app, [999])).status_code == 409


async def test_active_notifications_coalesce_and_re_resolve_current_anchor_and_state(running_app):
    app = running_app
    first, second, resolved = [_add(app, str(i)) for i in range(3)]
    _active(app)
    response = await _notify(app, [first, second])
    first_ids = _body(response)["notification_ids"]
    assert _body(response)["status"] == "queued"
    assert _body(await _notify(app, [second]))["notification_ids"] == [first_ids[1]]
    assert _body(await _notify(app, [resolved]))["status"] == "queued"
    assert app.agent_session.submitted_turns == []
    requested_revision = app.agent_review_notifications.ledger.read()[str(first)]["requested_revision"]
    app.agent_review_store.update_anchors(lambda comment: {**comment.anchor, "x": 79})
    app.agent_review_store.resolve_comment(resolved)
    _finish(app)
    await app.agent_review_notifications.deliver()
    assert len(app.agent_session.submitted_turns) == 1
    text = app.agent_session.submitted_turns[0][0][-1]["text"]
    metadata = json.loads(text.split("\n", 1)[1])
    assert {item["comment_id"] for item in metadata} == {first, second}
    assert all(item["anchor"]["x"] == 79 for item in metadata)
    assert all(item["current_review_revision"] != requested_revision for item in metadata)
    assert app.agent_review_notifications.ledger.read()[str(resolved)]["status"] == "skipped"
    assert (await _notify(app, [resolved])).status_code == 409
    assert _body(await _notify(app, [first]))["status"] == "sent"
    assert app.agent_session.interrupted == 0


async def test_delete_while_queued_is_not_delivered(running_app, monkeypatch):
    app = running_app
    comment_id = _add(app)
    _active(app)
    assert _body(await _notify(app, [comment_id]))["status"] == "queued"
    original_list = app.agent_review_store.list_comments
    monkeypatch.setattr(app.agent_review_store, "list_comments", lambda: SimpleNamespace(comments=()))
    _finish(app)
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
    assert app.agent_review_notifications.ledger.read()[str(comment_id)]["status"] == "skipped"
    monkeypatch.setattr(app.agent_review_store, "list_comments", original_list)


async def test_sent_ids_remain_idempotent_across_reopen_and_new_session(running_app, served_dir):
    app = running_app
    comment_id = _add(app)
    original = _body(await _notify(app, [comment_id]))
    app.agent_review_store.resolve_comment(comment_id)
    app.agent_review_store.reopen_comment(comment_id)
    await app.agent_stop()
    sid = sessions.create_session(served_dir)
    reopened = _app(served_dir, session_id=sid, build_session=lambda on_event, **_: FakeSession(on_event, session_id=sid))
    try:
        response = await _notify(reopened, [comment_id])
        assert _body(response) == original
        assert reopened.agent_session.submitted_turns == []
        await reopened.agent_review_notifications.deliver()
        assert reopened.agent_session.submitted_turns == []
    finally:
        await reopened.agent_stop()


async def test_queued_intent_recovers_on_real_app_start_and_stops_with_holder(running_app, served_dir):
    app = running_app
    comment_id = _add(app)
    _active(app)
    queued = _body(await _notify(app, [comment_id]))
    sid = app.agent_holder.report()["session_id"]
    await app.agent_stop()
    reopened = _app(served_dir, session_id=sid, build_session=lambda on_event, **_: FakeSession(on_event, session_id=sid))
    try:
        await reopened.agent_start()
        for _ in range(100):
            if reopened.agent_session.submitted_turns:
                break
            await asyncio.sleep(0.01)
        assert len(reopened.agent_session.submitted_turns) == 1
        assert _body(await _notify(reopened, [comment_id]))["notification_ids"] == queued["notification_ids"]
        assert _turns(reopened)[-1]["review_notification_ids"] == queued["notification_ids"]
    finally:
        await reopened.agent_stop()
    assert reopened.agent_holder._tasks == []


async def test_failed_or_uncertain_dispatch_is_not_replayed(running_app, monkeypatch, served_dir):
    app = running_app
    comment_id = _add(app)

    async def fail(blocks, viewer=None):
        app.agent_session.submitted_turns.append((blocks, viewer))
        raise OSError("transport vanished after acceptance")

    monkeypatch.setattr(app.agent_session, "submit_turn", fail)
    response = await _notify(app, [comment_id])
    assert response.status_code == 409 and "uncertain" in _body(response)["error"]
    assert app.agent_review_store.get_comment(comment_id).text == "Please check this"
    await app.agent_review_notifications.deliver()
    assert (await _notify(app, [comment_id])).status_code == 409
    assert len(app.agent_session.submitted_turns) == 1
    await app.agent_stop()
    sid = sessions.create_session(served_dir)
    reopened = _app(served_dir, session_id=sid, build_session=lambda on_event, **_: FakeSession(on_event, session_id=sid))
    try:
        await reopened.agent_review_notifications.deliver()
        assert (await _notify(reopened, [comment_id])).status_code == 409
        assert reopened.agent_session.submitted_turns == []
    finally:
        await reopened.agent_stop()


async def test_crash_between_dispatch_fence_and_handoff_stays_uncertain(running_app):
    app = running_app
    comment_id = _add(app)
    _active(app)
    await _notify(app, [comment_id])
    app.agent_review_notifications.ledger.change(
        lambda records: records[str(comment_id)].update(status="dispatching")
    )
    _finish(app)
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
    response = await _notify(app, [comment_id])
    assert response.status_code == 409
    assert "uncertain" in _body(response)["error"]


async def test_synchronous_provider_rejection_is_not_reported_as_sent(running_app, monkeypatch):
    app = running_app
    comment_id = _add(app)

    async def reject(_blocks, viewer=None):
        app.agent_session.emit(InputState("notify-input", app.agent_bus.turn, "rejected", "not ready"))
        app.agent_session.emit(TurnEnd(turn=app.agent_bus.turn, stop_reason="rejected", cost_usd=None))

    monkeypatch.setattr(app.agent_session, "submit_turn", reject)
    response = await _notify(app, [comment_id])
    assert response.status_code == 409
    assert app.agent_review_notifications.ledger.read()[str(comment_id)]["status"] == "uncertain"


async def test_pause_host_recovery_and_manual_admission_do_not_consume_notes(running_app, monkeypatch):
    app = running_app
    comment_id = _add(app)
    app.agent_bus.queue_note("Keep until an actually admitted human turn")
    app.agent_bus.set_paused(True)
    assert (await _notify(app, [comment_id])).status_code == 409
    app.agent_bus.set_paused(False)
    app.agent_bus.review_notification_refusal = lambda: "Operator reconciliation is required"
    response = await _notify(app, [comment_id])
    assert response.status_code == 409 and "reconciliation" in _body(response)["error"]
    app.agent_bus.review_notification_refusal = lambda: None
    monkeypatch.setattr(app.agent_session, "turn_refusal", lambda: "input queue is full", raising=False)
    assert (await _notify(app, [comment_id])).status_code == 409
    assert app.agent_bus.turn == 0 and app.agent_review_notifications.ledger.read() == {}
    monkeypatch.setattr(app.agent_session, "turn_refusal", lambda: None)
    assert _body(await _notify(app, [comment_id]))["status"] == "sent"
    assert "Keep until an actually admitted" in app.agent_session.submitted_turns[0][0][0]["text"]


async def test_automation_and_pending_permissions_hold_queued_notification(running_app, monkeypatch):
    app = running_app
    comment_id = _add(app)
    _active(app)
    monkeypatch.setattr(app.agent_session, "automation_metadata", lambda: {"inflight": True}, raising=False)
    assert _body(await _notify(app, [comment_id]))["status"] == "queued"
    _finish(app)
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
    monkeypatch.setattr(app.agent_session, "automation_metadata", lambda: {"inflight": False})
    app.agent_bus.broker = SimpleNamespace(pending_requests=lambda: ["permission-card"], shutdown=lambda: None)
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
    app.agent_bus.broker = None
    await app.agent_review_notifications.deliver()
    assert len(app.agent_session.submitted_turns) == 1


async def test_notify_records_authenticated_sender_without_promoting_authority(served_dir):
    sid = sessions.create_session(served_dir)
    app = _app(served_dir, session_id=sid,
               build_session=lambda on_event, **_: FakeSession(on_event, session_id=sid),
               identity=TailscaleIdentity(["andrew@example.com"]))
    try:
        comment_id = _add(app)
        headers = {"Origin": "http://127.0.0.1:8765", "Tailscale-User-Login": "andrew@example.com",
                   "Tailscale-User-Name": "Andrew"}
        response = await _notify(app, [comment_id], headers=headers)
        assert response.status_code == 200
        event = _turns(app)[0]
        assert event["by"] == "andrew@example.com" and "automation_source" not in event
        ledger = app.agent_review_notifications.ledger.read()[str(comment_id)]
        assert ledger["sender"]["name"] == "Andrew"
        assert ledger["sender"]["claims"] is None
    finally:
        await app.agent_stop()


async def test_definitive_preadmission_refusal_is_actionable_and_explicitly_retryable(running_app, monkeypatch):
    import annealage_ember.http.ws as ws

    app = running_app
    comment_id = _add(app)
    calls = 0

    def refusal(_session, _bus):
        nonlocal calls
        calls += 1
        return "No idle slot is available" if calls == 3 else None

    monkeypatch.setattr(ws, "turn_refusal", refusal)
    response = await _notify(app, [comment_id])
    assert response.status_code == 409
    assert "No idle slot" in _body(response)["error"]
    assert app.agent_bus.turn == 0 and app.agent_session.submitted_turns == []
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
    response = await _notify(app, [comment_id])
    assert _body(response)["status"] == "sent"
    assert len(app.agent_review_store.list_comments().comments) == 1


async def test_corrupt_ledger_returns_actionable_failure_instead_of_resetting_identity(running_app):
    app = running_app
    comment_id = _add(app)
    _active(app)
    await _notify(app, [comment_id])
    path = app.agent_review_notifications.ledger.path
    data = json.loads(path.read_text())
    data["records"]["not-an-integer"] = data["records"].pop(str(comment_id))
    path.write_text(json.dumps(data))
    response = await _notify(app, [comment_id])
    assert response.status_code == 409
    assert "invalid record" in _body(response)["error"]
    assert json.loads(path.read_text()) == data
    assert app.agent_session.submitted_turns == []


async def test_viewer_only_notification_failure_does_not_resave_comment(served_dir):
    app = _app(served_dir)
    try:
        comment_id = _add(app)
        response = await _notify(app, [comment_id])
        assert response.status_code == 409 and "coordinator" in _body(response)["error"]
        assert len(app.agent_review_store.list_comments().comments) == 1
    finally:
        await app.agent_stop()


@pytest.mark.parametrize("ops", [("project.comment",), ("project.exec",)])
async def test_hosted_notify_requires_comment_and_execution_operations(served_dir, tmp_path_factory, ops):
    from test_hosted_session import _Verifier, _build, _headers
    from toy_product import toy_review_store

    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier(ops=ops),
                    review_store=toy_review_store(served_dir))
    try:
        comment_id = _add(hosted.app)
        response = await _notify(hosted.app, [comment_id], headers=_headers())
        assert response.status_code == 403
        assert hosted.app.agent_review_notifications.ledger.read() == {}
        assert hosted.sessions[0].submitted_turns == []
    finally:
        await hosted.app.agent_stop()


async def test_hosted_queued_delivery_rechecks_expiry_and_retry_refreshes_only_notification(served_dir, tmp_path_factory):
    from test_hosted_session import PRINCIPAL, _Verifier, _build, _headers
    from toy_product import toy_review_store

    hosted = _build(served_dir, tmp_path_factory,
                    verifier=_Verifier(ops=("project.comment", "project.exec")),
                    review_store=toy_review_store(served_dir))
    app = hosted.app
    try:
        comment_id = _add(app)
        _active(app)
        queued = _body(await _notify(app, [comment_id], headers=_headers()))
        assert queued["status"] == "queued"
        app.agent_review_notifications.ledger.change(
            lambda records: records[str(comment_id)]["sender"]["claims"].update(exp=1))
        _finish(app)
        await app.agent_review_notifications.deliver()
        assert hosted.sessions[0].submitted_turns == []
        ledger = app.agent_review_notifications.ledger.read()
        assert "expired" in ledger[str(comment_id)]["error"]
        sent = _body(await _notify(app, [comment_id], headers=_headers()))
        assert sent["status"] == "sent" and sent["notification_ids"] == queued["notification_ids"]
        assert len(app.agent_review_store.list_comments().comments) == 1
        event = _turns(app)[-1]
        assert event["principal_id"] == PRINCIPAL and "automation_source" not in event
        assert set(app.agent_bus.hosted_turn_ops) == {"project.comment", "project.exec"}
        assert app.agent_bus.hosted_turn_live
        raw = app.agent_review_notifications.ledger.path.read_text()
        assert "delegation" not in raw
    finally:
        await app.agent_stop()


async def _review_comments(app):
    response = await make_test_client(app).get("/review?t=" + TOKEN)
    assert response.status_code == 200
    return _body(response)["comments"]


async def test_delayed_delivery_failure_is_visible_in_review_and_explicit_retry_only(running_app, monkeypatch):
    import annealage_ember.review.notifications as notifications
    from annealage_ember.review import ReviewError

    app = running_app
    comment_id = _add(app)
    review_before = app.agent_review_store.path.read_bytes()
    _active(app)
    queued = _body(await _notify(app, [comment_id]))
    assert (await _review_comments(app))[0]["notification"] == {
        "id": queued["notification_ids"][0], "status": "queued"}
    snapshot = notifications._snapshot

    def unreadable_snapshot(_store):
        raise ReviewError("Cannot prepare current delivery metadata; repair and retry notification.")

    monkeypatch.setattr(notifications, "_snapshot", unreadable_snapshot)
    before_failure = app.agent_event_log.current_seq
    _finish(app)
    await app.agent_review_notifications.deliver()
    listed = (await _review_comments(app))[0]
    assert listed["text"] == "Please check this"
    assert listed["notification"]["status"] == "error"
    assert "repair and retry" in listed["notification"]["error"]
    assert listed["notification"]["id"] == queued["notification_ids"][0]
    assert any(wire["kind"] == "review_changed"
               for _seq, wire in app.agent_event_log.replay(before_failure).events)
    monkeypatch.setattr(notifications, "_snapshot", snapshot)
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []  # no silent retry of error
    sent = _body(await _notify(app, [comment_id]))
    assert sent["status"] == "sent" and sent["notification_ids"] == queued["notification_ids"]
    assert (await _review_comments(app))[0]["notification"]["status"] == "sent"
    assert app.agent_review_store.path.read_bytes() == review_before
    _finish(app)
    await app.agent_review_notifications.deliver()
    assert len(app.agent_session.submitted_turns) == 1


async def test_reconnected_review_reads_durable_status_without_waking_agent_or_replaying(running_app, served_dir, monkeypatch):
    app = running_app
    comment_id = _add(app)
    sent = _body(await _notify(app, [comment_id]))
    await app.agent_stop()
    sid = sessions.create_session(served_dir)
    reopened = _app(served_dir, session_id=sid,
                    build_session=lambda on_event, **_: FakeSession(on_event, session_id=sid))
    try:
        async def must_not_wake(**_kwargs):
            pytest.fail("GET /review woke the agent")

        monkeypatch.setattr(reopened.agent_holder, "ensure", must_not_wake)
        ledger_before = reopened.agent_review_notifications.ledger.path.read_bytes()
        seq_before = reopened.agent_event_log.current_seq
        for _ in range(2):
            listed = (await _review_comments(reopened))[0]
            assert listed["notification"] == {"id": sent["notification_ids"][0], "status": "sent"}
            assert set(listed["notification"]) == {"id", "status"}  # no claims or sender
        assert reopened.agent_event_log.current_seq == seq_before
        assert reopened.agent_review_notifications.ledger.path.read_bytes() == ledger_before
        assert reopened.agent_session.submitted_turns == []
    finally:
        await reopened.agent_stop()


@pytest.mark.parametrize("corruption", ["json", "key", "status"])
async def test_unreadable_outbox_never_hides_saved_review_comments(running_app, corruption):
    app = running_app
    comment_id = _add(app)
    _add(app, "An agent callout is still readable", author="model")
    _active(app)
    await _notify(app, [comment_id])
    path = app.agent_review_notifications.ledger.path
    data = json.loads(path.read_text())
    if corruption == "json":
        raw = "{not json"
    elif corruption == "key":
        data["records"]["bad"] = data["records"].pop(str(comment_id))
        raw = json.dumps(data)
    else:
        data["records"][str(comment_id)]["status"] = []
        raw = json.dumps(data)
    path.write_text(raw)
    review_before = app.agent_review_store.path.read_bytes()
    listed = await _review_comments(app)
    assert len(listed) == 2
    assert listed[0]["text"] == "Please check this"
    assert listed[0]["notification"]["status"] == "error"
    assert "saved comment is intact" in listed[0]["notification"]["error"]
    assert "notification" not in listed[1]
    assert path.read_text() == raw
    assert app.agent_review_store.path.read_bytes() == review_before
    assert app.agent_session.submitted_turns == []


async def test_crashed_dispatch_is_visible_as_uncertain_after_read_only_refetch(running_app):
    app = running_app
    comment_id = _add(app)
    _active(app)
    queued = _body(await _notify(app, [comment_id]))
    app.agent_review_notifications.ledger.change(
        lambda records: records[str(comment_id)].update(status="dispatching"))
    listed = (await _review_comments(app))[0]
    assert listed["notification"]["id"] == queued["notification_ids"][0]
    assert listed["notification"]["status"] == "uncertain"
    assert "will not be replayed" in listed["notification"]["error"]
    _finish(app)
    await app.agent_review_notifications.deliver()
    assert app.agent_session.submitted_turns == []
