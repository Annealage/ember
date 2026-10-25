/**
 * Project management for a product's page: a list of projects (start one,
 * change one's id), a project's title, and its history (paged, grouped, a
 * diff on request, restore after asking). It is the page's side of
 * `annealage_ember.projects`, which says what the routes answer and what a
 * store must do; this module knows nothing else about the store or the
 * product, and holds no project content.
 *
 * Wiring, in the product's main module:
 *
 *   const ctx = createProjectContext({ client: createProjectClient() });
 *   const history = initProjectHistory({ ctx, root: historyPane, onRestored });
 *   initProjectHeader({ ctx, root: titleBar, title: "Camera board", projectId,
 *                       onTitle, onRenamed });
 *   initProjectList({ ctx, root: listPane });       // a page that lists projects
 *   const lineage = initProjectLineage({ ctx, root: lineagePane });  // optional: send to / derived from
 *   // initProjectHistory({ ..., lineage }) then offers "send" on each commit too
 *
 * `createProjectClient({endpoints, request})` names the routes relative to the
 * page (`appUrl`, the page's token), `endpoints` replacing any of them (the
 * default is a page bound to one project; `pathEndpoints(id)` is the form for a
 * page that lists projects). `request(route, body)` resolves `{status, data}`
 * and may be replaced, for a test or a host with its own transport.
 *
 * Every change is made against the etag of the state the person was looking
 * at: history, a list entry and every successful change report the store's
 * etag, `ctx.etag` holds the latest, and an edit begun at one etag is sent
 * with it. A store that finds the project moved on refuses (`ConflictError`),
 * and the page does not hide it: a banner says so and stays until dismissed,
 * the edit the person staged (a title, an id) is kept in it, Reload brings the
 * page up to the project as it is now, and only then may the kept edit be
 * saved, against the new etag, by the person's own click. A change is never
 * retried behind their back.
 *
 * The state and logic are in DOM-free models (`createHistoryModel`,
 * `createNameModel`, `createListModel`) that call `onChange(state)`; the
 * `init*` functions render them into a root element they own the contents of.
 *
 * Send to / derived from is optional: `initProjectLineage` reads the host's
 * lineage route once, and when the host has none (the route answers 404 with no
 * JSON, or says unsupported) it shows nothing at all. When it has, the page
 * shows a menu of the host's target products (words and products are all the
 * host's), a dialog to choose a new or an existing target project, and the
 * lists of what the project was derived from and sent to. The host's refusals
 * are shown, not retried: a 403 (`ForbiddenError`) in the dialog, a 409 as a
 * banner that keeps the request until the person reloads and sends again, and
 * someone who may not send gets the menu disabled with the host's reason.
 *
 * Every string from the server is written as text, never markup.
 */

import { toast } from "./ui.js";
import { appUrl } from "./url.js";
import { withToken } from "./ws.js";

// ── client ──────────────────────────────────────────────────────────────

export class ProjectError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = "ProjectError";
    this.status = status;
    this.data = data;
  }
}

/** The store refused a change: the project is no longer at the etag it was made against. */
export class ConflictError extends ProjectError {
  constructor(message, status, data) {
    super(message, status, data);
    this.name = "ConflictError";
    this.currentEtag = (data && data.current_etag) || null;
  }
}

/** Refused while the project is held (an agent turn, a build): the same request will do after. */
export class BusyError extends ProjectError {
  constructor(message, status, data) {
    super(message, status, data);
    this.name = "BusyError";
  }
}

/** The store refused this caller outright (a viewer, another workspace), in words for the person. */
export class ForbiddenError extends ProjectError {
  constructor(message, status, data) {
    super(message, status, data);
    this.name = "ForbiddenError";
  }
}

export const REFUSED =
  "This page is not signed in, or its link has gone stale: open the URL the server printed.";

const DEFAULT_ENDPOINTS = {
  list: "api/projects",
  create: "api/projects",
  renameId: (id) => `api/projects/${encodeURIComponent(id)}/rename`,
  title: "api/project/name",
  history: "api/history",
  diff: (commit) => `api/history/${encodeURIComponent(commit)}`,
  restore: "api/restore",
  lineage: "api/lineage",
  send: "api/send",
};

/** The history routes of a page that lists projects: the id is in the path. */
export function pathEndpoints(projectId) {
  const base = `api/projects/${encodeURIComponent(projectId)}`;
  return {
    title: `${base}/name`,
    history: `${base}/history`,
    diff: (commit) => `${base}/history/${encodeURIComponent(commit)}`,
    restore: `${base}/restore`,
    lineage: `${base}/lineage`,
    send: `${base}/send`,
  };
}

async function pageRequest(route, body) {
  const init = body === undefined
    ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(withToken(appUrl(route)), init);
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    // A body that is not JSON: the status says enough.
  }
  return { status: res.status, data };
}

