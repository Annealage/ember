// The page's project client and models (static/project.js) under Node, against
// a store written here to the same wire as projects.py's routes. The DOM-free
// models are what carry the behaviour; the views over them are driven in a real
// browser by test_projects_e2e.py. Run one case: node project_cases.mjs <name>;
// every case: node project_cases.mjs --list.
import assert from "node:assert/strict";

globalThis.window = globalThis;
globalThis.location = { hash: "", pathname: "/", search: "", protocol: "http:", host: "x" };
globalThis.history = { replaceState() {} };

const P = await import(new URL("../src/annealage_ember/static/project.js", import.meta.url).href);

// ── a store with the routes' wire ───────────────────────────────────────

function makeStore() {
  const s = {
    version: 1, title: "Camera", busy: false, calls: [], id: "camera",
    commits: [], // oldest first: {commit, subject, turn}
    hold: null, // a promise the next history read waits for
  };
  const etag = () => `v${s.version}`;
  s.commit = (subject, turn) => {
    s.version += 1;
    s.commits.push({ commit: `c${s.commits.length}`, subject, author: "Annie", actor: "human", date: "", files: [], turn });
  };
  s.commit("start", 0);
  for (let n = 1; n <= 6; n++) s.commit(`change ${n}`, Math.ceil(n / 2));
  const ok = (data) => ({ status: 200, data: { ok: true, ...data } });
  const conflict = (message) => ({
    status: 409, data: { ok: false, error: message, conflict: true, current_etag: etag() },
  });
  s.request = async (route, body) => {
    s.calls.push({ route, body });
    const [path, query = ""] = route.split("?");
    const q = new URLSearchParams(query);
    if (path === "api/history") {
      const hold = s.hold; // the reply is what the store held when asked, however late it lands
      const limit = Number(q.get("limit") || 100);
      const offset = Number(q.get("cursor") || 0);
      const newest = s.commits.slice().reverse();
      const page = newest.slice(offset, offset + limit);
      const turns = [...new Set(page.filter((c) => c.turn !== null).map((c) => c.turn))];
      const reply = ok({
        etag: etag(), head_label: "Turn now 3",
        commits: page.map((c) => ({ ...c, group: c.turn === null ? null : `turn:${c.turn}` })),
        groups: turns.map((t) => ({
          key: `turn:${t}`, label: t ? `Turn ${t}` : "Before the first turn",
          restore: t ? { target: `turn:${t}`, label: `Restore to before turn ${t}` } : null,
        })),
        next_cursor: offset + limit < newest.length ? String(offset + limit) : null,
      });
      if (hold) await hold;
      return reply;
    }
    if (path === "api/restore") {
      if (s.busy) return { status: 409, data: { ok: false, error: "The agent is working", busy: true } };
      if (body.expected_etag !== etag()) return conflict("the project changed after it was read");
      s.commit(`restore ${body.target}`, null);
      return ok({ etag: etag(), commit: s.commits.at(-1).commit, changes: ["a.py"], noop: false });
    }
    if (path === "api/project/name") {
      if (body.expected_etag !== etag()) return conflict("the project changed after it was read");
      s.title = body.name;
      s.commit(`name ${body.name}`, null);
      return ok({ name: body.name, etag: etag(), commit: s.commits.at(-1).commit });
    }
    if (path === "api/projects") {
      if (body === undefined) {
        return ok({ projects: [{ id: s.id, etag: etag(), title: s.title }], capabilities: { create: true, rename_id: true } });
      }
      return ok({ project: { id: body.id || "prj_1", etag: "v1", title: body.title } });
    }
    const rename = path.match(/^api\/projects\/(.+)\/rename$/);
    if (rename) {
      if (body.expected_etag !== etag()) return conflict("the project changed after it was read");
      s.id = body.id;
      s.commit(`rename ${body.id}`, null);
      return ok({ project: { id: s.id, etag: etag(), title: s.title } });
    }
    return { status: 404, data: { ok: false, error: `no route ${path}` } };
  };
  return s;
}

