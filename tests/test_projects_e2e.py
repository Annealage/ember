"""The project list, title bar and history (``static/project.js``, ``project.css``)
in a real Chromium, against the real routes (``projects.py``) over the fake
store (``fake_project_store.py``), served by the real app on a loopback socket.

What only a browser shows: the history really pages and groups, Restore asks
first and its dialog closes with Escape, and a change the store refuses as
stale stays on the page, with the edit the person made kept in it, instead of
a toast that fades. Skipped without Playwright and a Chromium binary; run for
real with:

    uv run --with playwright --with pytest --with pytest-asyncio \\
        python -m pytest tests/test_projects_e2e.py -q
"""

import asyncio
import glob
import os
import socket
import threading

import pytest

pytest.importorskip("playwright.sync_api")

from fake_project_store import FakeLineage, FakeProjects  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from toy_product import TOY_PAGE, register_toy_routes  # noqa: E402

from annealage_ember import app as agent_app  # noqa: E402
from annealage_ember import projects as pj  # noqa: E402


def _find_chrome():
    env = os.environ.get("EMBER_E2E_CHROME")
    if env:
        return env
    matches = sorted(
        glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    )
    return matches[-1] if matches else None


CHROME = _find_chrome()
if not CHROME:
    pytest.skip("no chromium binary found for playwright", allow_module_level=True)

TOKEN = "projects-e2e-token"

DEMO_PAGE = """<!doctype html>
<meta charset="utf-8"><title>projects</title>
<link rel="stylesheet" href="/agent/static/project.css">
<div id="toast" hidden></div>
<div id="header"></div>
<div id="history"></div>
<div id="list"></div>
<div id="lineage"></div>
<script type="module" src="/demo.js"></script>
"""

DEMO_SCRIPT = """
import { createProjectClient, createProjectContext, initProjectHeader, initProjectHistory,
  initProjectLineage, initProjectList } from "/agent/static/project.js";
const ctx = createProjectContext({ client: createProjectClient() });
const lineage = initProjectLineage({ ctx, root: document.getElementById("lineage") });
const history = initProjectHistory({ ctx, root: document.getElementById("history"), pageSize: 3,
  lineage });
const header = initProjectHeader({ ctx, root: document.getElementById("header"),
  title: "Camera board", projectId: "camera", idChange: { warning: "The address changes." } });
const list = initProjectList({ ctx, root: document.getElementById("list") });
window.demo = { ctx, history, header, list, lineage };
await Promise.all([history.refresh(), list.refresh(), lineage.refresh()]);
document.body.dataset.ready = "1";
"""


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """The app on its own loop in a thread: the browser's requests cannot be
    served by the loop the test thread is blocked in."""

    def __init__(self, store, serve_dir, lineage=None):
        self.store = store
        self.port = _free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self._ready = threading.Event()
        self._loop = None

        def register(app, allowed_origins):
            register_toy_routes(app, allowed_origins)
            from annealage_ember.http import Response

            @app.get("/demo")
            async def demo(req):
                return Response(DEMO_PAGE, headers={"Content-Type": "text/html; charset=utf-8"})

            @app.get("/demo.js")
            async def demo_js(req):
                return Response(
                    DEMO_SCRIPT, headers={"Content-Type": "text/javascript; charset=utf-8"}
                )

            pj.register_project_routes(
                app,
                auth=app.agent_auth,
                directory=store,
                history=store,
                lineage=lineage,
                project_id="camera",
            )

        self.app = agent_app.create_app(
            serve_dir,
            page_html=TOY_PAGE,
            port=self.port,
            token=TOKEN,
            host="127.0.0.1",
            register_routes=register,
        )

    def start(self):
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._task = loop.create_task(
                agent_app.serve(self.app, "127.0.0.1", self.port, on_ready=self._ready.set)
            )
            self._task.add_done_callback(lambda t: loop.stop())
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "the app did not start"

    def stop(self):
        try:
            self._loop.call_soon_threadsafe(self._task.cancel)
        except RuntimeError:
            return
        self._thread.join(timeout=5)