export function createProjectClient({ endpoints = {}, request = pageRequest } = {}) {
  const ep = { ...DEFAULT_ENDPOINTS, ...endpoints };
  const route = (name, ...args) => (typeof ep[name] === "function" ? ep[name](...args) : ep[name]);

  async function call(path, body) {
    const { status, data } = await request(path, body);
    // The server's own 403 says forbidden: true; the authentication refusal is plain text.
    if (status === 403 && data && data.forbidden) {
      throw new ForbiddenError(data.error || "You may not do that.", status, data);
    }
    if (status === 403) throw new ProjectError(REFUSED, status, data);
    if (status >= 200 && status < 300 && data && data.ok !== false) return data;
    const message = (data && data.error) || `${path} failed (${status})`;
    if (data && data.conflict) throw new ConflictError(message, status, data);
    if (data && data.busy) throw new BusyError(message, status, data);
    throw new ProjectError(message, status, data);
  }

  const query = (params) => {
    const parts = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`);
    return parts.length ? `?${parts.join("&")}` : "";
  };

  return {
    list: () => call(route("list")),
    create: ({ id, title, brief }) => call(route("create"), { id, title, brief }),
    renameId: (id, newId, expectedEtag) =>
      call(route("renameId", id), { id: newId, expected_etag: expectedEtag }),
    setTitle: (name, expectedEtag) => call(route("title"), { name, expected_etag: expectedEtag }),
    history: ({ limit, cursor } = {}) => call(route("history") + query({ limit, cursor })),
    diff: (commit) => call(route("diff", commit)),
    restore: (target, expectedEtag) => call(route("restore"), { target, expected_etag: expectedEtag }),
    lineage: () => call(route("lineage")),
    send: ({ revision, targetProduct, targetProject, intent }) =>
      call(route("send"), {
        revision, target_product: targetProduct, target_project: targetProject || null, intent: intent || "",
      }),
  };
}

// ── shared context ──────────────────────────────────────────────────────

/**
 * What the models of one page share: the client, how to tell the person
 * (`notify(message, ok)`, the page's toast), the latest etag, and the reload
 * every model that shows project state registers, so a conflict resolved in
 * one view brings the others up to date.
 */
export function createProjectContext({ client, notify = toast } = {}) {
  const reloaders = new Set();
  let fresh = false;
  const ctx = {
    client,
    notify,
    etag: null,
    setEtag(etag) {
      if (typeof etag === "string" && etag) {
        ctx.etag = etag;
        fresh = true;
      }
    },
    addReloader(fn) {
      reloaders.add(fn);
      return () => reloaders.delete(fn);
    },
    /** Bring every registered view up to the project as it is now. */
    async reload() {
      await Promise.all([...reloaders].map((fn) => fn()));
    },
    /**
     * `reload`, and the etag read again whether or not a view that reads it
     * is registered: the one a refused change is resolved with, so the edit
     * kept is never saved against the etag that was just refused.
     */
    async resync() {
      fresh = false;
      await ctx.reload();
      if (!fresh) {
        ctx.etag = null;
        await ctx.ensureEtag();
      }
    },
    /** The etag, asking the store for it when nothing has read the project yet. */
    async ensureEtag() {
      if (!ctx.etag) ctx.setEtag((await client.history({ limit: 1 })).etag);
      return ctx.etag;
    },
  };
  return ctx;
}

/** A refused change as the conflict banners hold it, with the edit staged. */
function conflictOf(kind, err, staged) {
  return { kind, message: err.message, currentEtag: err.currentEtag, staged, reloaded: false };
}

const clamp = (n, lo, hi) => Math.min(Math.max(n, lo), hi);
const MAX_PAGE = 500;

// ── history model ───────────────────────────────────────────────────────

/** Consecutive commits of one group, newest first. */
export function runs(commits) {
  const out = [];
  for (const c of commits) {
    const last = out[out.length - 1];
    const key = c.group === undefined ? null : c.group;
    if (last && last.key === key) last.commits.push(c);
    else out.push({ key, commits: [c] });
  }
  return out;
}

export function createHistoryModel(ctx, { pageSize = 100, onChange = () => {}, onRestored = () => {} } = {}) {
  const st = {
    commits: [], groups: new Map(), headLabel: "", etag: null, nextCursor: null,
    loading: false, loadingMore: false, error: "", notice: "",
    open: new Set(), diffs: new Map(), pending: null, restoring: false,
    busy: false, conflict: null,
  };
  let generation = 0;
  const emit = () => onChange(st);

  function apply(data) {
    st.groups = new Map(data.groups.map((g) => [g.key, g]));
    st.headLabel = data.head_label || "";
    st.etag = data.etag;
    ctx.setEtag(data.etag);
    st.nextCursor = data.next_cursor || null;
    if (st.pending) st.pending.stale = st.pending.etag !== st.etag;
  }

  /** Read the newest commits again, as many as are showing (at least a page). */
  async function refresh() {
    const mine = ++generation;
    st.loading = true;
    emit();
    try {
      const data = await ctx.client.history({
        limit: clamp(Math.max(pageSize, st.commits.length), 1, MAX_PAGE),
      });
      if (mine !== generation) return;
      st.commits = data.commits;
      apply(data);
      st.error = "";
    } catch (err) {
      if (mine !== generation) return;
      st.error = err.message;
    } finally {
      if (mine === generation) st.loading = false;
    }
    emit();
  }

  /**
   * The next page of older commits. A page read at another etag than the one
   * showing means the project moved while paging, so a cursor into the old
   * state is not trusted: the list is read again from the newest instead.
   */
  async function loadMore() {
    if (!st.nextCursor || st.loadingMore) return;
    const mine = generation;
    st.loadingMore = true;
    emit();
    try {
      const data = await ctx.client.history({ limit: pageSize, cursor: st.nextCursor });
      if (mine !== generation) return;
      if (data.etag !== st.etag) {
        st.loadingMore = false;
        st.notice = "The project changed while older commits were loading, so the list was read again.";
        await refresh();
        return;
      }
      const seen = new Set(st.commits.map((c) => c.commit));
      st.commits = st.commits.concat(data.commits.filter((c) => !seen.has(c.commit)));
      for (const g of data.groups) st.groups.set(g.key, g);
      st.nextCursor = data.next_cursor || null;
      st.error = "";
    } catch (err) {
      if (mine !== generation) return;
      st.error = err.message;
    } finally {
      st.loadingMore = false;
    }
    emit();
  }

  async function toggleDiff(commit) {
    if (st.open.has(commit)) {
      st.open.delete(commit);
      emit();
      return;
    }
    st.open.add(commit);
    if (!st.diffs.has(commit)) {
      st.diffs.set(commit, null);
      emit();
      try {
        st.diffs.set(commit, (await ctx.client.diff(commit)).diff);
      } catch (err) {
        st.diffs.set(commit, `The diff could not be read: ${err.message}`);
      }
    }
    emit();
  }

  /**
   * Ask to put the project back to before group `key`. The request keeps the
   * etag the person is looking at: the one the store will compare.
   */
  function requestRestore(key) {
    const group = st.groups.get(key);
    if (!group || !group.restore) return;
    // Everything newer than the group's oldest loaded commit, ungrouped
    // commits between included, goes with it.
    let oldest = -1;
    st.commits.forEach((c, i) => {
      if (c.group === key) oldest = i;
    });
    st.pending = {
      key, target: group.restore.target, label: group.restore.label, groupLabel: group.label,
      undone: st.commits.slice(0, oldest + 1),
      // The group's older commits may sit on a page not loaded yet.
      incomplete: oldest === st.commits.length - 1 && st.nextCursor !== null,
      etag: st.etag, stale: false, error: "",
    };
    emit();
  }

  function cancelRestore() {
    st.pending = null;
    emit();
  }

  async function confirmRestore() {
    const request = st.pending;
    if (!request || st.busy || st.restoring || request.stale) return;
    st.restoring = true;
    request.error = "";
    emit();
    try {
      const result = await ctx.client.restore(request.target, request.etag);
      ctx.setEtag(result.etag);
      st.pending = null;
      st.conflict = null;
      ctx.notify(result.noop
        ? "Nothing to restore: the project is already as it was then"
        : `${request.label}: commit ${result.commit}`, true);
      st.restoring = false;
      await onRestored(result);
      await ctx.reload();
      return;
    } catch (err) {
      if (err instanceof ConflictError) {
        st.conflict = conflictOf("restore", err, null);
        st.pending = null;
      } else {
        request.error = err.message;
      }
    }
    st.restoring = false;
    emit();
  }

  function setBusy(busy) {
    st.busy = busy;
    emit();
  }

  /** Reload after a refused restore, so the person sees what changed. */
  async function reloadAfterConflict() {
    if (!st.conflict) return;
    await ctx.resync();
    if (st.conflict) st.conflict.reloaded = true;
    emit();
  }

  function dismissConflict() {
    st.conflict = null;
    emit();
  }

  ctx.addReloader(refresh);
  return {
    state: st, refresh, loadMore, toggleDiff, requestRestore, cancelRestore,
    confirmRestore, setBusy, reloadAfterConflict, dismissConflict,
  };
}

// ── name model: a title or an id, staged and saved against an etag ─────────

/**
 * Edits of the project's title (`kind: "title"`) and, for a store that has
 * them, id (`"id"`). `beginEdit` records the etag the person started from;
 * `submit` sends the edit against it. Refused as stale, the edit stays in
 * `state.conflict.staged`; `reloadAfterConflict` reads the project again, and
 * `retryStaged` (offered only after it) saves the kept edit on the new etag.
 */
export function createNameModel(ctx, { onChange = () => {}, onTitle = () => {}, onRenamed = () => {} } = {}) {
  const st = { editing: null, saving: false, error: "", conflict: null, projectId: null };
  const emit = () => onChange(st);

  async function beginEdit(kind, value) {
    if (st.editing) return;
    try {
      st.editing = { kind, value, etag: await ctx.ensureEtag() };
      st.error = "";
    } catch (err) {
      st.error = err.message;
    }
    emit();
  }

  function cancelEdit() {
    if (!st.editing) return; // a field removed by a render blurs and lands here
    st.editing = null;
    emit();
  }

  async function save(kind, value, etag) {
    st.saving = true;
    st.error = "";
    emit();
    try {
      if (kind === "title") {
        const r = await ctx.client.setTitle(value, etag);
        ctx.setEtag(r.etag);
        ctx.notify(r.commit ? `Renamed: commit ${r.commit}` : "Name unchanged", true);
        onTitle(r.name);
      } else {
        const r = await ctx.client.renameId(st.projectId, value, etag);
        ctx.setEtag(r.project.etag);
        ctx.notify(`Renamed to ${r.project.id}`, true);
        onRenamed(r.project);
      }
      st.conflict = null;
      st.saving = false;
      emit();
      await ctx.reload();
      return;
    } catch (err) {
      if (err instanceof ConflictError) st.conflict = conflictOf(kind, err, { kind, value });
      else st.error = err.message;
    }
    st.saving = false;
    emit();
  }

  /** Save the edit in progress; an empty id or an unchanged one saves nothing. */
  async function submit(value) {
    const edit = st.editing;
    if (!edit) return;
    st.editing = null;
    if (edit.kind === "id" && (!value.trim() || value.trim() === st.projectId)) {
      emit();
      return;
    }
    await save(edit.kind, edit.kind === "id" ? value.trim() : value, edit.etag);
  }

  async function reloadAfterConflict() {
    if (!st.conflict) return;
    await ctx.resync();
    if (st.conflict) st.conflict.reloaded = true;
    emit();
  }

  /** The kept edit, saved on the etag the reload read: the person's own click. */
  async function retryStaged() {
    const c = st.conflict;
    if (!c || !c.staged || !c.reloaded) return;
    await save(c.staged.kind, c.staged.value, ctx.etag);
  }

  function dismissConflict() {
    st.conflict = null;
    emit();
  }

  return { state: st, beginEdit, cancelEdit, submit, reloadAfterConflict, retryStaged, dismissConflict };
}

// ── list model ──────────────────────────────────────────────────────────

/**
 * The projects of a directory. A row's id change is made against the etag its
 * entry carried when the list was read, so a project changed since is refused
 * and the row says so, keeping the id the person typed.
 */
export function createListModel(ctx, { onChange = () => {}, onRenamed = () => {} } = {}) {
  const st = {
    entries: [], capabilities: {}, loading: false, error: "", creating: false,
    createError: "", editing: null, conflicts: new Map(), rowError: new Map(),
  };
  let generation = 0;
  const emit = () => onChange(st);

  async function refresh() {
    const mine = ++generation;
    st.loading = true;
    emit();
    try {
      const data = await ctx.client.list();
      if (mine !== generation) return;
      st.entries = data.projects;
      st.capabilities = data.capabilities || {};
      st.error = "";
    } catch (err) {
      if (mine !== generation) return;
      st.error = err.message;
    } finally {
      if (mine === generation) st.loading = false;
    }
    emit();
  }

  async function create({ id, title, brief }) {
    if (st.creating) return false;
    st.creating = true;
    st.createError = "";
    emit();
    try {
      const data = await ctx.client.create({ id: id || undefined, title: title || "", brief: brief || "" });
      ctx.notify(`Started ${data.project.title || data.project.id}`, true);
      st.creating = false;
      await refresh();
      return data.project;
    } catch (err) {
      st.createError = err.message;
    }
    st.creating = false;
    emit();
    return false;
  }

  function beginRename(id) {
    const entry = st.entries.find((e) => e.id === id);
    if (!entry || st.editing) return;
    st.editing = { id, value: id, etag: entry.etag };
    st.rowError.delete(id);
    emit();
  }

  function cancelRename() {
    if (!st.editing) return; // an input removed by a render blurs and lands here
    st.editing = null;
    emit();
  }

  async function rename(id, newId, etag) {
    st.rowError.delete(id);
    try {
      const data = await ctx.client.renameId(id, newId, etag);
      st.conflicts.delete(id);
      ctx.notify(`Renamed ${id} to ${data.project.id}`, true);
      onRenamed(id, data.project);
    } catch (err) {
      if (err instanceof ConflictError) st.conflicts.set(id, conflictOf("id", err, { kind: "id", value: newId }));
      else st.rowError.set(id, err.message);
    }
    await refresh();
  }

  async function submitRename(value) {
    const edit = st.editing;
    if (!edit) return;
    st.editing = null;
    const next = value.trim();
    if (!next || next === edit.id) {
      emit();
      return;
    }
    await rename(edit.id, next, edit.etag);
  }

  /** Read the list again so the row shows the project as it is now; the kept id stays offered. */
  async function reloadRow(id) {
    const c = st.conflicts.get(id);
    if (!c) return;
    await refresh();
    c.reloaded = true;
    emit();
  }

  async function retryRename(id) {
    const c = st.conflicts.get(id);
    const entry = st.entries.find((e) => e.id === id);
    if (!c || !c.reloaded || !entry) return;
    await rename(id, c.staged.value, entry.etag);
  }

  function dismissConflict(id) {
    st.conflicts.delete(id);
    emit();
  }

  ctx.addReloader(refresh);
  return {
    state: st, refresh, create, beginRename, cancelRename, submitRename,
    reloadRow, retryRename, dismissConflict,
  };
}

// ── lineage model: send to / derived from ───────────────────────────────

/**
 * What the host offers for sending this project on, and where it came from.
 * `state.available` is null until the first read, false when the host has no
 * lineage (nothing is shown), true when it has. A send is only ever made by
 * `confirm`, from a draft the person filled in; a refused one is never
 * retried, only offered again after a reload (`sendAgain`).
 */
export function createLineageModel(ctx, { onChange = () => {} } = {}) {
  const st = {
    available: null, loading: false, error: "", canSend: false, disabledReason: "", headRevision: "",
    labels: { send: "Send to", derived: "Derived from", sent: "Sent to" },
    targets: [], derivedFrom: [], sentTo: [],
    menuOpen: false, revision: null, draft: null, result: null, conflict: null,
  };
  let generation = 0;
  const listeners = new Set([onChange]);
  const emit = () => listeners.forEach((fn) => fn(st));
  const targetOf = (product) => st.targets.find((t) => t.product === product);

  /** A host without lineage: its app has no such route (404, no JSON) or says so. */
  const absent = (err) => err.status === 404 && (!err.data || err.data.unsupported === true);

  async function refresh() {
    const mine = ++generation;
    st.loading = true;
    emit();
    try {
      const data = await ctx.client.lineage();
      if (mine !== generation) return;
      st.available = true;
      st.canSend = data.can_send !== false;
      st.disabledReason = data.disabled_reason || "";
      st.headRevision = data.head_revision || "";
      st.labels = {
        send: data.send_label || "Send to", derived: data.derived_label || "Derived from",
        sent: data.sent_label || "Sent to",
      };
      st.targets = data.targets || [];
      st.derivedFrom = data.derived_from || [];
      st.sentTo = data.sent_to || [];
      if (!st.canSend) st.menuOpen = false;
      st.error = "";
    } catch (err) {
      if (mine !== generation) return;
      if (absent(err)) {
        st.available = false;
        st.error = "";
      } else {
        st.error = err.message;
      }
    } finally {
      if (mine === generation) st.loading = false;
    }
    emit();
  }

  function toggleMenu(open = !st.menuOpen) {
    st.menuOpen = open && st.canSend && st.targets.length > 0;
    emit();
  }

  /** Send this revision (a commit of the history) instead of the project as it is now. */
  function chooseRevision(revision) {
    st.revision = revision || null;
    st.menuOpen = st.canSend && st.targets.length > 0;
    emit();
  }

  /** Open the dialog for sending to `product`: a new project of it unless it can only extend one. */
  function begin(product) {
    const target = targetOf(product);
    if (!target || target.unavailable || !st.canSend) return;
    const existing = target.existing || [];
    st.menuOpen = false;
    st.result = null;
    st.draft = {
      product, revision: st.revision || st.headRevision,
      project: target.can_create === false && existing.length ? existing[0].id : null,
      intent: "", sending: false, error: "",
    };
    emit();
  }

  function setProject(id) {
    if (st.draft) st.draft.project = id || null;
  }

  function setIntent(text) {
    if (st.draft) st.draft.intent = text;
  }

  function cancelDraft() {
    if (!st.draft) return;
    st.draft = null;
    emit();
  }

  async function confirm() {
    const d = st.draft;
    if (!d || d.sending) return;
    const target = targetOf(d.product);
    if (!st.canSend) {
      d.error = st.disabledReason || "You may not send this project.";
      emit();
      return;
    }
    if (!d.revision) {
      d.error = "This project has no revision to send yet.";
      emit();
      return;
    }
    d.sending = true;
    d.error = "";
    emit();
    try {
      const data = await ctx.client.send({
        revision: d.revision, targetProduct: d.product, targetProject: d.project, intent: d.intent,
      });
      const r = data.derivation;
      const label = target ? target.label : r.target_product || d.product;
      st.draft = null;
      st.conflict = null;
      st.revision = null;
      st.result = { ...r, label, revision: d.revision };
      ctx.notify(r.created === false ? `Already sent to ${label}` : `${st.labels.sent} ${label}`, true);
      emit();
      await refresh();
      return;
    } catch (err) {
      d.sending = false;
      if (err instanceof ConflictError || err instanceof BusyError) {
        st.conflict = {
          kind: "send", message: err.message, busy: err instanceof BusyError,
          reloaded: false, request: d, label: target ? target.label : d.product,
        };
        st.draft = null;
      } else {
        d.error = err.message;
      }
    }
    emit();
  }

  async function reloadAfterConflict() {
    if (!st.conflict) return;
    await refresh();
    if (st.conflict) st.conflict.reloaded = true;
    emit();
  }

  /** The kept request, back in the dialog once the options are read again: the person's own click sends it. */
  function sendAgain() {
    const c = st.conflict;
    if (!c || !c.reloaded) return;
    const target = targetOf(c.request.product);
    st.conflict = null;
    if (!target || target.unavailable || !st.canSend) {
      st.error = target && target.unavailable ? target.unavailable
        : "That is no longer offered for this project.";
    } else {
      st.draft = { ...c.request, sending: false, error: "" };
    }
    emit();
  }

  function dismissConflict() {
    st.conflict = null;
    emit();
  }

  function dismissResult() {
    st.result = null;
    emit();
  }

  /** `fn(state)` after every change; for a view (the history pane) that shows part of it. */
  function subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  ctx.addReloader(refresh);
  return {
    state: st, refresh, toggleMenu, chooseRevision, begin, setProject, setIntent, cancelDraft,
    confirm, reloadAfterConflict, sendAgain, dismissConflict, dismissResult, subscribe,
  };
}

// ── DOM ─────────────────────────────────────────────────────────────────

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c);
  return el;
}

const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

function when(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function diffLines(text) {
  return text.split("\n").map((line) => {
    const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff --git") ? "pj-d-file"
      : line.startsWith("+") ? "pj-d-add" : line.startsWith("-") ? "pj-d-del"
        : line.startsWith("@@") ? "pj-d-hunk" : "";
    return h("span", { class: cls }, line + "\n");
  });
}

/**
 * The conflict banner: the store's refusal, the edit that was kept, and the
 * way forward. It stays until the person dismisses it or saves.
 */
function conflictBanner(conflict, { reload, retry, dismiss }) {
  const kept = conflict.staged
    ? h("p", {}, conflict.staged.kind === "id"
      ? `Your new id, `
      : "Your new title, ", h("b", {}, JSON.stringify(conflict.staged.value)),
      conflict.reloaded ? " is kept below; it has not been saved." : " is kept; it has not been saved.")
    : null;
  return h("div", { class: "pj-conflict", role: "alert" },
    h("p", {}, h("b", {}, "Not saved: the project changed since you started. "), conflict.message),
    kept,
    conflict.reloaded && !conflict.staged
      ? h("p", {}, "Reloaded: this is the project as it is now. Choose again if you still want it.") : null,
    h("p", { class: "pj-actions" },
      conflict.reloaded ? null : h("button", { type: "button", class: "pj-btn", onclick: reload },
        "Reload to see what changed"),
      conflict.reloaded && conflict.staged
        ? h("button", { type: "button", class: "pj-btn", onclick: retry }, "Save my edit on the latest") : null,
      h("button", { type: "button", class: "pj-btn pj-quiet", onclick: dismiss },
        conflict.staged ? "Discard my edit" : "Dismiss")));
}

/** A modal over `root`: title, body, an error line, Cancel and a confirming button. */
function createModal(root) {
  const title = h("h2", { id: `pj-modal-title-${Math.random().toString(36).slice(2)}` });
  const body = h("div", { class: "pj-modal-body" });
  const error = h("div", { class: "pj-error", role: "alert" });
  const cancel = h("button", { type: "button", class: "pj-btn pj-quiet" }, "Cancel");
  const ok = h("button", { type: "button", class: "pj-btn pj-danger" });
  const box = h("div", { class: "pj-dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": title.id },
    h("header", {}, title), h("div", { class: "pj-modal-main" }, body, error), h("footer", {}, cancel, ok));
  const el = h("div", { class: "pj-modal", hidden: true }, box);
  let onClose = () => {};
  let onOk = () => {};
  const close = () => {
    if (el.hidden) return;
    el.hidden = true;
    onClose();
  };
  cancel.addEventListener("click", close);
  ok.addEventListener("click", () => onOk());
  el.addEventListener("click", (e) => {
    if (e.target === el) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el.hidden) close();
  });
  root.append(el);
  return {
    el, close,
    /** Show it; `show` fills (or refills) it and may be called again while open. */
    show({ heading, content, okLabel, okDisabled, errorText, ok: onConfirm, closed }) {
      title.textContent = heading;
      body.replaceChildren(...content);
      ok.textContent = okLabel;
      ok.disabled = !!okDisabled;
      error.textContent = errorText || "";
      onOk = onConfirm;
      onClose = closed || (() => {});
      const opening = el.hidden;
      el.hidden = false;
      if (opening) cancel.focus();
    },
  };
}

/** The lineage of one project: the other project, what it is, and the revisions the edge joins. */
function lineageItem(link, forward) {
  const there = link.revision ? h("code", { title: "revision of the other project" }, link.revision) : null;
  const here = link.local_revision ? h("code", { title: "revision of this project" }, link.local_revision) : null;
  const name = link.title || link.project;
  return h("li", { class: "pj-link", "data-project": link.project },
    h("div", { class: "pj-linkhead" },
      link.href ? h("a", { href: link.href, class: "pj-name" }, name) : h("span", { class: "pj-name" }, name),
      link.product_label ? h("span", { class: "pj-chip" }, link.product_label) : null,
      link.relation_label || link.relation
        ? h("span", { class: "pj-chip pj-relation" }, link.relation_label || link.relation) : null,
      link.state ? h("span", { class: "pj-state" }, link.state) : null),
    h("div", { class: "pj-meta" },
      here && there ? h("span", {}, ...(forward ? [here, " → ", there] : [there, " → ", here])) : (here || there),
      link.created_at ? h("span", {}, when(link.created_at)) : null,
      link.intent ? h("span", { class: "pj-intent" }, link.intent) : null));
}

/** The refusal of a send as a banner: it stays until dismissed, with the request kept and nothing sent. */
function sendBanner(conflict, { reload, again, dismiss }) {
  return h("div", { class: "pj-conflict", role: "alert" },
    h("p", {}, h("b", {}, conflict.busy ? "Not sent: it is busy. " : "Not sent: it changed since you started. "),
      conflict.message),
    h("p", {}, `Nothing was sent to ${conflict.label}, and your request is kept.`),
    conflict.reloaded ? h("p", {}, "Reloaded: these are the options as they are now.") : null,
    h("p", { class: "pj-actions" },
      conflict.reloaded ? null : h("button", { type: "button", class: "pj-btn", onclick: reload },
        "Reload to see what changed"),
      conflict.reloaded ? h("button", { type: "button", class: "pj-btn", onclick: again }, "Review and send again") : null,
      h("button", { type: "button", class: "pj-btn pj-quiet", onclick: dismiss }, "Discard my request")));
}

const BUSY = "The agent is working on a turn: wait for it to finish, or interrupt it, then restore.";

/**
 * The history pane in `root` (its contents are this module's): newest commits
 * first in their groups, a diff on request, older commits on "Load older",
 * Restore on each group that has it (asking first), the conflict banner when
 * the store refuses a restore as stale. `busy` text replaces the default
 * refusal shown while the product says a change would race (`setBusy`).
 * `lineage` (the model `initProjectLineage` returned) adds "send" to each
 * commit while the host offers it: it picks that commit as the revision.
 * Returns the model: `refresh()` when the product knows history changed,
 * `setBusy(bool)`.
 */
export function initProjectHistory({ ctx, root, pageSize = 100, onRestored = () => {}, busyText = BUSY, lineage = null } = {}) {
  root.classList.add("pj-history");
  const now = h("span", { class: "pj-now" });
  const refreshButton = h("button", { type: "button", class: "pj-btn pj-refresh" }, "Refresh");
  const banner = h("div", { class: "pj-banner" });
  const error = h("div", { class: "pj-error", role: "alert" });
  const notice = h("div", { class: "pj-notice", role: "status" });
  const list = h("div", { class: "pj-list" });
  const more = h("button", { type: "button", class: "pj-btn pj-more", hidden: true }, "Load older commits");
  root.replaceChildren(h("div", { class: "pj-bar" }, now, refreshButton), banner, error, notice, list, more);
  const modal = createModal(root);
  let model;

  function commitRow(c) {
    const open = model.state.open.has(c.commit);
    const diff = model.state.diffs.get(c.commit);
    return h("div", { class: "pj-commit" },
      h("div", { class: "pj-commithead" },
        h("span", { class: "pj-subject" }, c.subject),
        lineage && lineage.state.available && lineage.state.canSend && lineage.state.targets.length
          ? h("button", { type: "button", class: "pj-btn pj-sm pj-quiet", "data-send": c.commit,
            onclick: () => lineage.chooseRevision(c.commit) }, `${lineage.state.labels.send}…`) : null,
        h("button", { type: "button", class: "pj-btn pj-sm pj-quiet", "aria-expanded": String(open),
          onclick: () => model.toggleDiff(c.commit) }, open ? "Hide diff" : "Diff")),
      h("div", { class: "pj-meta" },
        h("span", { class: `pj-author pj-author-${c.actor || "other"}` }, c.author),
        c.date ? h("span", {}, when(c.date)) : null, h("code", {}, c.commit),
        c.files && c.files.length ? h("span", {}, c.files.join(", ")) : null),
      open ? h("pre", { class: "pj-diff" },
        ...(diff === null ? ["Reading the diff…"] : diffLines(diff || "(no changes)"))) : null);
  }

  function renderModal(st) {
    const p = st.pending;
    if (!p) {
      modal.close();
      return;
    }
    const undone = p.undone;
    const reason = p.stale
      ? "The project changed after you opened this, so it will not be restored as shown. Reload, then choose again."
      : st.busy ? busyText : p.error;
    modal.show({
      heading: `${p.label}?`,
      okLabel: p.label,
      okDisabled: st.busy || st.restoring || p.stale,
      errorText: reason,
      ok: () => model.confirmRestore(),
      closed: () => model.cancelRestore(),
      content: [
        h("p", {}, `The project goes back to how it was before ${p.groupLabel}, as a new commit by you, ` +
          "so nothing is lost and you can restore past this too."),
        h("p", { class: "pj-hint" }, `This undoes ${plural(undone.length, "commit")}${p.incomplete ? " or more" : ""}:`),
        h("ul", {}, ...undone.slice(0, 12).map((c) => h("li", {}, `${c.subject} (${c.author})`)),
          undone.length > 12 ? h("li", {}, `and ${undone.length - 12} more`) : null,
          p.incomplete ? h("li", {}, "older commits of this group are not loaded yet") : null),
      ],
    });
  }

  function render(st) {
    now.textContent = st.headLabel;
    error.textContent = st.error;
    notice.textContent = st.notice;
    more.hidden = !st.nextCursor;
    more.disabled = st.loadingMore;
    refreshButton.disabled = st.loading;
    banner.replaceChildren(...(st.conflict ? [conflictBanner(st.conflict, {
      reload: () => model.reloadAfterConflict(), retry: () => {}, dismiss: () => model.dismissConflict(),
    })] : []));
    list.replaceChildren(...runs(st.commits).map((run) => {
      const group = run.key === null ? null : st.groups.get(run.key);
      const restore = group && group.restore;
      return h("section", { class: "pj-group" },
        h("header", {},
          h("span", { class: "pj-grouplabel" }, group ? group.label : "Other commits"),
          h("span", { class: "pj-count" }, plural(run.commits.length, "commit")),
          restore ? h("button", { type: "button", class: "pj-btn pj-sm", disabled: st.busy,
            title: st.busy ? busyText : restore.label, onclick: () => model.requestRestore(run.key) },
          restore.label) : null),
        ...run.commits.map(commitRow));
    }));
    renderModal(st);
  }

  model = createHistoryModel(ctx, { pageSize, onChange: render, onRestored });
  if (lineage) lineage.subscribe(() => render(model.state));
  refreshButton.addEventListener("click", () => model.refresh());
  more.addEventListener("click", () => model.loadMore());
  return model;
}

/**
 * The title bar of one project in `root`: its title, "Rename" (a field; Enter
 * saves, Escape or leaving puts the name back) and, when `idChange` is given
 * (`{warning}`, what it costs, shown for confirming first), "Change id".
 * `onTitle(name)` and `onRenamed(entry)` tell the page what was saved. The
 * conflict banner shows above the field when a save is refused as stale.
 */
export function initProjectHeader({ ctx, root, title, projectId, idChange = null, onTitle = () => {}, onRenamed = () => {} } = {}) {
  root.classList.add("pj-header");
  const shown = h("strong", { class: "pj-title" }, title || projectId || "");
  const rename = h("button", { type: "button", class: "pj-btn pj-quiet" }, "Rename");
  const idButton = h("button", { type: "button", class: "pj-btn pj-quiet", hidden: !idChange }, "Change id");
  const banner = h("div", { class: "pj-banner" });
  const error = h("div", { class: "pj-error", role: "alert" });
  const row = h("div", { class: "pj-titlerow" }, shown, rename, idButton);
  root.replaceChildren(row, banner, error);
  const modal = createModal(root);
  let field = null;
  let model;

  const labels = {
    title: { label: "Project title", placeholder: "Project title", pattern: null },
    id: { label: "Project id", placeholder: "project_id", pattern: "[a-z0-9][a-z0-9_]*" },
  };

  function render(st) {
    error.textContent = st.error;
    banner.replaceChildren(...(st.conflict ? [conflictBanner(st.conflict, {
      reload: () => model.reloadAfterConflict(), retry: () => model.retryStaged(),
      dismiss: () => model.dismissConflict(),
    })] : []));
    if (field && !st.editing) {
      const gone = field;
      field = null; // before removing it: removing a focused field blurs it
      gone.remove();
    }
    shown.hidden = !!st.editing;
    rename.hidden = !!st.editing;
    idButton.hidden = !idChange || !!st.editing;
    rename.disabled = idButton.disabled = st.saving;
    if (st.editing && !field) {
      const spec = labels[st.editing.kind];
      field = h("input", {
        type: "text", class: "pj-nameedit", maxlength: 200, "aria-label": spec.label,
        value: st.editing.value, placeholder: spec.placeholder, pattern: spec.pattern,
        onkeydown: (event) => {
          if (event.key === "Escape") model.cancelEdit();
          else if (event.key === "Enter") {
            // Not left to the browser: it would press the button focus moves to.
            event.preventDefault();
            finish(field.value);
          }
        },
        onblur: () => {
          if (field && !modal.el.hidden) return; // the id warning has focus now
          model.cancelEdit();
        },
      });
      shown.after(field);
      field.focus();
      field.select();
    }
  }

  function finish(value) {
    const edit = model.state.editing;
    if (edit && edit.kind === "id" && value.trim() && value.trim() !== model.state.projectId) {
      const next = value.trim();
      modal.show({
        heading: `Change the project's id from ${model.state.projectId} to ${next}?`,
        okLabel: "Change id",
        content: [h("p", {}, idChange.warning || "Its address and file names change.")],
        ok: () => {
          model.submit(next); // takes the edit out first, so closing does not cancel it
          modal.close();
        },
        closed: () => model.cancelEdit(),
      });
      return;
    }
    model.submit(value);
  }

  model = createNameModel(ctx, {
    onChange: render,
    onTitle: (name) => {
      shown.textContent = name;
      onTitle(name);
    },
    onRenamed,
  });
  model.state.projectId = projectId;
  rename.addEventListener("click", () => model.beginEdit("title", shown.textContent));
  idButton.addEventListener("click", () => model.beginEdit("id", projectId));
  return model;
}

