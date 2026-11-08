"""A conversation file as omp writes it, for the tests whose omp is a fake client.

A resume now moves the file's recorded working directory first (``session/omp_session_file.py``),
which reads the header, so a stand-in for a conversation has to have one."""

import json
from pathlib import Path


def write_conversation(path, body="", *, cwd="/elsewhere/an-earlier-project"):
    """Header and then ``body`` lines; ``cwd`` is where an earlier Ember ran omp."""
    header = {
        "type": "session",
        "version": 3,
        "id": "01a1bcff-23d0-721e-a922-e8707eae2c5e",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "cwd": cwd,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(header) + "\n" + body, encoding="utf-8")
    return path


def conversation_body(path):
    """What follows the header: the history, which a resume must not change."""
    return Path(path).read_text(encoding="utf-8").split("\n", 1)[1]
