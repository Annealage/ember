"""The working directory omp runs in: private, trusted, and empty of project config.

omp discovers project config (``.mcp.json``, ``.omp/tools``, ``.claude/tools``,
``.omp/plugins``, ``.omp/settings``, ``AGENTS.md``, skills) from its working directory
and every parent, and acts on it before the first model call, so a run must never start
in a directory a project, or anyone else, can write to. ``session.omp`` therefore starts
omp here, in a directory this module makes and checks:

* It lives under ``<base>/annealage-omp-<uid>`` (``_root`` chooses a base outside the home
  directory, which omp's context-file walk would otherwise climb to), made with mode 0700 and refused unless
  it is a real directory (not a symlink) owned by this user with no group or other
  access, so another user cannot have created it first. The base it is made in is chosen
  by ``_root``: not under ``$HOME``, not inside a directory the caller names (the served
  project, the session directory), and with every directory from ``/`` down owned by root
  or this user and closed to other writers or sticky, so nobody else can rename the tree
  from under a checked directory.
* A conversation kept in a session directory runs in a directory named after that
  directory's real path, so a resume starts omp in the same place every time (omp
  cancels ``switch_session`` for a conversation recorded in another directory). A run
  with no session directory gets a fresh random one, which the session removes.
* It holds exactly an empty ``.omp`` directory and a ``.git`` directory with a ``HEAD``
  file. Those are anchors: omp's walks for project config, context files, rules and
  skills stop at the first directory holding one (tested on omp 18.4.5: ``.omp`` alone
  stops the tool and plugin search but a parent's ``AGENTS.md`` and skills still reach the
  prompt; ``.git`` stops those too).
* An existing directory is reused only if it holds exactly that and nothing else. Anything
  extra, or a symlink in place of an entry, raises ``OmpCwdError`` and nothing is deleted
  or repaired: a directory someone has written into is not one to run in.

What this does not stop is a process running as the same user writing into the directory
between the check and omp's start; that user can already do everything the agent can.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Optional, Sequence

#: omp's own project-config directory: empty, here only so omp stops walking up.
_OMP_DIR = ".omp"
#: The marker the git-root walk stops at: not a repository, only a ``HEAD`` file.
_GIT_DIR = ".git"
_GIT_HEAD = b"ref: refs/heads/main\n"


class OmpCwdError(RuntimeError):
    """The directory omp would run in is not one this user made and can trust."""


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _refuse_if_inside(directory: str, excluded: Sequence[str]) -> None:
    """The directory omp would run in is itself the home directory or the project, or inside one."""
    real = os.path.realpath(directory)
    for other in excluded:
        if _inside(real, other):
            raise OmpCwdError(
                "%s is inside %s, which is not private; refusing to run omp there" % (real, other)
            )


def _own_private_dir(path: str, *, create: bool) -> None:
    """``path`` is a real directory owned by this user that nobody else can enter; make it if
    ``create``. Never follows a symlink, never changes an existing directory's mode."""
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise OmpCwdError("cannot inspect %s: %s" % (path, exc)) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise OmpCwdError(
            "%s is not a directory (or is a symlink); refusing to run omp there" % path
        )
    if info.st_uid != os.getuid():
        raise OmpCwdError("%s is not owned by this user; refusing to run omp there" % path)
    if info.st_mode & 0o077:
        raise OmpCwdError(
            "%s is accessible to other users (mode %o); refusing to run omp there"
            % (path, stat.S_IMODE(info.st_mode))
        )


def _check_contents(directory: str) -> None:
    """Exactly the two anchors, nothing else, none of them a symlink."""
    entries = set(os.listdir(directory))
    if entries != {_OMP_DIR, _GIT_DIR}:
        extra = sorted(entries - {_OMP_DIR, _GIT_DIR})
        raise OmpCwdError(
            "%s holds %s instead of exactly %s and %s; refusing to run omp there"
            % (directory, extra or "no anchors", _OMP_DIR, _GIT_DIR)
        )
    omp_dir = os.path.join(directory, _OMP_DIR)
    git_dir = os.path.join(directory, _GIT_DIR)
    for path in (omp_dir, git_dir):
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OmpCwdError("%s is not a plain directory; refusing to run omp there" % path)
    if os.listdir(omp_dir):
        raise OmpCwdError("%s is not empty; refusing to run omp there" % omp_dir)
    if os.listdir(git_dir) != ["HEAD"]:
        raise OmpCwdError("%s holds more than HEAD; refusing to run omp there" % git_dir)
    head = os.path.join(git_dir, "HEAD")
    info = os.lstat(head)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OmpCwdError("%s is not a plain file; refusing to run omp there" % head)
    with open(head, "rb") as handle:
        if handle.read(len(_GIT_HEAD) + 1) != _GIT_HEAD:
            raise OmpCwdError("%s was changed; refusing to run omp there" % head)


