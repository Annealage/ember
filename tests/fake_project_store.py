"""An in-memory store meeting both project protocols (``projects.py``), for the
route tests, the page-client tests and the browser smoke.

Each project keeps its commits (oldest first) with the files each left, and an
``etag`` that moves on every change, so a test makes the store refuse a stale
write by changing the project behind a page's back (``commit``). Commits carry
a ``turn``; history groups by it as Loom's does, and ``turn:N`` is the target
that puts the project back to before turn N. A cursor is an offset, so a
commit made between two pages makes the second stale, the case the page
guards.
"""

from annealage_ember import projects as pj


class FakeProjects:
    capabilities = pj.Capabilities(create=True, rename_id=True, title=True, restore=True)

    def __init__(self):
        self.projects = {}
        self.busy = False
        self.calls = []
        self._next = 0

    # -- helpers a test drives the store with ----------------------------------

    def add(self, project_id, title="", **files):
        self.projects[project_id] = {"title": title, "version": 0, "commits": [], "tree": {}}
        self.commit(project_id, "start the project", author="Annie", actor="human", turn=0, **files)

    def commit(self, project_id, subject, *, author="Annie", actor="human", turn=None, **files):
        """Another writer's change: the project moves on, its etag with it."""
        project = self.projects[project_id]
        project["version"] += 1
        project["tree"] = {**project["tree"], **files}
        project["commits"].append(
            {
                "id": "c%03d" % len(project["commits"]),
                "subject": subject,
                "author": author,
                "actor": actor,
                "turn": turn,
                "files": tuple(sorted(files)),
                "date": "2026-10-24T10:%02d:00+00:00" % len(project["commits"]),
                "tree": dict(project["tree"]),
            }
        )

    def etag(self, project_id):
        return "v%d" % self.projects[project_id]["version"]

    # -- the protocols -----------------------------------------------------------

    def _get(self, project_id):
        try:
            return self.projects[project_id]
        except KeyError:
            raise pj.NotFound("there is no project %s" % project_id) from None

    def _check(self, project_id, expected_etag):
        self._get(project_id)
        if expected_etag != self.etag(project_id):
            raise pj.Conflict("the project changed after it was read", self.etag(project_id))

    def _entry(self, project_id):
        project = self.projects[project_id]
        last = project["commits"][-1]
        return pj.ProjectEntry(
            id=project_id,
            etag=self.etag(project_id),
            title=project["title"] or project_id,
            last_change=last["subject"],
            updated_at=last["date"],
            href="p/%s/" % project_id,
        )

    async def list_projects(self):
        self.calls.append(("list",))
        return [self._entry(p) for p in sorted(self.projects)]

    async def create(self, project_id, *, title, brief, actor):
        self.calls.append(("create", project_id, title, brief, actor.login))
        if project_id is None:
            self._next += 1
            project_id = "prj_%d" % self._next
        if project_id in self.projects:
            raise pj.Invalid("there is already a project called %s" % project_id)
        self.add(project_id, title, **({"brief.md": brief} if brief else {}))
        return self._entry(project_id)

    async def rename(self, project_id, new_id, *, expected_etag, actor):
        self.calls.append(("rename", project_id, new_id, expected_etag))
        self._check(project_id, expected_etag)
        if new_id in self.projects:
            raise pj.Invalid("%s is taken" % new_id)
        self.projects[new_id] = self.projects.pop(project_id)
        self.commit(new_id, "rename to %s" % new_id, author=actor.label or "user")
        return self._entry(new_id)

    async def set_title(self, project_id, title, *, expected_etag, actor):
        self.calls.append(("title", project_id, title, expected_etag))
        if self.busy:
            raise pj.Busy("the agent is working on a turn")
        self._check(project_id, expected_etag)
        project = self.projects[project_id]
        if title == project["title"]:
            return pj.TitleResult(title or project_id, self.etag(project_id))
        project["title"] = title
        self.commit(project_id, "project: name %r" % title, author=actor.label or "user")
        return pj.TitleResult(title or project_id, self.etag(project_id), self._head(project_id))

    def _head(self, project_id):
        return self.projects[project_id]["commits"][-1]["id"]

    async def history(self, project_id, *, limit, cursor=None):
        self.calls.append(("history", project_id, limit, cursor))
        project = self._get(project_id)
        newest_first = project["commits"][::-1]
        try:
            offset = int(cursor) if cursor is not None else 0
        except ValueError:
            raise pj.Invalid("that cursor is not one history gave") from None
        page = newest_first[offset : offset + limit]
        turns = sorted({c["turn"] for c in page if c["turn"] is not None}, reverse=True)
        groups = [
            pj.Group(
                "turn:%d" % t,
                "Turn %d" % t if t else "Before the first turn",
                restore_target="turn:%d" % t if t >= 1 else None,
                restore_label="Restore to before turn %d" % t if t >= 1 else "",
            )
            for t in turns
        ]
        commits = tuple(
            pj.Commit(
                c["id"],
                c["subject"],
                c["author"],
                c["date"],
                c["files"],
                group="turn:%d" % c["turn"] if c["turn"] is not None else None,
                actor=c["actor"],
            )
            for c in page
        )
        more = offset + limit < len(newest_first)
        return pj.HistoryPage(
            commits,
            self.etag(project_id),
            tuple(groups),
            head_label="Turn now %d" % max([c["turn"] or 0 for c in project["commits"]]),
            next_cursor=str(offset + limit) if more else None,
        )

    async def commit_diff(self, project_id, commit):
        project = self._get(project_id)
        for c in project["commits"]:
            if c["id"] == commit:
                return "".join("+++ %s\n" % f for f in c["files"]) or ""
        raise pj.NotFound("there is no commit %s" % commit)

    async def restore(self, project_id, target, *, expected_etag, actor):
        self.calls.append(("restore", project_id, target, expected_etag))
        if self.busy:
            raise pj.Busy("the agent is working on a turn")
        self._check(project_id, expected_etag)
        if not target.startswith("turn:") or not target[5:].isdigit():
            raise pj.Invalid("%r is not something history offered" % target)
        turn = int(target[5:])
        project = self.projects[project_id]
        commits = project["commits"]
        tagged = [i for i, c in enumerate(commits) if c["turn"] is not None and c["turn"] >= turn]
        if not tagged:
            return pj.RestoreResult(self.etag(project_id))
        before = commits[tagged[0] - 1]["tree"]
        changes = tuple(
            sorted(
                f for f in {*project["tree"], *before} if project["tree"].get(f) != before.get(f)
            )
        )
        if not changes:
            return pj.RestoreResult(self.etag(project_id))
        project["version"] += 1
        project["tree"] = dict(before)
        commits.append(
            {
                "id": "c%03d" % len(commits),
                "subject": "restore: to before turn %d" % turn,
                "author": actor.label or "user",
                "actor": "human",
                "turn": commits[-1]["turn"],
                "files": changes,
                "date": "2026-10-24T11:00:00+00:00",
                "tree": dict(before),
            }
        )
        return pj.RestoreResult(self.etag(project_id), commits[-1]["id"], changes, "restored")
