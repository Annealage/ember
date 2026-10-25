"""Project management over a store the product (or the hosted service) owns:
what ``static/project.js`` shows and posts, and the contract a store meets.

This is a projection and a staged-edit contract, not a store. A product's
project is held somewhere with its own rules (Loom's is a git repository with
a workflow ledger, a hosted project is the hosted ProjectStore's git
repository behind its single writer fence); this module never reads or writes
that content. It says what a project list, a title, a history page and a
restore look like on the wire, which errors a store raises and how each reaches
the page, and registers the routes. The store stays the one authority for what
the project is and for every compare-and-swap.

**The contract.** Two protocols, which one object (a hosted store) may meet
both of:

- ``ProjectDirectory``: ``list_projects``, ``create`` and ``rename`` (the id).
- ``ProjectHistory``: ``set_title``, ``history``, ``commit_diff`` and
  ``restore`` for one project id.

Every method is a coroutine; a store with blocking work (Loom's git calls)
moves it off the event loop itself. ``capabilities`` says which of the
operations the store does, so a page hides the others and the routes refuse
them in one voice (``Unsupported``, 404).

**Compare-and-swap, visible.** A project has an opaque ``etag``: the store's
token for the state a reader saw. It comes with every ``ProjectEntry``, every
``HistoryPage`` and every successful change. Each change (``rename``,
``set_title``, ``restore``) names the etag it was made against in
``expected_etag``, and the route refuses a request without one (400): there is
no unconditional write. A store that finds the project moved on raises
``Conflict(current_etag=...)``, which the route answers 409 with
``conflict: true`` and ``current_etag``, and the page shows (the staged edit is
kept, the change is never retried behind the person's back). A store whose
token is composite (Loom's is three numbers and a hash) packs it into one
string and unpacks it itself; the page never looks inside.

**History.** ``history(project_id, limit=, cursor=)`` returns the newest
commits first, ``next_cursor`` ``None`` at the end. A cursor is the store's
opaque string for "after the last commit of this page"; a store that pages by
offset makes a cursor stale once a commit lands, which is why every page
carries the etag it was read at and the page refetches from the top when a
later page's etag differs. A commit may name a ``group`` (Loom: the agent turn
it was made in); the page's ``groups`` list gives each group its label and, if
the project can be put back to how it was before that group, the opaque
``restore_target`` the page sends back to ``restore``. A restore is a new
commit by the person, never a rewrite, and a no-op restore says so.

**Routes**, registered by ``register_project_routes`` on a product's app, each
behind the app's ``BrowserAuth`` and refusing with the same opaque answer as
``/ws`` (``http.ws.refusal``). Reads need ``project.read`` and changes
``project.write`` (``operations`` replaces either).

Directory (``directory=`` given)::

    GET  /api/projects               {ok, projects: [entry...], capabilities}
    POST /api/projects               {id?, title?, brief?} -> {ok, project: entry}
    POST /api/projects/<id>/rename   {id: new, expected_etag} -> {ok, project: entry}

History, bound to one project (``project_id=`` given, a string or a callable of
the request, for a page that is one project's)::

    GET  /api/history                ?limit=&cursor= -> {ok, project_id, etag,
                                     head_label, commits, groups, next_cursor}
    GET  /api/history/<commit>       {ok, commit, diff}
    POST /api/restore                {target, expected_etag} -> {ok, etag, commit,
                                     changes, note, noop}
    POST /api/project/name           {name, expected_etag} -> {ok, name, etag, commit}

or, with no ``project_id``, the same four under ``/api/projects/<id>/history``,
``/history/<commit>``, ``/restore`` and ``/name``, for a page that lists
projects.

Errors are ``{ok: false, error}`` with 400 (``Invalid``), 403 (``Forbidden``,
``forbidden: true``, which the page tells from the opaque authentication
refusal, a plain-text ``forbidden``), 404 (``NotFound``, ``Unsupported``), 409
(``Conflict``, ``Busy``; ``conflict``/``busy`` flags say which) or 500
(``StoreFailed``). A store's own message is shown to the person, so it says why
and what to do; anything else a store raises is a bug and is not caught here.

**Send to / derived from (optional).** A host that can hand a project's
revision to another product, and knows where each project came from, meets a
third protocol, ``ProjectLineage``, and passes it as ``lineage=`` to
``register_project_routes``. The page's ``initProjectLineage`` then shows a
"send" menu on the project (and, when the history pane is given the same model,
on each commit) and two lists, what this project was derived from and what it
was sent to, each entry a link. A host that does not implement it (Canvas
today) registers nothing: the routes answer 404, the page finds that out on its
first read and shows nothing at all. Nothing here names a product: the
products, the words on the menu and the lists, and the relations are the host's
(``SendTarget.label``, ``SendOptions.send_label`` ..., ``LineageLink.relation_label``).

- ``send_options(project_id, actor=)`` returns ``SendOptions``: the products the
  project may be sent to (each ``SendTarget`` names the product, its label, the
  existing projects of that product a send may extend and whether it may start a
  new one), whether *this person* may send at all (``can_send``; a viewer gets
  ``False`` with ``disabled_reason``, and the menu is shown disabled saying
  why), and ``head_revision``, the revision "the project as it is now" means.
- ``derive(project_id, revision, target_product=, target_project=, intent=,
  actor=)`` makes the derivation from that source revision into a new
  (``target_project=None``) or an existing project of the target product, and
  returns a ``DeriveResult`` (an identical request returns the existing one,
  ``created=False``). It is the host's to authorise and pin: a store with a
  workspace boundary and roles raises ``Forbidden`` (one answer for a viewer, a
  missing project or another workspace; its message is shown), ``Busy`` or
  ``Conflict`` (the target is held or moved on, 409), ``Invalid`` (400).
- ``lineage(project_id, actor=)`` returns ``Lineage``: ``derived_from`` (the
  projects this one's revisions came from) and ``sent_to`` (the projects made
  from this one's), both as ``LineageLink`` with an ``href`` to open it.

Lineage routes, bound (``project_id=`` given) or with the id in the path::

    GET  /api/lineage                 {ok, project_id, can_send, disabled_reason,
    GET  /api/projects/<id>/lineage    head_revision, send_label, derived_label,
                                       sent_label, targets, derived_from, sent_to}
    POST /api/send                    {revision, target_product, target_project
    POST /api/projects/<id>/send       (a project id, or null for a new one),
                                       intent?} -> {ok, derivation: DeriveResult}

Reads need ``project.read`` and a send ``project.derive`` (``operations``
names it; it defaults to the write operation). The page never retries a send
that was refused: a 409 stays on the page as a banner with the request kept,
"Reload" reads the options and lineage again, and sending again is the
person's own click.
"""

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional, Protocol, Union
from urllib.parse import unquote

