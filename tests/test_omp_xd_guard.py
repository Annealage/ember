"""``run_prompt(..., deferred_tools=True)`` against a real ``omp`` process.

The model is a scripted loopback stand-in (``tests/omp_loopback.py``), so these
runs need the ``omp`` binary but no network, credentials or paid call. What they
pin is what the model can actually do: the guard extension is loaded by the real
omp, its ``tool_call`` handler is reached for the transport tools and the nested
device call, and whatever it blocks comes back to the model as the tool result.
Everything the fake-client tests in ``test_headless_omp.py`` cannot show.
"""

import http.server
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

# omp-rpc needs Python 3.11 or later; the 3.10 CI job does not install it.
pytest.importorskip("omp_rpc")

from omp_loopback import MODEL, LoopbackModel, loopback_env, write_agent_dir  # noqa: E402
from test_headless import make_args, make_toolset  # noqa: E402

from annealage_ember.headless import run_prompt  # noqa: E402
from annealage_ember.session import omp as omp_module  # noqa: E402

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which("omp") is None, reason="the omp binary is not installed"),
]

SECRET = "SENTINEL-4471-not-for-the-model"


@pytest.fixture
def loopback(monkeypatch):
    """A factory for the scripted model; omp inherits the environment it needs."""
    models = []
    dirs = []

    def build(script):
        model = LoopbackModel(script)
        models.append(model)
        agent_dir = write_agent_dir(model.port)
        dirs.append(agent_dir)
        for key, value in loopback_env(agent_dir).items():
            monkeypatch.setenv(key, value)
        return model, agent_dir

    yield build
    for model in models:
        model.close()
    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)


async def _run(loopback, tmp_path, script, *, toolset=None, **overrides):
    model, agent_dir = loopback(script)
    kwargs = {"model": MODEL, "omp_agent_dir": agent_dir, "timeout": 60}
    if toolset is not None:
        kwargs["tools"] = toolset
    kwargs.update(overrides)
    result = await run_prompt("go", **make_args(tmp_path, **kwargs))
    return result, model


def _call(name, **args):
    return ("call", name, args)


def _outputs(model, count):
    return [model.tool_output("call_%d" % i) for i in range(count)]


async def test_the_default_run_offers_the_tool_set_and_no_read_or_write(loopback, tmp_path):
    result, model = await _run(loopback, tmp_path, [("text", "hi")])
    assert result.error is None
    assert sorted(model.tool_names()) == ["lookup", "pan", "save"]


async def test_a_deferred_run_offers_only_the_transport(loopback, tmp_path):
    result, model = await _run(loopback, tmp_path, [("text", "hi")], deferred_tools=True)
    assert result.error is None
    assert sorted(model.tool_names()) == ["read", "write"]


FAKE_MCP_SERVER = """
import json, sys
open(sys.argv[1], "a").write("mcp server started\\n")
for line in sys.stdin:
    request = json.loads(line)
    method, ident = request.get("method"), request.get("id")
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "planted", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "planted_tool", "description": "x",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif ident is None:
        continue
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
"""

# A custom tool module: omp imports and runs it at startup, so this is code execution
# before any model call, and it offers a tool.
PLANTED_TOOL = """
import fs from "node:fs";
fs.appendFileSync(%r, "custom tool loaded\\n");
export default () => ({
  name: "planted_js", description: "x",
  parameters: {type: "object", properties: {}},
  execute: async () => ({content: [{type: "text", text: "x"}]}),
});
"""


def _plant(root, sentinel):
    """Everything in a project directory that omp would otherwise start or load."""
    server = root.parent / ("%s_mcp.py" % root.name)
    server.write_text(FAKE_MCP_SERVER)
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "planted": {"command": "python3", "args": [str(server), str(sentinel)]}
                }
            }
        )
    )
    (root / ".omp" / "tools").mkdir(parents=True)
    (root / ".omp" / "tools" / "planted.js").write_text(PLANTED_TOOL % str(sentinel))
    (root / ".claude" / "tools").mkdir(parents=True)
    (root / ".claude" / "tools" / "planted.js").write_text(PLANTED_TOOL % str(sentinel))
    plugin = root / ".omp" / "plugins"
    (plugin / "node_modules" / "planted").mkdir(parents=True)
    (plugin / "package.json").write_text(json.dumps({"dependencies": {"planted": "1"}}))
    (plugin / "node_modules" / "planted" / "package.json").write_text(
        json.dumps({"name": "planted", "version": "1", "omp": {"tools": ["tool.js"]}})
    )
    (plugin / "node_modules" / "planted" / "tool.js").write_text(PLANTED_TOOL % str(sentinel))
    # A project's own settings outrank the agent directory's config.yml, and can switch
    # on discovery from other tools' user-level config.
    (root / ".omp" / "settings.json").write_text(
        json.dumps({"mcp": {"enableProjectConfig": True}, "enabledProviders": ["*"]})
    )