@pytest.fixture
def store():
    s = FakeProjects()
    s.add("camera", "Camera board", **{"a.py": "1"})
    for n in range(6):
        s.commit(
            "camera",
            "change %d" % (n + 1),
            author="Loom agent",
            actor="agent",
            turn=1 + n // 2,
            **{"a.py": str(n + 2)},
        )
    s.add("lamp", "Lamp")
    return s


def _open_page(store, tmp_path, lineage):
    server = _Server(store, tmp_path, lineage)
    server.start()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        context = browser.new_context()
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # A 4xx the page was meant to see is logged by Chromium too; a script
        # error or a CSP violation is what this is for.
        page.on(
            "console",
            lambda m: (
                errors.append(m.text)
                if m.type == "error" and not m.text.startswith("Failed to load resource")
                else None
            ),
        )
        page.goto(server.base + "/demo#t=" + TOKEN)
        page.wait_for_selector("body[data-ready='1']")
        yield page
        assert errors == [], errors
        browser.close()
    server.stop()


@pytest.fixture
def page(store, tmp_path):
    """The host Canvas is today: no lineage, so no send to / derived from."""
    yield from _open_page(store, tmp_path, None)


@pytest.fixture
def lineage(store, request):
    return FakeLineage(store, **getattr(request, "param", {}))


@pytest.fixture
def lpage(store, tmp_path, lineage):
    yield from _open_page(store, tmp_path, lineage)


def _commits(page):
    return page.locator("#history .pj-commit .pj-subject").all_text_contents()


# --- history ---------------------------------------------------------------------------


def test_history_groups_and_pages_back(page):
    assert _commits(page) == ["change 6", "change 5", "change 4"]
    assert page.locator("#history .pj-now").text_content() == "Turn now 3"
    labels = page.locator("#history .pj-grouplabel").all_text_contents()
    assert labels == ["Turn 3", "Turn 2"]
    more = page.locator("#history .pj-more")
    more.click()
    expect(page.locator("#history .pj-commit")).to_have_count(6)
    more.click()
    expect(page.locator("#history .pj-commit")).to_have_count(7)
    expect(more).to_be_hidden()
    assert page.locator("#history .pj-grouplabel").all_text_contents() == [
        "Turn 3",
        "Turn 2",
        "Turn 1",
        "Before the first turn",
    ]
    # A group with nothing to restore to has no button.
    assert (
        page.locator("#history .pj-group").last.locator("button", has_text="Restore").count() == 0
    )


def test_a_diff_opens_in_place(page):
    page.locator("#history .pj-commit").first.get_by_role("button", name="Diff").click()
    expect(page.locator("#history pre.pj-diff")).to_contain_text("+++ a.py")


# --- restore ---------------------------------------------------------------------------


def test_restore_asks_first_and_escape_leaves_the_project_alone(page, store):
    page.get_by_role("button", name="Restore to before turn 2").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("This undoes 3 commits or more")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert [c for c in store.calls if c[0] == "restore"] == []


def test_restore_puts_the_project_back_as_a_new_commit(page, store):
    page.get_by_role("button", name="Restore to before turn 3").click()
    page.get_by_role("dialog").get_by_role("button", name="Restore to before turn 3").click()
    expect(page.get_by_role("dialog")).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("Restore to before turn 3: commit c007")
    expect(page.locator("#history .pj-commit .pj-subject").first).to_have_text(
        "restore: to before turn 3"
    )
    assert store.projects["camera"]["tree"]["a.py"] == "5"
    assert len(store.projects["camera"]["commits"]) == 8


def test_a_restore_refused_as_stale_stays_visible_until_reloaded(page, store):
    page.get_by_role("button", name="Restore to before turn 3").click()
    store.commit("camera", "someone else's change", turn=4)
    page.get_by_role("dialog").get_by_role("button", name="Restore to before turn 3").click()
    banner = page.locator("#history .pj-conflict")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Not saved: the project changed since you started")
    expect(page.get_by_role("dialog")).to_be_hidden()
    assert [c for c in store.calls if c[0] == "restore"] != []
    assert not any(c["subject"].startswith("restore") for c in store.projects["camera"]["commits"])
    # It is not a toast: it is still there after the toast would have faded.
    page.wait_for_timeout(3800)
    expect(banner).to_be_visible()
    banner.get_by_role("button", name="Reload to see what changed").click()
    expect(page.locator("#history .pj-commit .pj-subject").first).to_have_text(
        "someone else's change"
    )
    expect(banner).to_contain_text("Reloaded")
    banner.get_by_role("button", name="Dismiss").click()
    expect(banner).to_have_count(0)