from .http import read_json_body
from .http.ws import refusal
from .identity import Human

#: The most commits one page may carry, and the default.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100

#: Longest opaque string a request may name (an etag, a cursor, a restore
#: target, a commit): every one comes from the store, and none is long.
MAX_TOKEN = 256
#: Longest title, id or brief a request may carry; the store validates content.
MAX_TITLE = 200
MAX_BRIEF = 20_000
#: Longest intent (the sender's words with a derivation) a request may carry.
MAX_INTENT = 2_000

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --- errors a store raises ------------------------------------------------------------


class ProjectStoreError(Exception):
    """A refusal or failure whose message is for the person to read."""


class NotFound(ProjectStoreError):
    """No such project or commit, or one the caller may not know exists."""


class Invalid(ProjectStoreError):
    """The request is refused as asked (an id that is not one, a target that is
    nothing): the message says why and what to do."""


class Unsupported(ProjectStoreError):
    """The store does not do this (``Capabilities`` says so)."""


class Busy(ProjectStoreError):
    """Refused while something else holds the project (an agent turn, a build,
    another writer): the same request will do when it is over."""


class Conflict(ProjectStoreError):
    """``expected_etag`` is not the project's: it changed after the page read
    it. ``current_etag`` is the store's token now, so the page can reload."""

    def __init__(self, message: str, current_etag: Optional[str] = None):
        super().__init__(message)
        self.current_etag = current_etag