@pytest.mark.parametrize("where", ("cwd", "parent"))
@pytest.mark.parametrize("deferred", (False, True))
async def test_a_project_directory_starts_and_offers_nothing_to_omp(
    loopback, tmp_path, monkeypatch, deferred, where
):
    """omp discovers project config (``.mcp.json``, ``.omp/tools``, ``.claude/tools``,
    ``.omp/plugins``, ``.omp/settings``) from its working directory and every parent, imports
    and starts what it finds before the first model call, and offers the tools even under
    ``--no-tools``. None of that may happen, and the host tools still work beside it."""
    sentinel = tmp_path / "sentinel"
    project = tmp_path / "project"
    work = project if where == "cwd" else project / "sub"
    work.mkdir(parents=True)
    _plant(project, sentinel)
    home = tmp_path / "home"
    # Another tool's user-level MCP config, which a project setting can opt in to.
    (home).mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "foreign": {
                        "command": "python3",
                        "args": [str(tmp_path / "project_mcp.py"), str(sentinel)],
                    }
                }
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    call = (
        _call("write", path="xd://lookup", content=json.dumps({"q": "board"}))
        if deferred
        else _call("lookup", q="board")
    )
    result, model = await _run(
        loopback, tmp_path, [call, ("text", "done")], cwd=work, deferred_tools=deferred
    )
    assert result.error is None and result.text == "done"
    assert not sentinel.exists(), sentinel.read_text() if sentinel.exists() else ""
    assert sorted(model.tool_names()) == (
        ["read", "write"] if deferred else ["lookup", "pan", "save"]
    )
    assert "found board" in model.tool_output("call_0")


async def test_a_durable_conversation_resumes_though_omp_runs_in_its_own_directory(
    loopback, tmp_path
):
    """omp's working directory is private to each run, so a resumed conversation was started
    in a different one."""
    model, agent_dir = loopback([("text", "first answer"), ("text", "second answer")])
    state = tmp_path / "state"
    common = {"model": MODEL, "omp_agent_dir": agent_dir, "timeout": 60, "state_dir": state}
    first = await run_prompt("remember the word kestrel", **make_args(tmp_path, **common))
    assert first.error is None and first.text == "first answer"
    second = await run_prompt("what was the word", **make_args(tmp_path, resume=True, **common))
    assert second.error is None and second.text == "second answer"
    assert "kestrel" in json.dumps(model.requests[-1])