def test_a_dialog_the_project_moved_under_cannot_be_confirmed(page, store):
    page.get_by_role("button", name="Restore to before turn 3").click()
    store.commit("camera", "someone else's change", turn=4)
    page.evaluate("demo.history.refresh()")  # what a build event does while the dialog is open
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("The project changed after you opened this")
    expect(dialog.get_by_role("button", name="Restore to before turn 3")).to_be_disabled()


# --- title -----------------------------------------------------------------------------


def test_a_title_is_renamed_with_enter_and_escape_puts_it_back(page, store):
    page.get_by_role("button", name="Rename").click()
    field = page.locator("#header").get_by_label("Project title")
    field.fill("Throwaway")
    field.press("Escape")
    expect(page.locator("#header .pj-title")).to_have_text("Camera board")
    page.get_by_role("button", name="Rename").click()
    field = page.locator("#header").get_by_label("Project title")
    field.fill("Camera v2")
    field.press("Enter")
    expect(page.locator("#header .pj-title")).to_have_text("Camera v2")
    assert store.projects["camera"]["title"] == "Camera v2"
    expect(page.locator("#toast")).to_contain_text("Renamed: commit")


def test_a_title_refused_as_stale_keeps_what_was_typed(page, store):
    page.get_by_role("button", name="Rename").click()
    field = page.locator("#header").get_by_label("Project title")
    field.fill("Mine")
    store.commit("camera", "someone else's change", turn=4)
    field.press("Enter")
    banner = page.locator("#header .pj-conflict")
    expect(banner).to_contain_text('Your new title, "Mine" is kept')
    assert store.projects["camera"]["title"] == "Camera board"
    # Saving is offered only once the person has reloaded, then it is theirs to click.
    expect(banner.get_by_role("button", name="Save my edit on the latest")).to_have_count(0)
    banner.get_by_role("button", name="Reload to see what changed").click()
    expect(page.locator("#history .pj-commit .pj-subject").first).to_have_text(
        "someone else's change"
    )
    banner.get_by_role("button", name="Save my edit on the latest").click()
    expect(page.locator("#header .pj-title")).to_have_text("Mine")
    assert store.projects["camera"]["title"] == "Mine"
    expect(banner).to_have_count(0)


def test_changing_the_id_asks_what_it_costs_first(page, store):
    page.get_by_role("button", name="Change id").first.click()
    field = page.locator("#header").get_by_label("Project id")
    field.fill("cam2")
    field.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("The address changes.")
    dialog.get_by_role("button", name="Cancel").click()
    assert "camera" in store.projects and "cam2" not in store.projects
    page.get_by_role("button", name="Change id").first.click()
    field = page.locator("#header").get_by_label("Project id")
    field.fill("cam2")
    field.press("Enter")
    page.get_by_role("dialog").get_by_role("button", name="Change id").click()
    expect(page.locator("#list .pj-card[data-project='cam2']")).to_be_visible()
    assert "cam2" in store.projects and "camera" not in store.projects


# --- list ------------------------------------------------------------------------------


def test_the_list_starts_a_project_and_reports_a_refusal(page, store):
    cards = page.locator("#list .pj-card")
    expect(cards).to_have_count(2)
    form = page.locator("#list form")
    form.get_by_label("New project id").fill("lamp")
    form.get_by_role("button", name="Start a project").click()
    expect(form).to_contain_text("there is already a project called lamp")
    form.get_by_label("New project id").fill("clock")
    form.get_by_label("New project title").fill("Clock")
    form.get_by_role("button", name="Start a project").click()
    expect(cards).to_have_count(3)
    expect(page.locator("#list .pj-card[data-project='clock']")).to_contain_text("Clock")