class Forbidden(ProjectStoreError):
    """The caller may not do this (a viewer sending a project on, a target in
    another workspace). One message for every cause: it does not say which
    thing exists. Answered 403 with ``forbidden: true``, so the page does not
    take it for a stale link."""


class StoreFailed(ProjectStoreError):
    """The store itself failed (a broken repository, a full disk), not a
    refusal; answered 500."""


# --- what crosses the wire ------------------------------------------------------------


def _wire(value: Any) -> Any:
    """A dataclass as the JSON the page reads: its fields, tuples as lists,
    nested dataclasses likewise, ``extra`` merged in last-wins-never."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _wire(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_wire(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _wire(v) for k, v in value.items()}
    return value


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """What a store does. A page hides the rest; a route refuses it."""

    create: bool = False
    rename_id: bool = False
    title: bool = False
    restore: bool = False
    diff: bool = True


@dataclasses.dataclass(frozen=True)
class ProjectEntry:
    """One project in a list. ``etag`` is the token a rename of it is made
    against. ``title`` is what to call it, ``summary`` one line about its
    state (a stage, a part number), ``last_change``/``updated_at`` the last
    commit's subject and ISO time, ``state`` how it is served, ``href`` where
    to open it, ``error`` why it is listed but cannot be opened. ``extra`` is
    the product's own fields, passed through for a page that knows them."""

    id: str
    etag: str = ""
    title: str = ""
    summary: str = ""
    last_change: str = ""
    updated_at: str = ""
    state: str = ""
    href: Optional[str] = None
    error: Optional[str] = None
    extra: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Commit:
    """One commit of the history. ``commit`` is the id ``commit_diff`` takes,
    ``actor`` is ``"agent"`` or ``"human"`` (anything else is shown plain),
    ``group`` the key of the ``Group`` it belongs to."""

    commit: str
    subject: str
    author: str = ""
    date: str = ""
    files: tuple[str, ...] = ()
    group: Optional[str] = None
    actor: str = ""


@dataclasses.dataclass(frozen=True)
class Group:
    """A run of commits made together (Loom: one agent turn), with what to call
    it and, when the project can be put back to how it was before the group
    began, ``restore_target``: the opaque string ``restore`` takes, labelled
    ``restore_label`` on its button."""

    key: str
    label: str
    restore_target: Optional[str] = None
    restore_label: str = ""


@dataclasses.dataclass(frozen=True)
class HistoryPage:
    """A page of history, newest commit first. ``etag`` is the project's token
    when the page was read; ``head_label`` a short statement of where the
    project stands now ("Turn now 4")."""

    commits: tuple[Commit, ...]
    etag: str
    groups: tuple[Group, ...] = ()
    head_label: str = ""
    next_cursor: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class TitleResult:
    """A title change: the title now in force, the new etag, the commit made
    (``None`` when it was already so)."""

    title: str
    etag: str
    commit: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class RestoreResult:
    """A restore: the new etag, the commit it made (``None`` and ``noop`` when
    the project was already so), the changed paths and a line for the person."""

    etag: str
    commit: Optional[str] = None
    changes: tuple[str, ...] = ()
    note: str = ""

    @property
    def noop(self) -> bool:
        return self.commit is None


@dataclasses.dataclass(frozen=True)
class TargetProject:
    """An existing project of the target product that a send may extend."""

    id: str
    title: str = ""
    summary: str = ""


@dataclasses.dataclass(frozen=True)
class SendTarget:
    """One product a project may be sent to. ``product`` is the id
    ``derive`` takes, ``label`` what to call it on the menu, ``new_label`` the
    words for starting a new project of it, ``existing`` the projects of it a
    send may extend. ``unavailable`` is why this product cannot be chosen now
    (empty: it can)."""

    product: str
    label: str
    can_create: bool = True
    new_label: str = ""
    existing: tuple[TargetProject, ...] = ()
    unavailable: str = ""


