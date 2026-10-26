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

// ── a host with send to / derived from ──────────────────────────────────

function lineageHost(options) {
  const host = {
    calls: [], answer: options.answer || null, fail: options.fail || null,
    targets: [
      { product: "alpha", label: "Alpha", can_create: true, new_label: "A new Alpha", existing: [{ id: "existing_1", title: "One" }] },
      { product: "beta", label: "Beta", can_create: true, existing: [] },
      { product: "gamma", label: "Gamma", can_create: true, existing: [], unavailable: "Gamma is not set up here." },
    ],
  };
  host.request = async (route, body) => {
    host.calls.push({ route, body });
    if (host.answer) return host.answer;
    if (route === "api/lineage") {
      return {
        status: 200,
        data: {
          ok: true, can_send: options.canSend !== false, disabled_reason: options.reason || "",
          head_revision: options.head === undefined ? "c9" : options.head,
          send_label: "Pass to", derived_label: "Came from", sent_label: "Went to",
          targets: host.targets,
          derived_from: [{ project: "origin", title: "Origin", revision: "r1", local_revision: "c0" }],
          sent_to: [{ project: "next", title: "Next", revision: "r2", local_revision: "c5" }],
        },
      };
    }
    if (host.fail) return host.fail;
    return {
      status: 200,
      data: { ok: true, derivation: { target_project: "prj_1", target_title: "Review", created: true, state: "queued", href: "/p/prj_1/" } },
    };
  };
  return host;
}

