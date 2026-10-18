"""``sessions.rehome``: a project directory moved to a new name keeps its
conversations. The recorded project key is what makes a session known to a
directory, so a move without it leaves ``-c`` and a resume finding nothing."""

import json
import os

from annealage_ember import sessions


def _project_with_session(root, name="before"):
    old = root / name
    sid = sessions.create_session(old)
    omp = sessions.state_dir(old) / "omp"
    omp.mkdir()
    file = omp / "2026-10-19_abc.jsonl"
    header = {"type": "session", "version": 3, "id": "abc", "cwd": str(old.resolve())}
    body = b'{"type":"message","text":"hello \xc3\xa9 world"}\n'
    file.write_bytes(
        b'{"type":"title","v":1,"pad":"   "}\n'
        + json.dumps(header, separators=(",", ":")).encode()
        + b"\n"
        + body
    )
    sessions.set_omp_session_file(old, sid, str(file))
    sessions.record_last_session(old, sid)
    return old, sid, body


def test_a_moved_project_finds_its_sessions_and_its_omp_conversation_again(tmp_path):
    old, sid, body = _project_with_session(tmp_path)
    new = tmp_path / "after"
    os.rename(old, new)
    assert sessions.get_session_info(new, sid) is None  # the guard works: not this directory's
    assert sessions.resolve_continue(new) is None

    assert sessions.rehome(old, new) == {"sessions": 1, "omp_files": 1}

    info = sessions.get_session_info(new, sid)
    assert info is not None and sessions.resolve_continue(new) == sid
    moved = new.resolve() / ".toy" / "omp" / "2026-10-19_abc.jsonl"
    assert info.omp_session_file == str(moved)
    lines = moved.read_bytes().split(b"\n")
    assert json.loads(lines[1])["cwd"] == str(new.resolve())
    assert lines[0] == b'{"type":"title","v":1,"pad":"   "}'  # not the line omp rewrites in place
    assert lines[2] + b"\n" == body  # the conversation itself is copied as it was

    # Nothing left to change, and the move can be undone.
    assert sessions.rehome(old, new) == {"sessions": 0, "omp_files": 0}
    os.rename(new, old)
    assert sessions.rehome(new, old) == {"sessions": 1, "omp_files": 1}
    assert sessions.get_session_info(old, sid) is not None


def test_a_session_recorded_against_another_directory_is_left_alone(tmp_path):
    old, sid, _body = _project_with_session(tmp_path)
    other = sessions.create_session(old, "20260101T000000Z-other")
    meta = sessions.meta_path(old, other)
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["project_key"] = sessions.project_key_for_directory(tmp_path / "elsewhere")
    meta.write_text(json.dumps(data), encoding="utf-8")
    new = tmp_path / "after"
    os.rename(old, new)

    assert sessions.rehome(old, new)["sessions"] == 1
    assert json.loads(sessions.meta_path(new, other).read_text(encoding="utf-8")) == data