@dataclasses.dataclass(frozen=True)
class SendOptions:
    """What the menu offers *this person* for one project. ``can_send`` is
    false, with ``disabled_reason`` saying why, for someone who may look but
    not send (a viewer). ``head_revision`` is what "the project as it is now"
    means (empty: nothing to send yet). The three labels are the host's words
    for the menu and the two lists."""

    targets: tuple[SendTarget, ...] = ()
    can_send: bool = True
    disabled_reason: str = ""
    head_revision: str = ""
    send_label: str = "Send to"
    derived_label: str = "Derived from"
    sent_label: str = "Sent to"


@dataclasses.dataclass(frozen=True)
class DeriveResult:
    """A derivation made (or, ``created`` false, one that already existed for
    the identical request). ``job_id``/``state`` say where its work stands,
    ``target_*`` where it went (``new``: the project was started by it) and
    ``href`` where to open that."""

    target_project: str
    target_product: str = ""
    target_title: str = ""
    target_revision: str = ""
    new: bool = False
    created: bool = True
    job_id: str = ""
    state: str = ""
    href: Optional[str] = None
    note: str = ""


@dataclasses.dataclass(frozen=True)
class LineageLink:
    """The other end of one lineage edge. ``project`` (with ``title``,
    ``product_label``, ``href``) is the other project, ``revision`` its revision
    and ``local_revision`` this project's revision the edge is at.
    ``relation`` is the host's id for how they relate and ``relation_label``
    what to call it (``intent`` is the sender's own words); ``state`` is the
    work's, if it has not finished."""

    project: str
    title: str = ""
    product: str = ""
    product_label: str = ""
    relation: str = ""
    relation_label: str = ""
    revision: str = ""
    local_revision: str = ""
    intent: str = ""
    created_at: str = ""
    state: str = ""
    href: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Lineage:
    """Where a project came from and where it went, newest first."""

    derived_from: tuple[LineageLink, ...] = ()
    sent_to: tuple[LineageLink, ...] = ()


# --- the contract ---------------------------------------------------------------------


class ProjectDirectory(Protocol):
    """Many projects, side by side: what a project list shows and starts."""

    capabilities: Capabilities

    async def list_projects(self) -> Sequence[ProjectEntry]: ...

    async def create(
        self, project_id: Optional[str], *, title: str, brief: str, actor: Human
    ) -> ProjectEntry:
        """A new project, committed as ``actor``. ``project_id`` is ``None``
        when the store names its projects itself."""

    async def rename(
        self, project_id: str, new_id: str, *, expected_etag: str, actor: Human
    ) -> ProjectEntry:
        """Change the project's id (not its title), against ``expected_etag``."""


class ProjectHistory(Protocol):
    """One project's name and commits, for any project id the store holds."""

    capabilities: Capabilities

    async def set_title(
        self, project_id: str, title: str, *, expected_etag: str, actor: Human
    ) -> TitleResult:
        """Set (empty clears) the title, against ``expected_etag``."""

    async def history(
        self, project_id: str, *, limit: int, cursor: Optional[str] = None
    ) -> HistoryPage: ...

    async def commit_diff(self, project_id: str, commit: str) -> str: ...

    async def restore(
        self, project_id: str, target: str, *, expected_etag: str, actor: Human
    ) -> RestoreResult:
        """Put the project back to ``target`` (a ``Group.restore_target``) as a
        new commit by ``actor``, against ``expected_etag``."""