def test_an_id_change_refused_as_stale_stays_on_its_row(page, store):
    row = page.locator("#list .pj-card[data-project='lamp']")
    row.get_by_role("button", name="Change id").click()
    field = row.get_by_label("Project id")
    field.fill("lamp2")
    store.commit("lamp", "someone else's change")
    field.press("Enter")
    banner = row.locator(".pj-conflict")
    expect(banner).to_contain_text('Your new id, "lamp2"')
    assert "lamp" in store.projects
    banner.get_by_role("button", name="Reload to see what changed").click()
    banner.get_by_role("button", name="Save my edit on the latest").click()
    expect(page.locator("#list .pj-card[data-project='lamp2']")).to_be_visible()
    assert "lamp2" in store.projects


# --- send to / derived from ------------------------------------------------------------


def test_a_host_without_lineage_shows_no_send_and_no_lineage(page):
    expect(page.locator("#lineage")).to_be_hidden()
    assert page.locator("#lineage").inner_text() == ""
    assert page.locator("[data-send], .pj-send").count() == 0
    assert _commits(page) == ["change 6", "change 5", "change 4"]


def test_the_lineage_lists_show_both_directions_as_links(lpage):
    box = lpage.locator("#lineage")
    expect(box).to_be_visible()
    derived = box.locator(".pj-derived")
    expect(derived.locator("h4")).to_have_text("Derived from")
    link = derived.locator("a", has_text="Reference boards")
    assert link.get_attribute("href") == "/p/boards/"
    expect(derived).to_contain_text("Loom")
    expect(derived).to_contain_text("rescued from")
    expect(derived).to_contain_text("b042 → c000")
    expect(derived).to_contain_text("start from the reviewed boards")
    sent = box.locator(".pj-sent")
    expect(sent.locator("h4")).to_have_text("Sent to")
    expect(sent).to_contain_text("Nothing yet.")


def test_a_send_chooses_a_product_and_a_target_and_lands_in_the_list(lpage, lineage):
    button = lpage.locator("#lineage .pj-send")
    expect(button).to_be_enabled()
    button.click()
    items = lpage.locator("#lineage [role='menuitem']")
    assert items.all_text_contents() == ["Trace", "Mesh"]
    items.first.click()
    dialog = lpage.get_by_role("dialog")
    expect(dialog).to_contain_text("Send to Trace")
    expect(dialog).to_contain_text("Revision c006")
    radios = dialog.get_by_role("radio")
    assert radios.count() == 2
    expect(radios.first).to_be_checked()
    expect(dialog).to_contain_text("A new Trace project")
    expect(dialog).to_contain_text("Rescue review")
    dialog.get_by_label("Why you are sending it (optional)").fill("check the rails")
    dialog.get_by_role("button", name="Send to Trace").click()
    expect(dialog).to_be_hidden()
    result = lpage.locator("#lineage .pj-result")
    expect(result).to_contain_text("Sent to Trace: Camera board review")
    assert result.locator("a").get_attribute("href") == "/p/prj_1/"
    expect(lpage.locator("#toast")).to_contain_text("Sent to Trace")
    sent = lpage.locator("#lineage .pj-sent")
    expect(sent.locator(".pj-link")).to_have_count(1)
    expect(sent).to_contain_text("c006 → r001")
    expect(sent).to_contain_text("check the rails")
    [derive] = [c for c in lineage.calls if c[0] == "derive"]
    assert derive == ("derive", "camera", "c006", "trace", None, "check the rails")


def test_a_send_can_extend_an_existing_project(lpage, lineage):
    lpage.locator("#lineage .pj-send").click()
    lpage.locator("#lineage [role='menuitem']").first.click()
    dialog = lpage.get_by_role("dialog")
    dialog.get_by_role("radio").nth(1).check()
    dialog.get_by_role("button", name="Send to Trace").click()
    expect(lpage.locator("#lineage .pj-result")).to_contain_text("Sent to Trace: Rescue review")
    assert [c for c in lineage.calls if c[0] == "derive"][0][4] == "review"


def test_a_commit_of_the_history_is_the_revision_sent(lpage, lineage):
    lpage.locator("#history [data-send='c004']").click()
    expect(lpage.locator("#lineage .pj-revnote")).to_contain_text("revision c004")
    lpage.locator("#lineage [role='menuitem']").first.click()
    dialog = lpage.get_by_role("dialog")
    expect(dialog).to_contain_text("Revision c004")
    lpage.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert lineage.derived == []
    # "Use the latest" puts the project as it is now back.
    lpage.locator("#lineage .pj-revnote").get_by_role("button", name="Use the latest").click()
    expect(lpage.locator("#lineage .pj-revnote")).to_be_hidden()


