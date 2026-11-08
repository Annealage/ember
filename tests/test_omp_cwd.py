"""``omp_cwd``: the directory omp runs in is private, anchored and never reused once written into."""

import os
from pathlib import Path

import pytest

from annealage_ember.session.omp_cwd import OmpCwdError, omp_cwd


@pytest.fixture(autouse=True)
def private_tmp(tmp_path, monkeypatch):
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(base))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return base


def test_a_kept_directory_is_the_same_each_time_and_a_throwaway_one_is_new_each_time(tmp_path):
    first, ephemeral = omp_cwd(tmp_path / "state")
    again, _ = omp_cwd(tmp_path / "state")
    other, _ = omp_cwd(tmp_path / "other")
    assert first == again and first != other and not ephemeral
    one, flag = omp_cwd()
    two, _ = omp_cwd()
    assert one != two and flag
    for directory in (first, one):
        assert sorted(os.listdir(directory)) == [".git", ".omp"]
        assert os.listdir(directory / ".omp") == [] and os.listdir(directory / ".git") == ["HEAD"]
        assert oct(os.stat(directory).st_mode & 0o777) == "0o700"


def test_a_relative_and_an_absolute_spelling_of_a_session_directory_are_the_same(
    tmp_path, monkeypatch
):
    (tmp_path / "state").mkdir()
    monkeypatch.chdir(tmp_path)
    assert (
        omp_cwd("state")[0]
        == omp_cwd(tmp_path / "state")[0]
        == omp_cwd(tmp_path / "x" / ".." / "state")[0]
    )


@pytest.mark.parametrize(
    "poison", ("file_in_omp", "tool_dir", "extra_entry", "symlink_omp", "head_changed", "git_extra")
)
def test_a_directory_someone_wrote_into_is_refused_and_left_alone(tmp_path, poison):
    directory, _ = omp_cwd(tmp_path / "state")
    if poison == "file_in_omp":
        (directory / ".omp" / "tools").mkdir()
        (directory / ".omp" / "tools" / "x.js").write_text("x")
        planted = directory / ".omp" / "tools" / "x.js"
    elif poison == "tool_dir":
        (directory / ".omp" / "settings.json").write_text("{}")
        planted = directory / ".omp" / "settings.json"
    elif poison == "extra_entry":
        (directory / "AGENTS.md").write_text("x")
        planted = directory / "AGENTS.md"
    elif poison == "symlink_omp":
        os.rmdir(directory / ".omp")
        (directory / ".omp").symlink_to(tmp_path)
        planted = directory / ".omp"
    elif poison == "head_changed":
        (directory / ".git" / "HEAD").write_text("ref: refs/heads/evil\n")
        planted = directory / ".git" / "HEAD"
    else:
        (directory / ".git" / "hooks").mkdir()
        planted = directory / ".git" / "hooks"
    with pytest.raises(OmpCwdError, match="refusing"):
        omp_cwd(tmp_path / "state")
    assert os.path.lexists(planted)


def test_a_root_made_by_someone_else_or_open_to_others_is_refused(tmp_path, private_tmp):
    root = private_tmp / ("annealage-omp-%d" % os.getuid())
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    with pytest.raises(OmpCwdError, match="other users"):
        omp_cwd(tmp_path / "state")
    os.chmod(root, 0o700)
    root.rmdir()
    root.symlink_to(tmp_path)
    with pytest.raises(OmpCwdError, match="not a directory"):
        omp_cwd(tmp_path / "state")


def test_a_root_inside_the_project_or_the_session_directory_is_refused(tmp_path, private_tmp):
    with pytest.raises(OmpCwdError, match="inside the served project"):
        omp_cwd(tmp_path / "state", keep_out_of=[tmp_path])
    assert Path(omp_cwd(tmp_path / "state", keep_out_of=[tmp_path / "elsewhere"])[0]).is_dir()


def test_a_base_under_the_home_directory_is_passed_over_for_a_system_one(tmp_path, monkeypatch):
    """omp's context-file walk climbs from a git root to ``$HOME``, so under it the anchors stop nothing."""
    from annealage_ember.session import omp_cwd as module

    monkeypatch.delenv("ANNEALAGE_OMP_CWD_BASE")
    home = tmp_path / "home"
    under = home / "tmp"
    under.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(under))
    # /tmp, /var/tmp and /dev/shm are the machine's; only the rule is checked here.
    assert not module._inside(module._root(), str(home))
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(under))
    with pytest.raises(OmpCwdError, match="under the home directory"):
        omp_cwd(tmp_path / "state")
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(outside))
    assert str(omp_cwd(tmp_path / "state")[0]).startswith(str(outside))


def test_a_base_other_users_can_rename_in_is_refused_but_a_sticky_one_is_not(tmp_path, monkeypatch):
    """Whoever can rename entries in a directory can swap the checked tree from under omp."""
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    os.chmod(open_dir, 0o777)
    base = open_dir / "base"
    base.mkdir()
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(base))
    # Not the base itself but an ancestor is the hole.
    with pytest.raises(OmpCwdError, match="writable by other users and not sticky"):
        omp_cwd(tmp_path / "state")
    os.chmod(open_dir, 0o1777)
    assert str(omp_cwd(tmp_path / "state")[0]).startswith(str(base))
    os.chmod(base, 0o775)
    with pytest.raises(OmpCwdError, match="not sticky"):
        omp_cwd(tmp_path / "other")


def test_a_base_inside_the_project_is_skipped_for_the_next_default_but_a_named_one_is_not(
    tmp_path, monkeypatch
):
    from annealage_ember.session import omp_cwd as module

    project = tmp_path / "project"
    (project / "tmp").mkdir(parents=True)
    monkeypatch.delenv("ANNEALAGE_OMP_CWD_BASE")
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(project / "tmp"))
    chosen = module._root([str(project)])
    assert not module._inside(chosen, str(project))
    assert chosen != os.path.join(
        os.path.realpath(project / "tmp"), "annealage-omp-%d" % os.getuid()
    )
    # The project is /tmp itself: the others are tried.
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: "/tmp")
    chosen = module._root(["/tmp"])
    assert not module._inside(chosen, "/tmp")
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(project / "tmp"))
    with pytest.raises(OmpCwdError, match="inside the served project"):
        module._root([str(project)])


def test_a_root_that_is_exactly_the_project_or_the_home_directory_is_not_used(
    tmp_path, monkeypatch
):
    """The base can pass every test while ``<base>/annealage-omp-<uid>`` is the excluded directory."""
    from annealage_ember.session import omp_cwd as module

    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(base))
    exact = base / ("annealage-omp-%d" % os.getuid())
    exact.mkdir(mode=0o700)
    with pytest.raises(OmpCwdError, match="inside the served project"):
        module._root([str(exact)])
    with pytest.raises(OmpCwdError, match="inside the served project"):
        omp_cwd(tmp_path / "state", keep_out_of=[exact])
    monkeypatch.setenv("HOME", str(exact))
    with pytest.raises(OmpCwdError, match="under the home directory"):
        module._root()
    # Without a named base the next default is tried instead.
    monkeypatch.delenv("ANNEALAGE_OMP_CWD_BASE")
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(base))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert module._root([str(exact)]) != str(exact)
    # And a kept directory that is the project itself is refused outright.
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(base))
    directory, _ = omp_cwd(tmp_path / "state")
    with pytest.raises(OmpCwdError, match="not private"):
        omp_cwd(tmp_path / "state", keep_out_of=[directory])