class ProjectLineage(Protocol):
    """Optional: sending a project's revision to another product and the
    record of where each project came from. Absent for a host that has none."""

    async def send_options(self, project_id: str, *, actor: Human) -> SendOptions: ...

    async def derive(
        self,
        project_id: str,
        revision: str,
        *,
        target_product: str,
        target_project: Optional[str],
        intent: str,
        actor: Human,
    ) -> DeriveResult:
        """Derive ``revision`` of ``project_id`` into the target product:
        ``target_project`` is an existing project's id, or ``None`` for a new
        one. Success-only: a refused or cancelled derivation leaves the target
        as it was."""

    async def lineage(self, project_id: str, *, actor: Human) -> Lineage: ...


# --- routes ---------------------------------------------------------------------------

#: Operation names the routes authenticate with (``identity.HOSTED_OPERATIONS``).
DEFAULT_OPERATIONS = {"read": "project.read", "write": "project.write"}

ProjectId = Union[str, Callable[[Any], str]]


def _fail(exc: ProjectStoreError):
    body = {"ok": False, "error": str(exc)}
    if isinstance(exc, Conflict):
        body.update(conflict=True, current_etag=exc.current_etag)
        return body, 409
    if isinstance(exc, Busy):
        body["busy"] = True
        return body, 409
    if isinstance(exc, Forbidden):
        body["forbidden"] = True
        return body, 403
    if isinstance(exc, Unsupported):
        body["unsupported"] = True
        return body, 404
    if isinstance(exc, NotFound):
        return body, 404
    if isinstance(exc, Invalid):
        return body, 400
    return body, 500


def _bad(message: str):
    return {"ok": False, "error": message}, 400