function lineageSetup(options = {}) {
  const host = lineageHost(options);
  const notes = [];
  const ctx = P.createProjectContext({
    client: P.createProjectClient({ request: host.request }),
    notify: (message, ok) => notes.push({ message, ok }),
  });
  return { host, ctx, notes, model: P.createLineageModel(ctx) };
}

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

  // ── send to / derived from ──────────────────────────────────────────

  async a_host_without_lineage_shows_nothing() {
    for (const answer of [{ status: 404, data: null }, { status: 404, data: { ok: false, error: "no", unsupported: true } }]) {
      const { model } = lineageSetup({ answer });
      await model.refresh();
      assert.equal(model.state.available, false);
      assert.equal(model.state.error, "");
    }
    // A project that does not exist is the store's answer, not an absent feature.
    const { model } = lineageSetup({ answer: { status: 404, data: { ok: false, error: "there is no project camera" } } });
    await model.refresh();
    assert.equal(model.state.available, null);
    assert.equal(model.state.error, "there is no project camera");
  },

  async the_read_gives_the_options_and_both_directions_in_the_hosts_words() {
    const { model, host } = lineageSetup();
    await model.refresh();
    const st = model.state;
    assert.equal(st.available, true);
    assert.equal(st.canSend, true);
    assert.equal(st.headRevision, "c9");
    assert.deepEqual(st.labels, { send: "Pass to", derived: "Came from", sent: "Went to" });
    assert.deepEqual(st.targets.map((t) => t.label), ["Alpha", "Beta", "Gamma"]);
    assert.deepEqual(st.derivedFrom.map((l) => l.project), ["origin"]);
    assert.deepEqual(st.sentTo.map((l) => l.project), ["next"]);
    assert.equal(host.calls[0].route, "api/lineage");
  },

  async the_client_tells_a_hosts_403_from_a_stale_link() {
    const answers = [
      { status: 403, data: { ok: false, error: "You may not send this project.", forbidden: true } },
      { status: 403, data: null },
    ];
    const client = P.createProjectClient({ request: async () => answers.shift() });
    await assert.rejects(client.lineage(), (e) => e instanceof P.ForbiddenError && e.message === "You may not send this project.");
    await assert.rejects(client.lineage(), (e) => e.constructor === P.ProjectError && e.message === P.REFUSED);
    const calls = [];
    const sending = P.createProjectClient({ request: async (r, b) => (calls.push({ r, b }), { status: 200, data: { ok: true } }) });
    await sending.send({ revision: "c1", targetProduct: "alpha", targetProject: undefined, intent: undefined });
    assert.deepEqual(calls[0], { r: "api/send", b: { revision: "c1", target_product: "alpha", target_project: null, intent: "" } });
    assert.equal(P.pathEndpoints("a b").send, "api/projects/a%20b/send");
    assert.equal(P.pathEndpoints("a b").lineage, "api/projects/a%20b/lineage");
  },

  async someone_who_may_not_send_gets_nothing_to_send_with() {
    const { model, host } = lineageSetup({ canSend: false, reason: "Viewers cannot send." });
    await model.refresh();
    const st = model.state;
    assert.equal(st.canSend, false);
    assert.equal(st.disabledReason, "Viewers cannot send.");
    model.toggleMenu(true);
    assert.equal(st.menuOpen, false);
    model.chooseRevision("c3");
    assert.equal(st.menuOpen, false);
    model.begin("alpha");
    assert.equal(st.draft, null);
    assert.equal(host.calls.filter((c) => c.route === "api/send").length, 0);
  },

  async a_send_carries_the_revision_chosen_and_the_target_and_reads_again() {
    const { model, host, notes } = lineageSetup();
    await model.refresh();
    const st = model.state;
    model.chooseRevision("c3");
    assert.equal(st.menuOpen, true);
    model.begin("alpha");
    assert.equal(st.menuOpen, false);
    assert.equal(st.draft.revision, "c3");
    assert.equal(st.draft.project, null); // a new one unless chosen
    model.setProject("existing_1");
    model.setIntent("please review");
    await model.confirm();
    const sends = () => host.calls.filter((c) => c.route === "api/send");
    assert.deepEqual(sends().map((c) => c.body), [
      { revision: "c3", target_product: "alpha", target_project: "existing_1", intent: "please review" }]);
    assert.equal(st.draft, null);
    assert.equal(st.revision, null);
    assert.equal(st.result.label, "Alpha");
    assert.equal(st.result.target_project, "prj_1");
    assert.deepEqual(notes.at(-1), { message: "Went to Alpha", ok: true });
    assert.equal(host.calls.filter((c) => c.route === "api/lineage").length, 2);
    // The project as it is now is the default revision, and a new target sends null.
    model.begin("beta");
    assert.equal(st.draft.revision, "c9");
    await model.confirm();
    assert.equal(sends().length, 2);
    assert.equal(sends().at(-1).body.target_project, null);
    assert.equal(sends().at(-1).body.revision, "c9");
  },

  async a_product_that_cannot_be_chosen_and_a_project_with_nothing_to_send_are_refused_locally() {
    const { model, host } = lineageSetup();
    await model.refresh();
    model.begin("gamma"); // listed, unavailable
    assert.equal(model.state.draft, null);
    model.begin("nope");
    assert.equal(model.state.draft, null);
    const empty = lineageSetup({ head: "" });
    await empty.model.refresh();
    empty.model.begin("alpha");
    await empty.model.confirm();
    assert.match(empty.model.state.draft.error, /no revision/);
    assert.equal(empty.host.calls.filter((c) => c.route === "api/send").length, 0);
    assert.equal(host.calls.filter((c) => c.route === "api/send").length, 0);
  },

  async a_send_refused_as_busy_or_moved_on_is_kept_and_never_retried() {
    for (const [fail, busy] of [
      [{ status: 409, data: { ok: false, error: "the target is being built", busy: true } }, true],
      [{ status: 409, data: { ok: false, error: "the target moved on", conflict: true, current_etag: "v4" } }, false],
    ]) {
      const { model, host } = lineageSetup({ fail });
      await model.refresh();
      const st = model.state;
      model.begin("alpha");
      model.setIntent("mine");
      await model.confirm();
      assert.equal(st.draft, null);
      assert.equal(st.conflict.busy, busy);
      assert.equal(st.conflict.message, fail.data.error);
      assert.equal(st.conflict.label, "Alpha");
      assert.equal(st.conflict.request.intent, "mine");
      const sends = () => host.calls.filter((c) => c.route === "api/send").length;
      assert.equal(sends(), 1);
      // Not offered again before the person has seen the options as they are.
      model.sendAgain();
      assert.ok(st.conflict && st.draft === null);
      await model.reloadAfterConflict();
      assert.equal(st.conflict.reloaded, true);
      assert.equal(sends(), 1, "nothing was sent behind the person's back");
      host.fail = null;
      model.sendAgain();
      assert.equal(st.conflict, null);
      assert.equal(st.draft.intent, "mine");
      assert.equal(sends(), 1);
      await model.confirm(); // their own click
      assert.equal(sends(), 2);
      assert.equal(st.result.target_project, "prj_1");
    }
  },

  async a_kept_send_is_dropped_with_a_reason_when_the_options_no_longer_have_it() {
    const { model, host } = lineageSetup({ fail: { status: 409, data: { ok: false, error: "moved", conflict: true } } });
    await model.refresh();
    model.begin("alpha");
    await model.confirm();
    host.targets = host.targets.filter((t) => t.product !== "alpha");
    await model.reloadAfterConflict();
    model.sendAgain();
    assert.equal(model.state.draft, null);
    assert.equal(model.state.conflict, null);
    assert.match(model.state.error, /no longer offered/);
  },

  async a_forbidden_send_stays_in_the_dialog_with_the_hosts_words() {
    const { model, host } = lineageSetup({
      fail: { status: 403, data: { ok: false, error: "You may not send this project.", forbidden: true } },
    });
    await model.refresh();
    model.begin("alpha");
    await model.confirm();
    assert.equal(model.state.draft.error, "You may not send this project.");
    assert.equal(model.state.draft.sending, false);
    assert.equal(model.state.conflict, null);
    assert.equal(host.calls.filter((c) => c.route === "api/send").length, 1);
  },

  async only_one_send_is_in_flight() {
    const { model, host } = lineageSetup();
    await model.refresh();
    model.begin("alpha");
    await Promise.all([model.confirm(), model.confirm()]);
    assert.equal(host.calls.filter((c) => c.route === "api/send").length, 1);
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