/**
 * The project list in `root`: a card per project (title, id, last change,
 * state, where it can be opened, why it cannot), a form to start one when the
 * store can, "Change id" on each project when it can. An id change refused as
 * stale keeps the typed id on its card under the conflict banner.
 */
export function initProjectList({ ctx, root, onRenamed = () => {} } = {}) {
  root.classList.add("pj-projects");
  const error = h("div", { class: "pj-error", role: "alert" });
  const form = h("form", { class: "pj-new", hidden: true });
  const cards = h("div", { class: "pj-cards" });
  root.replaceChildren(error, form, cards);
  let model;

  const idField = h("input", { type: "text", name: "id", placeholder: "project_id", "aria-label": "New project id" });
  const titleField = h("input", { type: "text", name: "title", placeholder: "Title", "aria-label": "New project title" });
  const briefField = h("textarea", { name: "brief", rows: "2", placeholder: "What is it for?", "aria-label": "New project brief" });
  const createError = h("div", { class: "pj-error", role: "alert" });
  const submit = h("button", { type: "submit", class: "pj-btn" }, "Start a project");
  form.append(idField, titleField, briefField, submit, createError);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const made = await model.create({ id: idField.value.trim(), title: titleField.value, brief: briefField.value });
    if (made) form.reset();
  });

  function card(entry, st) {
    const conflict = st.conflicts.get(entry.id);
    const editing = st.editing && st.editing.id === entry.id;
    let input = null;
    if (editing) {
      input = h("input", {
        type: "text", class: "pj-nameedit", value: st.editing.value, "aria-label": "Project id",
        pattern: "[a-z0-9][a-z0-9_]*", maxlength: 200,
        oninput: () => {
          if (st.editing) st.editing.value = input.value;
        },
        onkeydown: (e) => {
          if (e.key === "Escape") model.cancelRename();
          else if (e.key === "Enter") {
            e.preventDefault();
            model.submitRename(input.value);
          }
        },
        onblur: () => model.cancelRename(),
      });
      queueMicrotask(() => {
        input.focus();
        input.select();
      });
    }
    const name = entry.title || entry.id;
    return h("article", { class: "pj-card", "data-project": entry.id },
      h("header", {},
        entry.href && !entry.error ? h("a", { href: entry.href, class: "pj-name" }, name)
          : h("span", { class: "pj-name" }, name),
        h("code", {}, entry.id),
        entry.state ? h("span", { class: "pj-state" }, entry.state) : null),
      entry.error ? h("div", { class: "pj-error" }, entry.error) : null,
      h("div", { class: "pj-meta" },
        entry.summary ? h("span", {}, entry.summary) : null,
        entry.last_change ? h("span", {}, entry.last_change) : null,
        entry.updated_at ? h("span", {}, when(entry.updated_at)) : null),
      conflict ? conflictBanner(conflict, {
        reload: () => model.reloadRow(entry.id), retry: () => model.retryRename(entry.id),
        dismiss: () => model.dismissConflict(entry.id),
      }) : null,
      st.rowError.has(entry.id) ? h("div", { class: "pj-error", role: "alert" }, st.rowError.get(entry.id)) : null,
      input || (st.capabilities.rename_id && !entry.error
        ? h("button", { type: "button", class: "pj-btn pj-sm pj-quiet", onclick: () => model.beginRename(entry.id) },
          "Change id") : null));
  }

  function render(st) {
    error.textContent = st.error;
    form.hidden = !st.capabilities.create;
    createError.textContent = st.createError;
    submit.disabled = st.creating;
    cards.replaceChildren(...st.entries.map((e) => card(e, st)));
    if (!st.entries.length && !st.loading && !st.error) {
      cards.replaceChildren(h("p", { class: "pj-empty" }, "No projects here yet."));
    }
  }

  model = createListModel(ctx, { onChange: render, onRenamed });
  return model;
}

