"""The optional send to / derived from routes (``projects.py``,
``ProjectLineage``) over ``FakeLineage``: absent unless the host registers a
lineage, one read for the options and both directions of lineage, a send that
carries the revision it was made from and refuses without one, and the refusals
the page shows (403 with a message that is not the authentication refusal, 409
busy or conflict, a viewer who may look but not send).
"""

import json

import pytest
from conftest import DEFAULT_PORT, TEST_HOST, make_test_client
from fake_project_store import FakeLineage, FakeProjects
from toy_product import TOY_PAGE, register_toy_routes

from annealage_ember import app as agent_app
from annealage_ember import projects as pj

pytestmark = pytest.mark.asyncio

TOKEN = "lineage-browser-token"


def _app(
    served_dir, *, lineage=None, history=None, project_id="camera", operations=None, auth=None
):
    def register(app, allowed_origins):
        register_toy_routes(app, allowed_origins)
        pj.register_project_routes(
            app,
            auth=auth or app.agent_auth,
            history=history,
            lineage=lineage,
            project_id=project_id,
            operations=operations,
        )

    return make_test_client(
        agent_app.create_app(
            served_dir,
            page_html=TOY_PAGE,
            port=DEFAULT_PORT,
            token=TOKEN,
            host=TEST_HOST,
            register_routes=register,
        )
    )


def _stores(**kwargs):
    store = FakeProjects()
    store.add("camera", "Camera board", **{"a.py": "1"})
    store.commit("camera", "change 1", author="Loom agent", actor="agent", turn=1, **{"a.py": "2"})
    return store, FakeLineage(store, **kwargs)


def _body(res):
    return json.loads(res.body.decode("utf-8"))


def _get(client, path):
    return client.get(path + "?t=" + TOKEN)


def _post(client, path, body, *, token=TOKEN):
    return client.post(
        path + "?t=" + token, headers={"Content-Type": "application/json"}, body=json.dumps(body)
    )


SEND = {
    "revision": "c001",
    "target_product": "trace",
    "target_project": None,
    "intent": "review it",
}


@pytest.fixture
def hosted(served_dir):
    store, lineage = _stores()
    return store, lineage, _app(served_dir, lineage=lineage, history=store)


# --- absent unless the host has it -----------------------------------------------------


async def test_a_host_without_lineage_has_no_lineage_routes(served_dir):
    store, _ = _stores()
    client = _app(served_dir, history=store)
    for res in (await _get(client, "/api/lineage"), await _post(client, "/api/send", SEND)):
        assert res.status_code == 404
        # Not a store's answer: no JSON, which is how the page tells "no lineage" from a
        # project that does not exist.
        with pytest.raises(ValueError):
            _body(res)


async def test_lineage_alone_registers_without_a_history_store(served_dir):
    _, lineage = _stores()
    client = _app(served_dir, lineage=lineage)
    assert _body(await _get(client, "/api/lineage"))["ok"]


async def test_a_store_that_does_not_send_says_unsupported(served_dir):
    class NoSend:
        async def send_options(self, project_id, *, actor):
            raise pj.Unsupported("this project store does not do that")

        async def lineage(self, project_id, *, actor):
            raise AssertionError("not reached")

    res = await _get(_app(served_dir, lineage=NoSend()), "/api/lineage")
    assert res.status_code == 404 and _body(res)["unsupported"]


async def test_the_routes_are_refused_without_the_browser_token(hosted):
    _, lineage, client = hosted
    assert (await client.get("/api/lineage")).status_code == 403
    res = await _post(client, "/api/send", SEND, token="wrong")
    assert (res.status_code, res.body) == (403, b"forbidden")
    assert lineage.calls == [] and lineage.derived == []


async def test_a_send_is_the_callers_to_name_and_defaults_to_write(served_dir):
    seen = []

    class Auth:
        def authenticate(self, req, op=None):
            seen.append(op)
            return

    _, lineage = _stores()
    for operations in (None, {"derive": "project.exec"}):
        seen.clear()
        client = _app(served_dir, lineage=lineage, auth=Auth(), operations=operations)
        await client.get("/api/lineage")
        await client.post("/api/send", headers={"Content-Type": "application/json"}, body="{}")
        assert seen == ["project.read", operations["derive"] if operations else "project.write"]


# --- reading -----------------------------------------------------------------------------