@pytest.mark.parametrize("lineage", [{"viewer": True}], indirect=True)
def test_someone_who_may_not_send_sees_it_disabled_and_why(lpage, lineage):
    expect(lpage.locator("#lineage .pj-send")).to_be_disabled()
    expect(lpage.locator("#lineage .pj-notice")).to_have_text(
        "Viewers can look at this project but cannot send it."
    )
    assert lpage.locator("[data-send]").count() == 0
    expect(lpage.locator("#lineage .pj-derived")).to_contain_text("Reference boards")
    assert [c for c in lineage.calls if c[0] == "derive"] == []


def _open_dialog(page):
    page.locator("#lineage .pj-send").click()
    page.locator("#lineage [role='menuitem']").first.click()
    return page.get_by_role("dialog")


def test_a_refusal_from_the_host_is_shown_in_the_dialog(lpage, lineage):
    lineage.fail = pj.Forbidden("You may not send this project to that workspace.")
    dialog = _open_dialog(lpage)
    dialog.get_by_role("button", name="Send to Trace").click()
    expect(dialog).to_contain_text("You may not send this project to that workspace.")
    # It is the host's answer, not the stale-link message.
    expect(dialog).not_to_contain_text("link has gone stale")
    expect(dialog.get_by_role("button", name="Send to Trace")).to_be_enabled()
    assert lineage.derived == []


def test_a_send_that_conflicts_stays_visible_and_is_only_sent_again_by_the_person(lpage, lineage):
    lineage.fail = pj.Conflict("the target moved on while you were choosing", current_etag="v9")
    dialog = _open_dialog(lpage)
    dialog.get_by_label("Why you are sending it (optional)").fill("kept words")
    dialog.get_by_role("button", name="Send to Trace").click()
    expect(dialog).to_be_hidden()
    banner = lpage.locator("#lineage .pj-conflict")
    expect(banner).to_contain_text("Not sent: it changed since you started.")
    expect(banner).to_contain_text("the target moved on while you were choosing")
    expect(banner).to_contain_text("Nothing was sent to Trace, and your request is kept.")
    # Not a toast: still there after one would have faded, and nothing retried behind the person.
    lpage.wait_for_timeout(3800)
    expect(banner).to_be_visible()
    assert len([c for c in lineage.calls if c[0] == "derive"]) == 1
    assert banner.get_by_role("button", name="Review and send again").count() == 0
    lineage.fail = None
    banner.get_by_role("button", name="Reload to see what changed").click()
    expect(banner).to_contain_text("Reloaded")
    assert len([c for c in lineage.calls if c[0] == "derive"]) == 1
    banner.get_by_role("button", name="Review and send again").click()
    expect(dialog).to_be_visible()
    assert dialog.get_by_label("Why you are sending it (optional)").input_value() == "kept words"
    dialog.get_by_role("button", name="Send to Trace").click()
    expect(lpage.locator("#lineage .pj-result")).to_contain_text("Sent to Trace")
    expect(banner).to_have_count(0)
    assert len([c for c in lineage.calls if c[0] == "derive"]) == 2


def test_a_busy_target_can_be_discarded(lpage, lineage):
    lineage.fail = pj.Busy("the target is being built; try again when it is done")
    dialog = _open_dialog(lpage)
    dialog.get_by_role("button", name="Send to Trace").click()
    banner = lpage.locator("#lineage .pj-conflict")
    expect(banner).to_contain_text("Not sent: it is busy.")
    expect(banner).to_contain_text("the target is being built")
    banner.get_by_role("button", name="Discard my request").click()
    expect(banner).to_have_count(0)
    assert lineage.derived == []


def test_a_product_that_is_unavailable_is_listed_but_cannot_be_chosen(lpage, lineage):
    lineage.unavailable = "Mesh is not set up for this workspace."
    lpage.evaluate("demo.lineage.refresh()")
    lpage.locator("#lineage .pj-send").click()
    mesh = lpage.locator("#lineage [role='menuitem']", has_text="Mesh")
    expect(mesh).to_be_disabled()
    assert mesh.get_attribute("title") == "Mesh is not set up for this workspace."