def _token(value: Any, what: str):
    """``(value, None)`` for an opaque string a store handed the page, else
    ``(None, 400 response)``."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_TOKEN or _CONTROL.search(value):
        return None, _bad(
            "expected %s (a non-empty string of at most %d characters)" % (what, MAX_TOKEN)
        )
    return value, None
    return value, None


def _text(value: Any, what: str, limit: int, *, allow_empty: bool = True):
    if not isinstance(value, str) or len(value) > limit or (not value and not allow_empty):
        return None, _bad("expected %s (a string of at most %d characters)" % (what, limit))
    return value, None
    return value, None


def register_project_routes(
    app,
    *,
    auth,
    directory: Optional[ProjectDirectory] = None,
    history: Optional[ProjectHistory] = None,
    lineage: Optional[ProjectLineage] = None,
    project_id: Optional[ProjectId] = None,
    operations: Optional[Mapping[str, str]] = None,
) -> None:
    """Register the project routes (module docstring) on ``app`` over a
    ``directory`` and/or a ``history`` store (and, optionally, a ``lineage``
    one: send to / derived from), gated by ``auth``
    (``identity.BrowserAuth``). With ``project_id`` the history routes are the
    page's own project's (``/api/history``); without it they take the id in
    the path. Call it from the product's ``register_routes``, where the
    product's own routes are registered."""
    ops = {**DEFAULT_OPERATIONS, **(operations or {})}

    def reader(req):
        return auth.authenticate(req, ops["read"])

    def writer(req):
        return auth.authenticate(req, ops["write"])

    def deriver(req):
        return auth.authenticate(req, ops.get("derive", ops["write"]))

    def capability(store, name):
        if store is None or not getattr(store.capabilities, name):
            return _fail(Unsupported("this project store does not do that"))
        return None

    async def body_of(req):
        data, refused = await read_json_body(req)
        if refused is not None:
            return None, refused
        if not isinstance(data, dict):
            return None, _bad("expected a JSON object")
        return data, None

    def resolve(req, pid):
        if project_id is None:
            return unquote(pid)
        return project_id(req) if callable(project_id) else project_id

    if directory is not None:

        @app.get("/api/projects")
        async def list_projects(req):
            if reader(req) is None:
                return refusal()
            try:
                entries = await directory.list_projects()
            except ProjectStoreError as exc:
                return _fail(exc)
            return {
                "ok": True,
                "projects": [_wire(entry) for entry in entries],
                "capabilities": _wire(directory.capabilities),
            }, 200

        @app.post("/api/projects")
        async def create_project(req):
            human = writer(req)
            if human is None:
                return refusal()
            if (unsupported := capability(directory, "create")) is not None:
                return unsupported
            data, refused = await body_of(req)
            if refused is not None:
                return refused
            new_id = data.get("id")
            if new_id is not None:
                new_id, bad = _text(new_id, "id", MAX_TITLE, allow_empty=False)
                if bad:
                    return bad
            title, bad = _text(data.get("title", ""), "title", MAX_TITLE)
            if bad:
                return bad
            brief, bad = _text(data.get("brief", ""), "brief", MAX_BRIEF)
            if bad:
                return bad
            try:
                entry = await directory.create(new_id, title=title, brief=brief, actor=human)
            except ProjectStoreError as exc:
                return _fail(exc)
            return {"ok": True, "project": _wire(entry)}, 200

        @app.post("/api/projects/<pid>/rename")
        async def rename_project(req, pid):
            human = writer(req)
            if human is None:
                return refusal()
            if (unsupported := capability(directory, "rename_id")) is not None:
                return unsupported
            data, refused = await body_of(req)
            if refused is not None:
                return refused
            new_id, bad = _text(
                data.get("id"), "id, the project's new id", MAX_TITLE, allow_empty=False
            )
            if bad:
                return bad
            etag, bad = _token(data.get("expected_etag"), "expected_etag")
            if bad:
                return bad
            try:
                entry = await directory.rename(
                    unquote(pid), new_id, expected_etag=etag, actor=human
                )
            except ProjectStoreError as exc:
                return _fail(exc)
            return {"ok": True, "project": _wire(entry)}, 200

    if lineage is not None:

        async def get_lineage(req, pid=None):
            human = reader(req)
            if human is None:
                return refusal()
            which = resolve(req, pid)
            try:
                options = await lineage.send_options(which, actor=human)
                found = await lineage.lineage(which, actor=human)
            except ProjectStoreError as exc:
                return _fail(exc)
            return {
                "ok": True,
                "project_id": which,
                "can_send": options.can_send,
                "disabled_reason": options.disabled_reason,
                "head_revision": options.head_revision,
                "send_label": options.send_label,
                "derived_label": options.derived_label,
                "sent_label": options.sent_label,
                "targets": [_wire(t) for t in options.targets],
                "derived_from": [_wire(link) for link in found.derived_from],
                "sent_to": [_wire(link) for link in found.sent_to],
            }, 200

        async def post_send(req, pid=None):
            human = deriver(req)
            if human is None:
                return refusal()
            data, refused = await body_of(req)
            if refused is not None:
                return refused
            revision, bad = _token(data.get("revision"), "revision, the one to send")
            if bad:
                return bad
            product, bad = _token(data.get("target_product"), "target_product")
            if bad:
                return bad
            target = data.get("target_project")
            if target is not None:
                target, bad = _token(target, "target_project (a project id, or null for a new one)")
                if bad:
                    return bad
            intent, bad = _text(data.get("intent", ""), "intent", MAX_INTENT)
            if bad:
                return bad
            try:
                result = await lineage.derive(
                    resolve(req, pid),
                    revision,
                    target_product=product,
                    target_project=target,
                    intent=intent,
                    actor=human,
                )
            except ProjectStoreError as exc:
                return _fail(exc)
            return {"ok": True, "derivation": _wire(result)}, 200

        if project_id is not None:
            app.get("/api/lineage")(get_lineage)
            app.post("/api/send")(post_send)
        else:
            app.get("/api/projects/<pid>/lineage")(get_lineage)
            app.post("/api/projects/<pid>/send")(post_send)

    if history is None:
        return

    async def get_history(req, pid=None):
        if reader(req) is None:
            return refusal()
        try:
            limit = min(max(int(req.args.get("limit", DEFAULT_LIMIT)), 1), MAX_LIMIT)
        except ValueError:
            limit = DEFAULT_LIMIT
        cursor = req.args.get("cursor")
        if cursor is not None:
            cursor, bad = _token(cursor, "cursor")
            if bad:
                return bad
        which = resolve(req, pid)
        try:
            page = await history.history(which, limit=limit, cursor=cursor)
        except ProjectStoreError as exc:
            return _fail(exc)
        return {
            "ok": True,
            "project_id": which,
            "etag": page.etag,
            "head_label": page.head_label,
            "commits": [_wire(c) for c in page.commits],
            "groups": [
                {
                    "key": g.key,
                    "label": g.label,
                    "restore": {"target": g.restore_target, "label": g.restore_label}
                    if g.restore_target is not None
                    else None,
                }
                for g in page.groups
            ],
            "next_cursor": page.next_cursor,
        }, 200

    async def get_diff(req, commit, pid=None):
        if reader(req) is None:
            return refusal()
        if (unsupported := capability(history, "diff")) is not None:
            return unsupported
        commit, bad = _token(unquote(commit), "commit")
        if bad:
            return bad
        try:
            diff = await history.commit_diff(resolve(req, pid), commit)
        except ProjectStoreError as exc:
            return _fail(exc)
        return {"ok": True, "commit": commit, "diff": diff}, 200

    async def post_restore(req, pid=None):
        human = writer(req)
        if human is None:
            return refusal()
        if (unsupported := capability(history, "restore")) is not None:
            return unsupported
        data, refused = await body_of(req)
        if refused is not None:
            return refused
        target, bad = _token(data.get("target"), "target, what history offered to restore to")
        if bad:
            return bad
        etag, bad = _token(data.get("expected_etag"), "expected_etag, the etag history was read at")
        if bad:
            return bad
        try:
            result = await history.restore(
                resolve(req, pid), target, expected_etag=etag, actor=human
            )
        except ProjectStoreError as exc:
            return _fail(exc)
        return {
            "ok": True,
            "etag": result.etag,
            "commit": result.commit,
            "changes": list(result.changes),
            "note": result.note,
            "noop": result.noop,
        }, 200

    async def post_title(req, pid=None):
        human = writer(req)
        if human is None:
            return refusal()
        if (unsupported := capability(history, "title")) is not None:
            return unsupported
        data, refused = await body_of(req)
        if refused is not None:
            return refused
        name, bad = _text(data.get("name"), "name, the title (empty clears it)", MAX_TITLE)
        if bad:
            return bad
        etag, bad = _token(data.get("expected_etag"), "expected_etag")
        if bad:
            return bad
        try:
            result = await history.set_title(
                resolve(req, pid), name, expected_etag=etag, actor=human
            )
        except ProjectStoreError as exc:
            return _fail(exc)
        return {
            "ok": True,
            "name": result.title,
            "etag": result.etag,
            "commit": result.commit,
        }, 200

    if project_id is not None:
        app.get("/api/history")(get_history)
        app.get("/api/history/<commit>")(get_diff)
        app.post("/api/restore")(post_restore)
        app.post("/api/project/name")(post_title)
    else:
        app.get("/api/projects/<pid>/history")(get_history)

        async def diff_in_path(req, pid, commit):
            return await get_diff(req, commit, pid)

        app.get("/api/projects/<pid>/history/<commit>")(diff_in_path)
        app.post("/api/projects/<pid>/restore")(post_restore)
        app.post("/api/projects/<pid>/name")(post_title)


__all__ = [
    "Busy",
    "Capabilities",
    "Commit",
    "Conflict",
    "DEFAULT_LIMIT",
    "DeriveResult",
    "Forbidden",
    "Group",
    "HistoryPage",
    "Invalid",
    "Lineage",
    "LineageLink",
    "MAX_LIMIT",
    "NotFound",
    "ProjectDirectory",
    "ProjectEntry",
    "ProjectHistory",
    "ProjectLineage",
    "ProjectStoreError",
    "RestoreResult",
    "SendOptions",
    "SendTarget",
    "StoreFailed",
    "TargetProject",
    "TitleResult",
    "Unsupported",
    "register_project_routes",
]