async def test_the_read_carries_the_options_and_both_directions(hosted):
    _, lineage, client = hosted
    await _post(client, "/api/send", SEND)
    body = _body(await _get(client, "/api/lineage"))
    assert body["ok"] and body["project_id"] == "camera"
    assert body["can_send"] is True and body["disabled_reason"] == ""
    assert body["head_revision"] == "c001"
    assert (body["send_label"], body["derived_label"], body["sent_label"]) == (
        "Send to",
        "Derived from",
        "Sent to",
    )
    assert [(t["product"], t["label"]) for t in body["targets"]] == [
        ("trace", "Trace"),
        ("mesh", "Mesh"),
    ]
    trace = body["targets"][0]
    assert trace["can_create"] is True and trace["new_label"] == "A new Trace project"
    assert trace["existing"] == [
        {"id": "review", "title": "Rescue review", "summary": "3 findings"}
    ]
    [origin] = body["derived_from"]
    assert (origin["project"], origin["revision"], origin["local_revision"]) == (
        "boards",
        "b042",
        "c000",
    )
    assert origin["relation_label"] == "rescued from" and origin["href"] == "/p/boards/"
    [sent] = body["sent_to"]
    assert (sent["project"], sent["local_revision"], sent["intent"]) == (
        "prj_1",
        "c001",
        "review it",
    )
    assert sent["product_label"] == "Trace" and sent["href"] == "/p/prj_1/"


async def test_a_viewer_is_told_why_they_cannot_send(served_dir):
    store, lineage = _stores(viewer=True)
    body = _body(await _get(_app(served_dir, lineage=lineage), "/api/lineage"))
    assert body["ok"] and body["can_send"] is False
    assert "cannot send" in body["disabled_reason"]
    assert body["targets"], "a viewer still sees what exists, disabled"


async def test_the_path_form_names_the_project_in_the_path(served_dir):
    store, lineage = _stores()
    client = _app(served_dir, lineage=lineage, project_id=None)
    assert _body(await _get(client, "/api/projects/camera/lineage"))["project_id"] == "camera"
    assert (await _get(client, "/api/projects/nothing/lineage")).status_code == 404
    assert (await _post(client, "/api/projects/camera/send", SEND)).status_code == 200
    assert lineage.calls[-1][:3] == ("derive", "camera", "c001")


# --- sending ---------------------------------------------------------------------------


async def test_a_send_names_the_revision_and_the_target_and_reports_where_it_went(hosted):
    _, lineage, client = hosted
    res = await _post(client, "/api/send", SEND)
    assert res.status_code == 200
    derivation = _body(res)["derivation"]
    assert derivation["target_project"] == "prj_1" and derivation["new"] is True
    assert derivation["created"] is True and derivation["href"] == "/p/prj_1/"
    assert lineage.calls[-1] == ("derive", "camera", "c001", "trace", None, "review it")
    # An identical request is the existing one, which the page says.
    again = _body(await _post(client, "/api/send", SEND))["derivation"]
    assert again["created"] is False and again["job_id"] == derivation["job_id"]
    existing = _body(await _post(client, "/api/send", {**SEND, "target_project": "review"}))[
        "derivation"
    ]
    assert existing["new"] is False and existing["target_project"] == "review"


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in SEND.items() if k != "revision"},
        {**SEND, "revision": ""},
        {**SEND, "revision": 7},
        {k: v for k, v in SEND.items() if k != "target_product"},
        {**SEND, "target_project": 3},
        {**SEND, "target_project": ""},
        {**SEND, "intent": 4},
        {**SEND, "intent": "x" * 2_001},
    ],
)
async def test_a_malformed_send_is_a_400_and_reaches_nothing(hosted, body):
    _, lineage, client = hosted
    res = await _post(client, "/api/send", body)
    assert res.status_code == 400 and not _body(res)["ok"]
    assert lineage.derived == [] and [c for c in lineage.calls if c[0] == "derive"] == []


async def test_a_revision_the_host_does_not_know_is_its_400(hosted):
    _, _, client = hosted
    res = await _post(client, "/api/send", {**SEND, "revision": "zzz"})
    assert res.status_code == 400 and "not a revision" in _body(res)["error"]


async def test_a_refusal_is_a_403_the_page_can_tell_from_a_stale_link(served_dir):
    store, lineage = _stores(viewer=True)
    res = await _post(_app(served_dir, lineage=lineage), "/api/send", SEND)
    body = _body(res)
    assert res.status_code == 403 and body["forbidden"] is True
    assert body["error"] == "You may not send this project."
    assert lineage.derived == []


@pytest.mark.parametrize(
    ("error", "flags"),
    [
        (pj.Busy("the target is being built; try again when it is done"), {"busy": True}),
        (
            pj.Conflict("the target moved on", current_etag="v3"),
            {"conflict": True, "current_etag": "v3"},
        ),
    ],
)
async def test_a_target_that_is_held_or_moved_on_is_a_409_with_its_words(hosted, error, flags):
    _, lineage, client = hosted
    lineage.fail = error
    res = await _post(client, "/api/send", SEND)
    body = _body(res)
    assert res.status_code == 409 and body["error"] == str(error)
    assert {k: body[k] for k in flags} == flags
    assert lineage.derived == []