def _populate(directory: str) -> None:
    os.mkdir(os.path.join(directory, _OMP_DIR), 0o700)
    git_dir = os.path.join(directory, _GIT_DIR)
    os.mkdir(git_dir, 0o700)
    fd = os.open(
        os.path.join(git_dir, "HEAD"),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as handle:
        handle.write(_GIT_HEAD)


def _make(root: str, name: Optional[str]) -> str:
    """A finished directory under ``root``: built under a private name, then renamed into place
    so a second run starting at the same moment never sees it half made."""
    staging = tempfile.mkdtemp(prefix="new-", dir=root)
    try:
        _populate(staging)
        if name is None:
            return staging
        target = os.path.join(root, name)
        try:
            os.rename(staging, target)
        except OSError:
            # Another run got there first (rename onto a non-empty directory fails).
            if not os.path.lexists(target):
                raise
        else:
            return target
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    return target


def _home() -> str:
    """The home directory omp will see: its own ``os.homedir()``, which reads ``$HOME`` first."""
    return os.path.realpath(os.environ.get("HOME") or os.path.expanduser("~"))


def _untrusted_path(base: str) -> Optional[str]:
    """Why ``base`` or one of its ancestors could be swapped from under a directory made in it, or None.

    Whoever can rename entries in a directory can replace what is inside it between a check and
    omp's start. Every directory from ``/`` to ``base`` therefore has to belong to root or this
    user and be closed to group and other writers, or be sticky (``/tmp``: others can create
    entries but cannot rename or remove ours)."""
    current = base
    while True:
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode):
            return "%s is not a directory" % current
        if info.st_uid not in (0, os.getuid()):
            return "%s belongs to another user" % current
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            return "%s is writable by other users and not sticky" % current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _root(keep_out_of: Sequence[str] = ()) -> str:
    """``<base>/annealage-omp-<uid>``, with the first base that is safe to make it in.

    A base is passed over when it is under the home directory, inside a directory in ``keep_out_of``
    (the served project), or not trustworthy (``_untrusted_path``). omp's context-file walk
    (``loadStandaloneContextFiles``) continues past a ``.git`` root up to the home directory when the
    working directory is below it, so under ``$HOME`` the anchors below stop nothing (tested on omp
    18.4.5 and 18.11.3: a parent's ``AGENTS.md`` reached the prompt). The defaults are the temp
    directory, then ``/tmp``, ``/var/tmp`` and ``/dev/shm``; ``ANNEALAGE_OMP_CWD_BASE`` names one
    outright, and that one is not replaced by another when it is refused."""
    home = _home()
    named = os.environ.get("ANNEALAGE_OMP_CWD_BASE", "").strip()
    candidates = [named] if named else [tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm"]
    avoid = [os.path.realpath(os.fspath(d)) for d in keep_out_of]
    refused = []
    for candidate in candidates:
        base = os.path.realpath(candidate)
        root = os.path.join(base, "annealage-omp-%d" % os.getuid())
        if not (os.path.isdir(base) and os.access(base, os.W_OK | os.X_OK)):
            refused.append("%s is not a writable directory" % candidate)
        elif _inside(base, home) or _inside(root, home):
            refused.append("%s is under the home directory %s" % (root, home))
        elif any(_inside(base, d) or _inside(root, d) for d in avoid):
            # The root itself too: the project can be exactly <base>/annealage-omp-<uid>.
            refused.append("%s is inside the served project or session directory" % root)
        else:
            why = _untrusted_path(base)
            if why is None:
                return root
            refused.append("%s: %s" % (base, why))
    raise OmpCwdError(
        "no safe directory to run omp in (%s); set ANNEALAGE_OMP_CWD_BASE to a directory "
        "of this user's own outside the home directory" % "; ".join(refused)
    )


def omp_cwd(
    session_dir: Optional[os.PathLike] = None, *, keep_out_of: Sequence[os.PathLike] = ()
) -> "tuple[Path, bool]":
    """The directory to start omp in, and whether this call made a throwaway one.

    With ``session_dir`` the directory is named after its real path and kept between runs (the
    second item is False); without, it is a new random one the caller removes with
    ``shutil.rmtree`` (True). ``keep_out_of`` are directories the result must not sit inside.
    Raises ``OmpCwdError``."""
    root = _root([os.fspath(d) for d in keep_out_of])
    _own_private_dir(root, create=True)
    excluded = [_home()] + [os.path.realpath(os.fspath(d)) for d in keep_out_of]
    if session_dir is None:
        directory = _make(root, None)
        _refuse_if_inside(directory, excluded)
        _own_private_dir(directory, create=False)
        _check_contents(directory)
        return Path(directory), True
    key = hashlib.sha256(os.path.realpath(os.fspath(session_dir)).encode()).hexdigest()[:32]
    directory = os.path.join(root, key)
    _refuse_if_inside(directory, excluded)
    if not os.path.lexists(directory):
        directory = _make(root, key)
    _own_private_dir(directory, create=False)
    _check_contents(directory)
    return Path(directory), False