def _set_header_cwd(path, cwd):
    """What a conversation an earlier Ember started looks like: the caller's directory recorded."""
    lines = path.read_bytes().splitlines(True)
    for index, line in enumerate(lines[:2]):
        header = json.loads(line)
        if header.get("type") == "session":
            header["cwd"] = str(cwd)
            lines[index] = json.dumps(header, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(b"".join(lines))


def _conversation_file(state):
    (found,) = (state / "omp").glob("*.jsonl")
    return found


async def test_a_conversation_an_earlier_version_recorded_resumes_with_its_history(
    loopback, tmp_path
):
    """The real omp refuses to resume a conversation recorded in another directory; the file's
    header is moved first, the original kept, and the strict headless resume works."""
    model, agent_dir = loopback([("text", "first answer"), ("text", "second answer")])
    state = tmp_path / "state"
    common = {"model": MODEL, "omp_agent_dir": agent_dir, "timeout": 60, "state_dir": state}
    first = await run_prompt("remember the word kestrel", **make_args(tmp_path, **common))
    assert first.error is None
    file = _conversation_file(state)
    _set_header_cwd(file, tmp_path / "the-old-project")
    before = file.read_bytes()
    before_header = [json.loads(x) for x in before.splitlines()[:2]]
    second = await run_prompt("what was the word", **make_args(tmp_path, resume=True, **common))
    assert second.error is None and second.text == "second answer"
    assert "kestrel" in json.dumps(model.requests[-1])
    after = [json.loads(x) for x in file.read_bytes().splitlines()]
    (session,) = [x for x in after if x["type"] == "session"]
    (old,) = [x for x in before_header if x["type"] == "session"]
    assert session["id"] == old["id"] and session["cwd"] != old["cwd"]
    assert {k: v for k, v in session.items() if k != "cwd"} == {
        k: v for k, v in old.items() if k != "cwd"
    }
    backup = file.with_name(file.name + ".cwd-original")
    assert backup.read_bytes() == before
    # Both runs were one conversation to the provider: same session, same cache identity.
    assert len({json.dumps(r.get("prompt_cache_key")) for r in model.requests}) == 1


async def test_a_conversation_file_that_cannot_be_moved_stops_a_strict_resume_and_is_untouched(
    loopback, tmp_path
):
    model, agent_dir = loopback([("text", "first answer"), ("text", "second answer")])
    state = tmp_path / "state"
    common = {"model": MODEL, "omp_agent_dir": agent_dir, "timeout": 60, "state_dir": state}
    first = await run_prompt("remember the word kestrel", **make_args(tmp_path, **common))
    assert first.error is None
    file = _conversation_file(state)
    _set_header_cwd(file, tmp_path / "the-old-project")
    lines = file.read_bytes().splitlines(True)
    index = next(i for i, x in enumerate(lines[:2]) if json.loads(x).get("type") == "session")
    header = json.loads(lines[index])
    header["version"] = 99
    lines[index] = json.dumps(header, separators=(",", ":")).encode() + b"\n"
    file.write_bytes(b"".join(lines))
    before = file.read_bytes()
    second = await run_prompt("what was the word", **make_args(tmp_path, resume=True, **common))
    assert second.error is not None and "cannot be moved" in second.error
    assert file.read_bytes() == before
    assert not list(file.parent.glob("*.cwd-original"))


async def test_a_relative_session_directory_is_the_one_asked_for(loopback, tmp_path, monkeypatch):
    model, agent_dir = loopback([("text", "hi")])
    monkeypatch.chdir(tmp_path)
    result = await run_prompt(
        "go",
        **make_args(
            tmp_path, model=MODEL, omp_agent_dir=agent_dir, timeout=60, omp_session_dir="usage"
        ),
    )
    assert result.error is None
    assert len(list((tmp_path / "usage").glob("*.jsonl"))) == 1
    assert not list(tmp_path.rglob(".omp-cwd"))


async def test_a_poisoned_kept_directory_is_refused_before_omp_starts(loopback, tmp_path):
    from annealage_ember.session.omp_cwd import omp_cwd

    sentinel = tmp_path / "sentinel"
    usage = tmp_path / "usage"
    usage.mkdir()
    directory, _ = omp_cwd(usage)
    (directory / ".omp" / "tools").mkdir()
    (directory / ".omp" / "tools" / "planted.js").write_text(PLANTED_TOOL % str(sentinel))
    result, model = await _run(loopback, tmp_path, [("text", "hi")], omp_session_dir=str(usage))
    assert result.error is not None and "refusing to run omp there" in result.error
    assert not sentinel.exists() and model.requests == []


async def test_nothing_above_the_private_directory_reaches_omp(loopback, tmp_path, monkeypatch):
    """The temp directory sits under a tree an attacker could have written to: every
    ancestor holds project config, context files and skills."""
    sentinel = tmp_path / "sentinel"
    tree = tmp_path / "attacker"
    base = tree / "a" / "tmp"
    base.mkdir(parents=True)
    for ancestor in (tree, tree / "a", base):
        _plant(ancestor, sentinel)
        (ancestor / "AGENTS.md").write_text("MARK_ANCESTOR_AGENTS")
        (ancestor / "CLAUDE.md").write_text("MARK_ANCESTOR_CLAUDE")
        (ancestor / ".claude" / "skills" / "evil").mkdir(parents=True)
        (ancestor / ".claude" / "skills" / "evil" / "SKILL.md").write_text(
            "---\nname: evil-skill\ndescription: MARK_ANCESTOR_SKILL\n---\nx\n"
        )
        (ancestor / ".agents" / "skills" / "evil2").mkdir(parents=True)
        (ancestor / ".agents" / "skills" / "evil2" / "SKILL.md").write_text(
            "---\nname: evil2-skill\ndescription: MARK_ANCESTOR_SKILL2\n---\nx\n"
        )
    monkeypatch.setenv("ANNEALAGE_OMP_CWD_BASE", str(base))
    work = tmp_path / "work"
    work.mkdir()
    result, model = await _run(loopback, tmp_path, [("text", "hi")], cwd=work)
    assert result.error is None and not sentinel.exists()
    body = json.dumps(model.requests[0])
    assert "MARK_ANCESTOR" not in body, body[
        body.index("MARK_ANCESTOR") - 260 : body.index("MARK_ANCESTOR")
    ]
    assert sorted(model.tool_names()) == ["lookup", "pan", "save"]


async def test_host_tools_run_in_the_hosts_directory_not_omps(loopback, tmp_path, monkeypatch):
    from claude_agent_sdk import tool

    from annealage_ember.headless import ToolSet

    seen = []

    @tool("where", "report the working directory", {})
    async def where(args):
        seen.append(os.getcwd())
        return ok(text="cwd %s" % os.getcwd())

    from annealage_ember.tools import Grading, ok

    toolset = ToolSet(tools=[where], grading=Grading(read=("where",), view=(), write=()))
    monkeypatch.chdir(tmp_path)
    result, model = await _run(
        loopback,
        tmp_path,
        [_call("where"), ("text", "done")],
        toolset=toolset,
    )
    assert result.error is None and seen == [str(tmp_path)]
    assert str(tmp_path) in model.tool_output("call_0")
    assert "annealage-omp" not in model.tool_output("call_0")


async def test_the_model_finds_a_device_schema_and_corrects_its_call_in_the_same_session(
    loopback, tmp_path
):
    script = [
        _call("read", path="xd://"),
        _call("read", path="xd://lookup"),
        _call("write", path="xd://lookup", content="{}"),
        _call("write", path="xd://lookup", content=json.dumps({"q": "board"})),
        ("text", "done"),
    ]
    result, model = await _run(loopback, tmp_path, script, deferred_tools=True)
    listing, schema, rejected, corrected = _outputs(model, 4)
    assert result.error is None
    for name in ("lookup", "pan", "save"):
        assert "xd://%s" % name in listing
    assert "q: string" in schema
    assert "Invalid args" in rejected and "q: is required" in rejected
    assert "found board" in corrected
    assert result.text == "done"
    # omp reports the transport call, not the device behind it: the refused write is
    # an error and the corrected one is not.
    assert result.calls == (("read", True), ("read", True), ("write", False), ("write", True))


# Every spelling of a read or write that is not an `xd://` device call. The first
# column is the tool, the second its path.
BLOCKED = (
    ("read", "{secret}"),
    ("read", "{secret}:1-1"),
    ("read", "file://{secret}"),
    ("read", "{directory}"),
    ("read", "/etc/hostname"),
    ("read", "http://127.0.0.1:{port}/page"),
    ("read", "local://scratch"),
    ("read", "skill://anything"),
    ("read", "agent://anything"),
    ("read", "artifact://0"),
    ("read", "proc://"),
    ("read", "memory://root"),
    ("read", "xd://eval"),
    ("read", "xd://resolve"),
    ("read", "xd://lookup?q=1"),
    ("read", "xd://lookup/doc/extra"),
    ("read", "XD://lookup"),
    ("read", " xd://lookup"),
    ("read", "xd://lookup "),
    ("write", "{planted}"),
    ("write", "local://note"),
    ("write", "file://{planted}"),
    ("write", "xd://resolve"),
    ("write", "xd://propose"),
    ("write", "xd://"),
    ("write", "XD://save"),
)


async def test_nothing_but_device_calls_gets_through_read_and_write(loopback, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)
    planted = tmp_path / "planted.txt"
    page_hits = []

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            page_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(SECRET.encode())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fields = {
            "secret": str(secret),
            "directory": str(tmp_path),
            "planted": str(planted),
            "port": server.server_address[1],
        }
        script = []
        for tool, path in BLOCKED:
            args = {"path": path.format(**fields)}
            if tool == "write":
                args["content"] = json.dumps({"v": "x"})
            script.append(("call", tool, args))
        script.append(("text", "done"))
        result, model = await _run(loopback, tmp_path, script, deferred_tools=True)
    finally:
        server.shutdown()
        server.server_close()

    assert result.error is None
    outputs = _outputs(model, len(BLOCKED))
    for (tool, path), output in zip(BLOCKED, outputs, strict=True):
        assert output is not None and output.startswith("Blocked:"), (tool, path, output)
    assert SECRET not in json.dumps(model.requests)
    assert page_hits == []
    assert not planted.exists()


async def test_a_read_or_write_with_extra_arguments_is_blocked(loopback, tmp_path):
    script = [
        ("call", "read", {"path": "xd://lookup", "limit": 5}),
        ("call", "write", {"path": "xd://lookup", "content": "{}", "append": True}),
        ("text", "done"),
    ]
    result, model = await _run(loopback, tmp_path, script, deferred_tools=True)
    assert result.error is None
    first, second = _outputs(model, 2)
    assert first is not None and first.startswith("Blocked:")
    assert second is not None and second.startswith("Blocked:")


async def test_a_device_call_still_goes_through_the_tool_sets_own_grading(loopback, tmp_path):
    saved = []
    script = [_call("write", path="xd://save", content=json.dumps({"v": "kept"})), ("text", "done")]
    result, model = await _run(
        loopback, tmp_path, script, toolset=make_toolset(saved=saved), deferred_tools=True
    )
    assert saved == []
    assert "unattended run" in model.tool_output("call_0")
    assert result.calls == (("write", False),)

    granted = []
    result, model = await _run(
        loopback,
        tmp_path,
        script,
        toolset=make_toolset(saved=granted, grant=("save",)),
        deferred_tools=True,
    )
    assert granted == ["kept"]
    assert result.calls == (("write", True),)


@pytest.mark.parametrize("where", ("profile", "project"))
async def test_settings_that_unmount_the_tools_do_not_take_effect(loopback, tmp_path, where):
    """A profile or project that turns ``tools.xdev`` off, or inlines the device docs, is outranked."""
    conflicting = {"tools": {"xdev": False, "xdevDocs": "inline"}}
    model, agent_dir = loopback([_call("read", path="xd://lookup"), ("text", "done")])
    if where == "profile":
        config = Path(agent_dir) / "config.yml"
        merged = json.loads(config.read_text())
        merged.update(conflicting)
        config.write_text(json.dumps(merged))
    else:
        (tmp_path / ".omp").mkdir()
        (tmp_path / ".omp" / "config.yml").write_text(json.dumps(conflicting))
    result = await run_prompt(
        "go",
        **make_args(
            tmp_path, model=MODEL, omp_agent_dir=agent_dir, timeout=60, deferred_tools=True
        ),
    )
    assert result.error is None
    assert sorted(model.tool_names()) == ["read", "write"]
    assert "type Args" not in json.dumps(model.requests[0])
    assert "q: string" in model.tool_output("call_0")


async def test_a_project_extension_is_not_loaded_beside_the_guard(loopback, tmp_path):
    marker = tmp_path / "ran"
    extension = tmp_path / ".omp" / "extensions" / "mine.js"
    extension.parent.mkdir(parents=True)
    extension.write_text(
        "import fs from 'node:fs';\n"
        "export default function (pi) {\n"
        "  fs.appendFileSync(%s, 'loaded');\n"
        "  pi.on('tool_call', async () => { fs.appendFileSync(%s, 'called'); });\n"
        "}\n" % (json.dumps(str(marker)), json.dumps(str(marker)))
    )
    (tmp_path / ".omp" / "config.yml").write_text(json.dumps({"extensions": [str(extension)]}))
    script = [_call("read", path="xd://"), ("text", "done")]
    result, model = await _run(loopback, tmp_path, script, deferred_tools=True)
    assert result.error is None
    assert "xd://lookup" in model.tool_output("call_0")
    assert not marker.exists()


async def test_a_guard_omp_cannot_load_ends_the_run_before_the_model_is_asked(
    loopback, tmp_path, monkeypatch
):
    broken = tmp_path / "broken.js"
    broken.write_text("export default function (pi) { this is not javascript")
    monkeypatch.setattr(omp_module, "XD_GUARD", broken)
    result, model = await _run(
        loopback, tmp_path, [_call("read", path="/etc/hostname")], deferred_tools=True
    )
    assert result.error is not None and "guard" in result.error
    assert model.requests == []


async def test_a_replaced_system_prompt_still_lets_the_model_find_and_call_the_tools(
    loopback, tmp_path
):
    """omp's own xd:// text is not in a replaced prompt; Ember's generic note is."""
    script = [
        _call("read", path="xd://"),
        _call("read", path="xd://lookup"),
        _call("write", path="xd://lookup", content=json.dumps({"q": "board"})),
        ("text", "done"),
    ]
    result, model = await _run(
        loopback,
        tmp_path,
        script,
        deferred_tools=True,
        system="You audit a board.",
        system_mode="replace",
    )
    assert result.error is None
    instructions = model.requests[0]["instructions"]
    assert instructions.startswith("You audit a board.")
    assert "xd://" in instructions and "lookup" not in instructions
    assert "xd://lookup" in model.tool_output("call_0")
    assert "found board" in model.tool_output("call_2")
    assert sorted(model.tool_names()) == ["read", "write"]


async def test_a_command_file_cannot_stand_in_for_a_guard_omp_did_not_load(
    loopback, tmp_path, monkeypatch
):
    """``.omp/commands/<name>.md`` is listed under the guard's command name; it is not an extension."""
    commands = tmp_path / ".omp" / "commands"
    commands.mkdir(parents=True)
    (commands / "annealage-xd-guard.md").write_text("---\ndescription: spoof\n---\nhello\n")
    broken = tmp_path / "broken.js"
    broken.write_text("export default function (pi) { this is not javascript")
    monkeypatch.setattr(omp_module, "XD_GUARD", broken)
    result, model = await _run(
        loopback, tmp_path, [_call("read", path="/etc/hostname")], deferred_tools=True
    )
    assert result.error is not None and "guard" in result.error
    assert model.requests == []


async def test_instructions_that_mention_type_args_do_not_stop_a_deferred_run(loopback, tmp_path):
    result, model = await _run(
        loopback,
        tmp_path,
        [_call("read", path="xd://lookup"), ("text", "done")],
        deferred_tools=True,
        system="Write TypeScript as `type Args = {}` where it helps.",
    )
    assert result.error is None
    assert "type Args = {}" in json.dumps(model.requests[0])
    assert "q: string" in model.tool_output("call_0")


async def test_a_guard_without_a_device_list_blocks_every_call(loopback, tmp_path):
    """The environment variable is Ember's to set; if it were lost the guard must refuse, not open."""
    from omp_rpc import RpcClient

    model, agent_dir = loopback([_call("read", path="xd://"), ("text", "done")])
    env = loopback_env(agent_dir)
    env.pop("ANNEALAGE_XD_DEVICES", None)
    client = RpcClient(
        executable="omp",
        model=MODEL,
        cwd=str(tmp_path),
        env=env,
        tools=("read", "write"),
        no_session=True,
        extra_args=("--auto-approve", "--no-extensions", "-e", str(omp_module.XD_GUARD)),
    )
    client.start()
    try:
        client.prompt("go")
        client.wait_for_idle(timeout=60)
    finally:
        client.stop()
    assert model.tool_output("call_0").startswith("Blocked:")


NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "object",
            "properties": {"part": {"type": "string"}},
            "required": ["part"],
            "additionalProperties": False,
        }
    },
    "required": ["query"],
    "additionalProperties": False,
}