function setup(options = {}) {
  const store = makeStore();
  const notes = [];
  const ctx = P.createProjectContext({
    client: P.createProjectClient({ request: store.request }),
    notify: (message, ok) => notes.push({ message, ok }),
  });
  const model = P.createHistoryModel(ctx, { pageSize: 3, ...options });
  return { store, ctx, model, notes, st: model.state };
}

const subjects = (st) => st.commits.map((c) => c.subject);
const restoreCalls = (store) => store.calls.filter((c) => c.route === "api/restore");
const lastCall = (store, route) => store.calls.filter((c) => c.route === route).at(-1);

// ── cases ───────────────────────────────────────────────────────────────

const cases = {
  async client_maps_refusals_to_errors() {
    const answers = [
      { status: 409, data: { ok: false, error: "stale", conflict: true, current_etag: "v9" } },
      { status: 409, data: { ok: false, error: "working", busy: true } },
      { status: 403, data: null },
      { status: 500, data: null },
      { status: 400, data: { ok: false, error: "bad id" } },
    ];
    const client = P.createProjectClient({ request: async () => answers.shift() });
    await assert.rejects(client.restore("t", "v1"), (e) => e instanceof P.ConflictError && e.currentEtag === "v9" && e.message === "stale");
    await assert.rejects(client.restore("t", "v1"), (e) => e instanceof P.BusyError && !(e instanceof P.ConflictError));
    await assert.rejects(client.history(), (e) => e.message === P.REFUSED);
    await assert.rejects(client.history(), (e) => e.status === 500 && /failed \(500\)/.test(e.message));
    await assert.rejects(client.create({ id: "x" }), (e) => e.constructor === P.ProjectError && e.message === "bad id");
  },

  async client_names_routes_and_always_sends_the_etag() {
    const calls = [];
    const client = P.createProjectClient({ request: async (route, body) => (calls.push({ route, body }), { status: 200, data: { ok: true } }) });
    await client.history({ limit: 5, cursor: "a b" });
    await client.history();
    await client.diff("c/1");
    await client.restore("turn:2", "v7");
    await client.setTitle("T", "v7");
    await client.renameId("old one", "new", "v7");
    assert.deepEqual(calls.map((c) => c.route), [
      "api/history?limit=5&cursor=a%20b", "api/history", "api/history/c%2F1", "api/restore",
      "api/project/name", "api/projects/old%20one/rename"]);
    assert.deepEqual(calls[3].body, { target: "turn:2", expected_etag: "v7" });
    assert.deepEqual(calls[4].body, { name: "T", expected_etag: "v7" });
    assert.deepEqual(calls[5].body, { id: "new", expected_etag: "v7" });
    const inPath = P.pathEndpoints("cam era");
    assert.equal(inPath.history, "api/projects/cam%20era/history");
    assert.equal(inPath.diff("c1"), "api/projects/cam%20era/history/c1");
  },

  async history_pages_back_and_never_repeats_a_commit() {
    const { store, model, st } = setup();
    await model.refresh();
    assert.deepEqual(subjects(st), ["change 6", "change 5", "change 4"]);
    assert.equal(st.nextCursor, "3");
    assert.equal(st.headLabel, "Turn now 3");
    await model.loadMore();
    await model.loadMore();
    assert.deepEqual(subjects(st), ["change 6", "change 5", "change 4", "change 3", "change 2", "change 1", "start"]);
    assert.equal(st.nextCursor, null);
    await model.loadMore(); // nothing left: no request
    assert.equal(store.calls.filter((c) => c.route.startsWith("api/history")).length, 3);
    // Groups from every page are known, so a run split across pages still has its label.
    assert.equal(st.groups.get("turn:1").label, "Turn 1");
    assert.equal(st.groups.get("turn:0").restore, null);
  },

  async history_refresh_keeps_as_many_commits_as_are_showing() {
    const { store, model, st } = setup();
    await model.refresh();
    await model.loadMore();
    assert.equal(st.commits.length, 6);
    store.commit("change 7", 4);
    await model.refresh();
    assert.equal(st.commits.length, 6); // one request for the six showing, now starting at the newest
    assert.equal(st.commits[0].subject, "change 7");
    assert.equal(store.calls.at(-1).route, "api/history?limit=6");
  },

  async paging_across_a_new_commit_reads_again_from_the_newest() {
    const { store, model, st } = setup();
    await model.refresh();
    const before = st.etag;
    store.commit("change 7", 4); // a cursor by offset is now off by one
    await model.loadMore();
    assert.notEqual(st.etag, before);
    assert.match(st.notice, /read again/);
    assert.equal(st.commits[0].subject, "change 7");
    assert.equal(new Set(st.commits.map((c) => c.commit)).size, st.commits.length);
    assert.equal(st.loadingMore, false);
  },

  async a_reply_that_lands_after_a_later_refresh_started_is_dropped() {
    const { store, model, st } = setup();
    let release;
    store.hold = new Promise((r) => (release = r));
    const slow = model.refresh(); // started first, held
    store.hold = null;
    store.commit("change 7", 4);
    await model.refresh(); // started second, answers at once
    release();
    await slow;
    assert.equal(st.commits[0].subject, "change 7"); // the later read stands
    assert.equal(st.etag, "v9");
  },

  async restore_asks_with_the_etag_it_was_shown_and_reloads() {
    const { store, model, st, notes } = setup();
    await model.refresh();
    const shown = st.etag;
    model.requestRestore("turn:2");
    assert.equal(st.pending.etag, shown);
    assert.equal(st.pending.label, "Restore to before turn 2");
    // Turn 3's two commits and turn 2's: what a restore to before turn 2 undoes.
    assert.deepEqual(st.pending.undone.map((c) => c.subject), ["change 6", "change 5", "change 4"]);
    assert.equal(st.pending.incomplete, true); // turn 2's older commit is on the next page
    await model.confirmRestore();
    assert.deepEqual(restoreCalls(store).map((c) => c.body), [{ target: "turn:2", expected_etag: shown }]);
    assert.equal(st.pending, null);
    assert.equal(st.conflict, null);
    assert.deepEqual(notes, [{ message: "Restore to before turn 2: commit c7", ok: true }]);
    assert.equal(st.commits[0].subject, "restore turn:2"); // reloaded
    assert.notEqual(st.etag, shown);
  },

  async a_restore_refused_as_stale_stays_on_screen_with_the_changes_to_reload() {
    const { store, ctx, model, st, notes } = setup();
    await model.refresh();
    const shown = st.etag;
    model.requestRestore("turn:3");
    store.commit("someone else's change", 4); // another writer, after the page read
    await model.confirmRestore();
    assert.equal(restoreCalls(store).length, 1);
    assert.equal(st.conflict.kind, "restore");
    assert.equal(st.conflict.currentEtag, "v9");
    assert.match(st.conflict.message, /changed/);
    assert.equal(st.conflict.reloaded, false);
    assert.equal(st.pending, null); // the dialog is gone, the banner is not
    assert.deepEqual(notes, []); // not a toast that fades
    assert.equal(st.etag, shown); // nothing silently adopted
    assert.equal(store.commits.some((c) => c.subject.startsWith("restore")), false);
    // Reload shows what changed; the banner remains until dismissed, and nothing re-sends the restore.
    await model.reloadAfterConflict();
    assert.equal(st.conflict.reloaded, true);
    assert.equal(st.commits[0].subject, "someone else's change");
    assert.equal(ctx.etag, "v9");
    assert.equal(restoreCalls(store).length, 1);
    model.dismissConflict();
    assert.equal(st.conflict, null);
  },

  async a_restore_is_sent_with_the_etag_of_its_dialog_not_the_latest_the_page_learned() {
    const { store, ctx, model, st } = setup();
    await model.refresh();
    model.requestRestore("turn:3");
    store.commit("someone else's change", 4);
    ctx.setEtag("v9"); // another view of the page learned of it, this dialog was not told
    await model.confirmRestore();
    assert.deepEqual(restoreCalls(store).map((c) => c.body.expected_etag), ["v8"]);
    assert.equal(st.conflict.kind, "restore");
    assert.equal(store.commits.some((c) => c.subject.startsWith("restore")), false);
  },

  async a_restore_opened_before_the_project_moved_cannot_be_confirmed() {
    const { store, model, st } = setup();
    await model.refresh();
    model.requestRestore("turn:3");
    store.commit("someone else's change", 4);
    await model.refresh(); // an event, or the person's refresh, while the dialog is open
    assert.equal(st.pending.stale, true);
    await model.confirmRestore();
    assert.equal(restoreCalls(store).length, 0);
    // Choosing again takes the etag now showing.
    model.requestRestore("turn:3");
    assert.equal(st.pending.stale, false);
    assert.equal(st.pending.etag, "v9");
  },

  async a_busy_project_refuses_the_restore_and_keeps_the_dialog() {
    const { store, model, st } = setup();
    await model.refresh();
    model.requestRestore("turn:3");
    model.setBusy(true);
    await model.confirmRestore();
    assert.equal(restoreCalls(store).length, 0);
    model.setBusy(false);
    store.busy = true; // the store refuses though the page did not know
    await model.confirmRestore();
    assert.equal(restoreCalls(store).length, 1);
    assert.match(st.pending.error, /working/);
    assert.equal(st.conflict, null);
  },

  async only_a_group_with_a_restore_target_can_be_restored() {
    const { model, st } = setup();
    await model.refresh();
    await model.loadMore();
    await model.loadMore();
    model.requestRestore("turn:0");
    assert.equal(st.pending, null);
    model.requestRestore("nope");
    assert.equal(st.pending, null);
  },

  async diffs_are_read_once_and_failures_shown_in_place() {
    const { store, model, st } = setup();
    await model.refresh();
    const reads = () => store.calls.filter((c) => c.route.startsWith("api/history/")).length;
    await model.toggleDiff("c6");
    assert.equal(reads(), 1);
    assert.equal(st.open.has("c6"), true);
    assert.match(st.diffs.get("c6"), /could not be read: no route/); // the fake serves no diffs
    await model.toggleDiff("c6");
    await model.toggleDiff("c6");
    assert.equal(reads(), 1);
  },

  async runs_group_consecutive_commits() {
    const runs = P.runs([
      { commit: "a", group: "g2" }, { commit: "b", group: "g2" }, { commit: "c", group: null },
      { commit: "d", group: "g1" }, { commit: "e" },
    ]);
    assert.deepEqual(runs.map((r) => [r.key, r.commits.length]), [["g2", 2], [null, 1], ["g1", 1], [null, 1]]);
  },

  async a_title_edit_is_made_against_the_etag_it_began_at() {
    const { store, ctx } = setup();
    const titles = [];
    const name = P.createNameModel(ctx, { onTitle: (t) => titles.push(t) });
    await name.beginEdit("title", "Camera"); // nothing has read the project: the etag is asked for
    assert.equal(name.state.editing.etag, "v8");
    await name.submit("Camera v2");
    assert.deepEqual(lastCall(store, "api/project/name").body, { name: "Camera v2", expected_etag: "v8" });
    assert.deepEqual(titles, ["Camera v2"]);
    assert.equal(ctx.etag, "v9");
    assert.equal(name.state.conflict, null);
  },

  async a_title_edit_refused_as_stale_is_kept_and_saved_only_by_the_person_after_reload() {
    const { store, ctx, notes } = setup();
    const history = P.createHistoryModel(ctx, { pageSize: 3 });
    await history.refresh();
    const titles = [];
    const name = P.createNameModel(ctx, { onTitle: (t) => titles.push(t) });
    await name.beginEdit("title", "Camera");
    store.commit("someone else's change", 4);
    await name.submit("Mine");
    const c = name.state.conflict;
    assert.equal(c.kind, "title");
    assert.deepEqual(c.staged, { kind: "title", value: "Mine" });
    assert.equal(store.title, "Camera");
    assert.deepEqual(titles, []);
    assert.deepEqual(notes, []);
    // Before the person has seen what changed, retry does nothing.
    const sent = store.calls.length;
    await name.retryStaged();
    assert.equal(store.calls.length, sent);
    // Reload brings every view up (history too), and then the edit may be saved on the new etag.
    await name.reloadAfterConflict();
    assert.equal(name.state.conflict.reloaded, true);
    assert.equal(history.state.commits[0].subject, "someone else's change");
    await name.retryStaged();
    assert.deepEqual(lastCall(store, "api/project/name"), { route: "api/project/name", body: { name: "Mine", expected_etag: "v9" } });
    assert.equal(store.title, "Mine");
    assert.deepEqual(titles, ["Mine"]);
    assert.equal(name.state.conflict, null);
  },

  async an_unchanged_or_empty_id_saves_nothing_and_a_stale_one_is_kept() {
    const { store, ctx } = setup();
    const renamed = [];
    const name = P.createNameModel(ctx, { onRenamed: (p) => renamed.push(p.id) });
    name.state.projectId = "camera";
    await name.beginEdit("id", "camera");
    await name.submit("  camera ");
    await name.beginEdit("id", "camera");
    await name.submit("   ");
    assert.equal(store.calls.filter((c) => c.route.includes("rename")).length, 0);
    await name.beginEdit("id", "camera");
    store.commit("someone else's change", 4);
    await name.submit("cam2");
    assert.equal(name.state.conflict.staged.value, "cam2");
    assert.deepEqual(renamed, []);
    await name.reloadAfterConflict();
    await name.retryStaged();
    assert.deepEqual(renamed, ["cam2"]);
    assert.deepEqual(lastCall(store, "api/projects/camera/rename").body, { id: "cam2", expected_etag: "v9" });
  },

  async the_list_keeps_a_stale_id_change_on_its_row() {
    const { store, ctx } = setup();
    const list = P.createListModel(ctx, {});
    await list.refresh();
    const st = list.state;
    assert.deepEqual(st.entries.map((e) => e.id), ["camera"]);
    assert.equal(st.capabilities.rename_id, true);
    list.beginRename("camera");
    assert.equal(st.editing.etag, "v8");
    store.commit("someone else's change", 4);
    await list.submitRename("cam2");
    assert.equal(st.conflicts.get("camera").staged.value, "cam2");
    assert.equal(store.id, "camera");
    assert.equal(st.entries[0].etag, "v9"); // the row now shows the project as it is
    await list.retryRename("camera"); // not reloaded by the person yet: refused locally
    assert.equal(store.id, "camera");
    await list.reloadRow("camera");
    await list.retryRename("camera");
    assert.equal(store.id, "cam2");
    assert.equal(st.conflicts.size, 0);
  },

  async a_new_project_reports_the_stores_refusal_in_the_form() {
    const { ctx } = setup();
    const refusing = P.createProjectClient({ request: async () => ({ status: 400, data: { ok: false, error: "there is already a project called x" } }) });
    const list = P.createListModel(P.createProjectContext({ client: refusing, notify() {} }), {});
    assert.equal(await list.create({ id: "x" }), false);
    assert.equal(list.state.createError, "there is already a project called x");
    assert.equal(list.state.creating, false);
    const made = await P.createListModel(ctx, {}).create({ id: "lamp", title: "Lamp" });
    assert.equal(made.id, "lamp");
  },
};

if (process.argv[2] === "--list") {
  console.log(Object.keys(cases).join("\n"));
} else {
  const run = cases[process.argv[2]];
  if (!run) throw new Error(`no case ${process.argv[2]}`);
  await run();
  console.log("ok");
}