/**
 * The send to / derived from section in `root`, absent (hidden, empty) unless
 * the host has lineage. A menu of the host's target products on the project (or
 * on a commit, when `initProjectHistory` is given the returned model), a dialog
 * to pick a new or an existing target project and say why, what was just sent,
 * the refusal banner, and the lists of what this project was derived from and
 * sent to, each entry a link. Someone who may not send sees the menu disabled
 * with the host's reason. `onSent(derivation)` tells the page a send succeeded.
 * Returns the model (`refresh()`, `chooseRevision(commit)`, `subscribe(fn)`).
 */
export function initProjectLineage({ ctx, root, heading = "Lineage", onSent = () => {} } = {}) {
  root.classList.add("pj-lineage");
  root.hidden = true;
  const title = h("h3", { class: "pj-lineage-title" }, heading);
  const menuButton = h("button", { type: "button", class: "pj-btn pj-send", "aria-haspopup": "menu", "aria-expanded": "false" });
  const menu = h("div", { class: "pj-menu", role: "menu", hidden: true });
  const wrap = h("div", { class: "pj-menuwrap" }, menuButton, menu);
  const revision = h("span", { class: "pj-revnote", hidden: true });
  const why = h("span", { class: "pj-notice", role: "status" });
  const error = h("div", { class: "pj-error", role: "alert" });
  const banner = h("div", { class: "pj-banner" });
  const result = h("div", { class: "pj-result", role: "status" });
  const derivedBox = h("section", { class: "pj-links pj-derived" });
  const sentBox = h("section", { class: "pj-links pj-sent" });
  root.replaceChildren(h("div", { class: "pj-bar" }, title, wrap, revision, why), error, banner, result, derivedBox, sentBox);
  const modal = createModal(root);
  // Built, but not in the page until the host has shown it has lineage: with none the root is empty.
  const parts = [...root.children];
  root.replaceChildren();
  let mounted = false;
  let model;
  let wasOpen = false;

  function renderDraft(st) {
    const d = st.draft;
    const target = d && st.targets.find((t) => t.product === d.product);
    if (!d || !target) {
      modal.close();
      return;
    }
    const existing = target.existing || [];
    const nowhere = target.can_create === false && !existing.length;
    const choice = (value, label, detail) => {
      const input = h("input", { type: "radio", name: "pj-target-project", checked: d.project === value,
        onchange: () => model.setProject(value) });
      return h("label", { class: "pj-choice" }, input, h("span", {}, label),
        detail ? h("small", { class: "pj-hint" }, detail) : null);
    };
    const intent = h("textarea", { rows: "3", maxlength: 2000, "aria-label": "Why you are sending it (optional)",
      placeholder: "Why? (optional)", oninput: () => model.setIntent(intent.value) });
    intent.value = d.intent;
    modal.show({
      heading: `${st.labels.send} ${target.label}`,
      okLabel: d.sending ? "Sending…" : `${st.labels.send} ${target.label}`,
      okDisabled: d.sending || !d.revision || nowhere,
      errorText: nowhere ? `There is no ${target.label} project to send this to.`
        : !d.revision ? "This project has no revision to send yet." : d.error,
      ok: () => model.confirm(),
      closed: () => model.cancelDraft(),
      content: [
        h("p", {}, "Revision ", h("code", {}, d.revision || "(none)")),
        h("fieldset", { class: "pj-choices" },
          target.can_create === false ? null : choice(null, target.new_label || "A new project", null),
          ...existing.map((p) => choice(p.id, p.title || p.id, p.summary))),
        intent,
      ],
    });
  }

  function render(st) {
    root.hidden = st.available !== true && !st.error;
    if (st.available !== true) {
      mounted = false;
      error.textContent = st.error;
      root.replaceChildren(...(st.error ? [error] : []));
      return;
    }
    if (!mounted) {
      root.replaceChildren(...parts);
      mounted = true;
    }
    error.textContent = st.error;
    title.textContent = heading;
    const usable = st.targets.length > 0;
    menuButton.textContent = `${st.labels.send}…`;
    menuButton.disabled = !st.canSend || !usable;
    menuButton.setAttribute("aria-expanded", String(st.menuOpen));
    menuButton.title = !st.canSend ? st.disabledReason : "";
    why.textContent = st.available !== true ? ""
      : !st.canSend ? st.disabledReason || "You may not send this project."
        : !usable ? "There is nowhere to send this project yet." : "";
    revision.hidden = !st.revision;
    revision.replaceChildren(...(st.revision ? [
      "revision ", h("code", {}, st.revision), " ",
      h("button", { type: "button", class: "pj-btn pj-sm pj-quiet", onclick: () => model.chooseRevision(null) },
        "Use the latest"),
    ] : []));
    menu.hidden = !st.menuOpen;
    menu.replaceChildren(...st.targets.map((t) => h("button", {
      type: "button", role: "menuitem", class: "pj-menuitem", "data-product": t.product,
      disabled: !!t.unavailable, title: t.unavailable || null, onclick: () => model.begin(t.product),
    }, t.label)));
    banner.replaceChildren(...(st.conflict ? [sendBanner(st.conflict, {
      reload: () => model.reloadAfterConflict(), again: () => model.sendAgain(),
      dismiss: () => model.dismissConflict(),
    })] : []));
    const r = st.result;
    result.hidden = !r;
    result.replaceChildren(...(r ? [
      h("span", {}, r.created === false
        ? `This had already been sent to ${r.label}: ` : `${st.labels.sent} ${r.label}: `),
      r.href ? h("a", { href: r.href, class: "pj-name" }, r.target_title || r.target_project)
        : h("b", {}, r.target_title || r.target_project),
      r.state ? h("span", { class: "pj-state" }, r.state) : null,
      r.note ? h("span", { class: "pj-hint" }, r.note) : null,
      h("button", { type: "button", class: "pj-btn pj-sm pj-quiet", onclick: () => model.dismissResult() }, "Dismiss"),
    ] : []));
    for (const [box, label, links, forward] of [
      [derivedBox, st.labels.derived, st.derivedFrom, false], [sentBox, st.labels.sent, st.sentTo, true],
    ]) {
      box.replaceChildren(h("h4", {}, label),
        links.length ? h("ul", {}, ...links.map((l) => lineageItem(l, forward)))
          : h("p", { class: "pj-empty" }, "Nothing yet."));
    }
    renderDraft(st);
    if (st.menuOpen && !wasOpen && root.scrollIntoView) root.scrollIntoView({ block: "nearest" });
    wasOpen = st.menuOpen;
  }

  model = createLineageModel(ctx, { onChange: render });
  let lastResult = null;
  model.subscribe((st) => {
    if (st.result && st.result !== lastResult) onSent(st.result);
    lastResult = st.result;
  });
  menuButton.addEventListener("click", () => model.toggleMenu());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && model.state.menuOpen) model.toggleMenu(false);
  });
  document.addEventListener("click", (e) => {
    if (model.state.menuOpen && !wrap.contains(e.target) && !e.target.closest("[data-send]")) model.toggleMenu(false);
  });
  return model;
}