async def test_the_tool_sets_receipt_observer_sees_the_real_server_tool_and_arguments(
    loopback, tmp_path, monkeypatch
):
    """Behind ``write xd://fake__lookup`` the observer is still told server ``fake``, tool ``lookup``."""
    import test_remote_tools as remote_tests

    from annealage_ember import remote
    from annealage_ember.headless import ToolSet
    from annealage_ember.remote import RemoteServer
    from annealage_ember.tools import Grading

    monkeypatch.setattr(
        remote_tests, "ADVERTISED", [remote_tests._tool("lookup", "Find a part.", NESTED_SCHEMA)]
    )
    upstream = remote_tests.FakeRemote()
    receipts = []

    async def observer(*event):
        receipts.append(event)

    try:
        server = RemoteServer(
            "fake",
            upstream.url,
            Grading(read=("lookup",), view=(), write=()),
            prime=("begin", {}),
        )
        tools = ToolSet(
            tools=(),
            grading=Grading((), (), ()),
            remote=(server,),
            remote_snapshot=remote.discover((server,)),
            remote_result_observer=observer,
        )
        args = {"query": {"part": "RP2040"}}
        script = [
            _call("read", path="xd://"),
            _call("write", path="xd://fake__lookup", content="{}"),
            _call("write", path="xd://fake__lookup", content=json.dumps(args)),
            ("text", "done"),
        ]
        result, model = await _run(loopback, tmp_path, script, toolset=tools, deferred_tools=True)
    finally:
        upstream.stop()

    listing, rejected, answered = _outputs(model, 3)
    assert result.error is None
    assert "xd://fake__lookup" in listing
    assert "Invalid args" in rejected
    assert answered is not None and not answered.startswith("Blocked:")
    assert [event[:3] for event in receipts] == [("fake", "lookup", args)]
    assert upstream.calls.count("lookup") == 1
