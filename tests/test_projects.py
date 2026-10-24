"""The project routes (``projects.py``) over a fake store
(``fake_project_store.py``): what the page reads and posts, and above all that
a stale write is refused and says so.

The store is the authority for every compare-and-swap; these tests pin that
the routes never write without the etag the page read, carry the store's
refusal to the page in a form it can show (409 with ``conflict`` and the
current etag), and page history so that a commit landing between two pages is
detectable from the etag each page carries.
"""

import json

import pytest
from conftest import DEFAULT_PORT, TEST_HOST, make_test_client
from fake_project_store import FakeProjects
from toy_product import TOY_PAGE, register_toy_routes

from annealage_ember import app as agent_app
from annealage_ember import projects as pj

pytestmark = pytest.mark.asyncio

TOKEN = "projects-browser-token"


def _app(served_dir, *, directory=None, history=None, project_id=None, operations=None):
    def register(app, allowed_origins):
        register_toy_routes(app, allowed_origins)
        pj.register_project_routes(
            app,
            auth=app.agent_auth,
            directory=directory,
            history=history,
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


def _store(commits=0):
    store = FakeProjects()
    store.add("camera", "Camera board", **{"a.py": "1"})
    for n in range(commits):
        store.commit(
            "camera",
            "change %d" % n,
            author="Loom agent",
            actor="agent",
            turn=1 + n // 2,
            **{"a.py": str(n + 2)},
        )
    return store


def _body(res):
    return json.loads(res.body.decode("utf-8"))


def _get(client, path):
    return client.get(path + ("&" if "?" in path else "?") + "t=" + TOKEN)


def _post(client, path, body, *, token=TOKEN):
    return client.post(
        path + "?t=" + token, headers={"Content-Type": "application/json"}, body=json.dumps(body)
    )


@pytest.fixture
def both(served_dir):
    store = _store(commits=6)
    return store, _app(served_dir, directory=store, history=store)


@pytest.fixture
def bound(served_dir):
    store = _store(commits=6)
    return store, _app(served_dir, history=store, project_id="camera")


# --- the browser token, and nothing else -----------------------------------------------


async def test_every_route_is_refused_without_the_browser_token(both):
    store, client = both
    reads = ["/api/projects", "/api/projects/camera/history", "/api/projects/camera/history/c001"]
    posts = [
        "/api/projects",
        "/api/projects/camera/rename",
        "/api/projects/camera/restore",
        "/api/projects/camera/name",
    ]
    for path in reads:
        res = await client.get(path)
        assert (res.status_code, res.body) == (403, b"forbidden"), path
    for path in posts:
        res = await _post(client, path, {"expected_etag": "v1"}, token="wrong")
        assert (res.status_code, res.body) == (403, b"forbidden"), path
    assert store.calls == []


async def test_the_operations_are_the_callers_to_name(served_dir):
    seen = []

    class Auth:
        def authenticate(self, req, op=None):
            seen.append(op)
            return

    app = agent_app.create_app(
        served_dir,
        page_html=TOY_PAGE,
        port=DEFAULT_PORT,
        token=TOKEN,
        host=TEST_HOST,
        register_routes=lambda app, origins: pj.register_project_routes(
            app, auth=Auth(), directory=FakeProjects(), operations={"write": "project.admin"}
        ),
    )
    client = make_test_client(app)
    await client.get("/api/projects")
    await client.post("/api/projects", headers={"Content-Type": "application/json"}, body="{}")
    assert seen == ["project.read", "project.admin"]


# --- the directory ---------------------------------------------------------------------


async def test_the_list_carries_each_projects_etag_and_what_the_store_can_do(both):
    store, client = both
    body = _body(await _get(client, "/api/projects"))
    assert body["ok"]
    [entry] = body["projects"]
    assert entry["id"] == "camera" and entry["etag"] == store.etag("camera")
    assert entry["title"] == "Camera board" and entry["last_change"] == "change 5"
    assert body["capabilities"] == {
        "create": True,
        "rename_id": True,
        "title": True,
        "restore": True,
        "diff": True,
    }


async def test_a_project_is_started_and_a_taken_id_refused(both):
    store, client = both
    made = _body(
        await _post(client, "/api/projects", {"id": "lamp", "title": "Lamp", "brief": "a lamp"})
    )
    assert made["ok"] and made["project"]["id"] == "lamp"
    assert store.calls[-1] == ("create", "lamp", "Lamp", "a lamp", None)
    again = await _post(client, "/api/projects", {"id": "lamp"})
    assert again.status_code == 400 and "already" in _body(again)["error"]
    # A store that names its own projects takes no id.
    assert (
        _body(await _post(client, "/api/projects", {"title": "Auto"}))["project"]["id"] == "prj_1"
    )


@pytest.mark.parametrize("body", [{"id": 7}, {"id": "x", "title": []}, {"brief": "x" * 20_001}])
async def test_a_malformed_new_project_is_a_400(both, body):
    store, client = both
    res = await _post(client, "/api/projects", body)
    assert res.status_code == 400 and not _body(res)["ok"]
    assert [c for c in store.calls if c[0] == "create"] == []


async def test_what_the_store_does_not_do_is_a_404_that_says_so(served_dir):
    store = _store()
    store.capabilities = pj.Capabilities()
    client = _app(served_dir, directory=store, history=store, project_id="camera")
    for path, body in [
        ("/api/projects", {"id": "x"}),
        ("/api/restore", {"target": "turn:1", "expected_etag": "v1"}),
        ("/api/project/name", {"name": "x", "expected_etag": "v1"}),
    ]:
        res = await _post(client, path, body)
        assert res.status_code == 404 and _body(res)["unsupported"], path
    assert [c for c in store.calls if c[0] != "list"] == []


# --- history: paging -------------------------------------------------------------------


async def test_history_pages_newest_first_and_every_page_carries_the_etag(bound):
    store, client = bound
    seen, cursor, etags = [], None, set()
    while True:
        path = "/api/history?limit=3" + ("&cursor=" + cursor if cursor else "")
        body = _body(await _get(client, path))
        assert body["ok"] and body["project_id"] == "camera"
        etags.add(body["etag"])
        seen += [c["commit"] for c in body["commits"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ["c%03d" % n for n in range(6, -1, -1)]  # 7 commits, newest first
    assert etags == {store.etag("camera")}


async def test_history_groups_say_what_can_be_restored(bound):
    _, client = bound
    body = _body(await _get(client, "/api/history?limit=500"))
    groups = {g["key"]: g for g in body["groups"]}
    assert groups["turn:3"]["restore"] == {"target": "turn:3", "label": "Restore to before turn 3"}
    assert (
        groups["turn:0"]["restore"] is None and groups["turn:0"]["label"] == "Before the first turn"
    )
    first = body["commits"][0]
    assert first["group"] == "turn:3" and first["actor"] == "agent" and first["files"] == ["a.py"]
    assert body["head_label"] == "Turn now 3"


async def test_a_page_read_after_a_commit_has_a_new_etag_so_the_page_can_tell(bound):
    store, client = bound
    first = _body(await _get(client, "/api/history?limit=3"))
    store.commit("camera", "someone else's change", turn=4)
    second = _body(await _get(client, "/api/history?limit=3&cursor=" + first["next_cursor"]))
    assert second["etag"] != first["etag"]


async def test_limit_is_clamped_and_a_foreign_cursor_refused(bound):
    store, client = bound
    await _get(client, "/api/history?limit=100000")
    await _get(client, "/api/history?limit=zero")
    assert [c[2] for c in store.calls if c[0] == "history"] == [pj.MAX_LIMIT, pj.DEFAULT_LIMIT]
    res = await _get(client, "/api/history?cursor=not-a-number")
    assert res.status_code == 400
    res = await _get(client, "/api/history?cursor=" + "x" * 300)
    assert res.status_code == 400 and "cursor" in _body(res)["error"]


async def test_the_id_in_the_path_selects_the_project_and_an_unknown_one_is_a_404(both):
    store, client = both
    assert _body(await _get(client, "/api/projects/camera/history?limit=2"))["ok"]
    assert (await _get(client, "/api/projects/nothing/history")).status_code == 404
    diff = _body(await _get(client, "/api/projects/camera/history/c001"))
    assert diff["ok"] and diff["commit"] == "c001"
    assert (await _get(client, "/api/projects/camera/history/c999")).status_code == 404


async def test_a_bound_project_may_be_chosen_per_request(served_dir):
    store = _store()
    store.add("lamp", "Lamp")
    client = _app(served_dir, history=store, project_id=lambda req: "lamp")
    assert _body(await _get(client, "/api/history"))["project_id"] == "lamp"


# --- compare-and-swap, visible ---------------------------------------------------------


async def test_a_restore_without_the_etag_it_was_read_at_is_refused_unread(bound):
    store, client = bound
    for body in [
        {"target": "turn:1"},
        {"target": "turn:1", "expected_etag": ""},
        {"target": "turn:1", "expected_etag": 3},
        {"expected_etag": "v1"},
    ]:
        res = await _post(client, "/api/restore", body)
        assert res.status_code == 400 and "expected" in _body(res)["error"], body
    assert [c for c in store.calls if c[0] == "restore"] == []


async def test_a_restore_after_another_writer_is_a_conflict_the_page_can_show(bound):
    store, client = bound
    seen = _body(await _get(client, "/api/history"))["etag"]
    store.commit("camera", "someone else's change", turn=4, **{"b.py": "x"})
    tree_before = dict(store.projects["camera"]["tree"])
    res = await _post(client, "/api/restore", {"target": "turn:1", "expected_etag": seen})
    body = _body(res)
    assert res.status_code == 409
    assert body["ok"] is False and body["conflict"] is True
    assert body["current_etag"] == store.etag("camera") != seen
    assert "changed" in body["error"]
    # Nothing was restored: the project is as the other writer left it.
    assert store.projects["camera"]["tree"] == tree_before
    assert store.projects["camera"]["commits"][-1]["subject"] == "someone else's change"


async def test_a_title_and_an_id_change_are_compare_and_swap_too(both):
    store, client = both
    seen = store.etag("camera")
    store.commit("camera", "someone else's change")
    for path, body in [
        ("/api/projects/camera/name", {"name": "Mine", "expected_etag": seen}),
        ("/api/projects/camera/rename", {"id": "mine", "expected_etag": seen}),
    ]:
        res = await _post(client, path, body)
        assert res.status_code == 409 and _body(res)["conflict"], path
    assert store.projects["camera"]["title"] == "Camera board" and "mine" not in store.projects
    # On the etag it now has, the same changes are made.
    now = store.etag("camera")
    named = _body(
        await _post(client, "/api/projects/camera/name", {"name": "Mine", "expected_etag": now})
    )
    assert named["ok"] and named["name"] == "Mine" and named["etag"] == store.etag("camera") != now
    moved = _body(
        await _post(
            client, "/api/projects/camera/rename", {"id": "mine", "expected_etag": named["etag"]}
        )
    )
    assert moved["ok"] and moved["project"]["id"] == "mine" and "camera" not in store.projects


# --- restore ---------------------------------------------------------------------------


async def test_a_restore_is_a_new_commit_and_moves_the_etag(bound):
    store, client = bound
    seen = _body(await _get(client, "/api/history"))["etag"]
    res = await _post(client, "/api/restore", {"target": "turn:2", "expected_etag": seen})
    body = _body(res)
    assert res.status_code == 200 and body["ok"] and body["noop"] is False
    assert body["commit"] == store._head("camera") and body["changes"] == ["a.py"]
    assert body["etag"] == store.etag("camera") != seen
    assert store.projects["camera"]["tree"]["a.py"] == "3"  # as it was before turn 2 began
    # The history it was made from is kept: restore adds, never rewrites.
    assert len(store.projects["camera"]["commits"]) == 8
    page = _body(await _get(client, "/api/history"))
    assert page["commits"][0]["subject"] == "restore: to before turn 2"


async def test_a_restore_that_changes_nothing_says_so_and_leaves_the_etag(bound):
    store, client = bound
    seen = store.etag("camera")
    body = _body(await _post(client, "/api/restore", {"target": "turn:9", "expected_etag": seen}))
    assert body["ok"] and body["noop"] is True and body["commit"] is None and body["etag"] == seen


async def test_a_restore_to_nothing_history_offered_is_refused(bound):
    store, client = bound
    res = await _post(
        client, "/api/restore", {"target": "HEAD~3", "expected_etag": store.etag("camera")}
    )
    assert res.status_code == 400 and not _body(res)["ok"]


async def test_a_busy_store_refuses_and_the_page_is_told_it_is_busy(bound):
    store, client = bound
    store.busy = True
    res = await _post(
        client, "/api/restore", {"target": "turn:1", "expected_etag": store.etag("camera")}
    )
    body = _body(res)
    assert res.status_code == 409 and body["busy"] is True and "conflict" not in body
    assert "working" in body["error"]


async def test_a_store_failure_is_a_500_with_its_message(served_dir):
    class Broken(FakeProjects):
        async def history(self, *args, **kwargs):
            raise pj.StoreFailed("the repository is unreadable")

    store = Broken()
    store.add("camera")
    client = _app(served_dir, history=store, project_id="camera")
    res = await _get(client, "/api/history")
    assert res.status_code == 500 and _body(res)["error"] == "the repository is unreadable"
