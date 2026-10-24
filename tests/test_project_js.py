"""The page's project client and models (``static/project.js``) under Node.

The cases live in ``project_cases.mjs``, each an async function with its own
assertions against a store speaking the routes' wire: the conflict the page
must keep on screen (a restore, a title, an id), the etag a change is made
against, history paging that never repeats a commit and reads again when the
project moved under a cursor, and a late reply that must not overwrite a later
one. The views over these models run in a real browser in
``test_projects_e2e.py``.
"""

import pathlib
import shutil
import subprocess

import pytest

CASES = pathlib.Path(__file__).resolve().parent / "project_cases.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def _node(*args):
    return subprocess.run(
        ["node", str(CASES), *args], capture_output=True, text=True, timeout=60, check=False
    )


def _names():
    if shutil.which("node") is None:
        return []
    return _node("--list").stdout.split()


@pytest.mark.parametrize("case", _names())
def test_case(case):
    done = _node(case)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_the_cases_were_found():
    assert len(_names()) >= 15
