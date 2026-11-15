"""Tests for the per-session tool scope (``tools.py``): ``host_tool_table(only=)``,
the hosted drop, ``bus.tool_scope``'s refusal at execute time, ``with_scope`` and
``scoped_host_tool_table``, driven through the toy product's tool server.

The launch wiring (``Product.session_tools``, ``Product.omp_base_prompt``) is
tested in ``test_omp_session.py`` beside the other ``launch.build_session`` tests,
and a remote's side in ``test_remote_tools.py``.
"""

import pytest
from test_tools import ARGS, FakeBus, text_of
from toy_product import NOTES_FILE, build_toy_tools

from annealage_ember import tools as tools_module
from annealage_ember.tools import namespaced, scope_refusal, with_scope

pytestmark = pytest.mark.asyncio

ALL = ["list_notes", "get_view", "set_view", "add_note", "clear_notes"]


@pytest.fixture
def project(tmp_path):
    (tmp_path / NOTES_FILE).write_text('["first"]', encoding="utf-8")
    return tmp_path


def _in(*names):
    return frozenset(names).__contains__


# ---------------------------------------------------------------------------
# host_tool_table(only=)
# ---------------------------------------------------------------------------


async def test_without_only_the_host_table_is_every_tool(project):
    assert list(build_toy_tools(FakeBus(), project).host_tool_table()) == ALL


async def test_only_restricts_the_table_in_its_own_order_and_ignores_unknown_names(project):
    server = build_toy_tools(FakeBus(), project)
    table = server.host_tool_table(only={"clear_notes", "list_notes", "no_such_tool"})
    assert list(table) == ["list_notes", "clear_notes"]
    # The same specs the full table holds, not copies with another meaning.
    assert table["clear_notes"].write is True
    assert server.host_tool_table(only=[]) == {}


async def test_the_full_tool_table_is_unchanged_by_only(project):
    server = build_toy_tools(FakeBus(), project)
    server.host_tool_table(only={"list_notes"})
    assert list(server.tool_table()) == ALL


async def test_hosted_mode_drops_tools_with_no_declared_operation(project):
    bus = FakeBus()
    bus.hosted_mode = True
    bus.hosted_tool_ops = {
        namespaced("toy", "list_notes"): "project.read",
        namespaced("toy", "add_note"): "project.write",
    }
    server = build_toy_tools(bus, project)
    assert list(server.host_tool_table()) == ["list_notes", "add_note"]
    # ``only`` narrows further, and cannot bring a refused tool back.
    assert list(server.host_tool_table(only={"add_note", "set_view"})) == ["add_note"]
    # The product's own full table is not the hosted one.
    assert list(server.tool_table()) == ALL


async def test_outside_hosted_mode_the_declared_operations_do_not_filter(project):
    bus = FakeBus()
    bus.hosted_mode = False
    bus.hosted_tool_ops = {namespaced("toy", "list_notes"): "project.read"}
    assert list(build_toy_tools(bus, project).host_tool_table()) == ALL


# ---------------------------------------------------------------------------
# bus.tool_scope at execute time
# ---------------------------------------------------------------------------


async def test_a_tool_outside_the_scope_is_refused_by_name_and_never_runs(project):
    bus = FakeBus()
    bus.tool_scope = _in("list_notes")
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}

    result = await handlers["add_note"](ARGS["add_note"])

    assert result["is_error"] is True
    text = text_of(result)
    assert "add_note" in text and "not part of this agent's work" in text
    assert text == scope_refusal("add_note")
    assert "the human" not in text.lower()
    # Nothing was written: the handler was never reached.
    assert (project / NOTES_FILE).read_text(encoding="utf-8") == '["first"]'


async def test_a_tool_inside_the_scope_runs(project):
    bus = FakeBus()
    bus.tool_scope = _in("list_notes")
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}
    result = await handlers["list_notes"](ARGS["list_notes"])
    assert not result.get("is_error")
    assert "first" in text_of(result)


async def test_no_scope_is_the_old_behaviour(project):
    bus = FakeBus()  # no tool_scope attribute at all
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}
    assert not (await handlers["add_note"](ARGS["add_note"])).get("is_error")
    bus.tool_scope = None
    assert not (await handlers["add_note"](ARGS["add_note"])).get("is_error")


async def test_every_tool_outside_the_scope_is_refused(project):
    bus = FakeBus()
    bus.tool_scope = _in()
    for tool in build_toy_tools(bus, project).tools:
        result = await tool.handler(ARGS[tool.name])
        assert result["is_error"] is True, tool.name
        assert tool.name in text_of(result)
    assert bus.calls == []


async def test_the_scope_is_checked_before_the_hosted_gate_and_the_pause_gate(project):
    bus = FakeBus()
    bus.tool_scope = _in("list_notes")
    bus.hosted_mode = True
    bus.hosted_tool_ops = {}
    bus.paused = True
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}
    # Out of scope: the scope's answer, not the hosted one or the paused one.
    assert text_of(await handlers["add_note"](ARGS["add_note"])) == scope_refusal("add_note")
    # In scope: the hosted gate still has its say.
    in_scope = await handlers["list_notes"](ARGS["list_notes"])
    assert "hosted operation" in text_of(in_scope)


async def test_a_scope_that_changes_applies_to_the_next_call(project):
    bus = FakeBus()
    bus.tool_scope = _in("list_notes")
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}
    assert (await handlers["get_view"](ARGS["get_view"]))["is_error"] is True
    bus.tool_scope = _in("list_notes", "get_view")
    assert not (await handlers["get_view"](ARGS["get_view"])).get("is_error")


# ---------------------------------------------------------------------------
# with_scope: a sub-agent's own scope
# ---------------------------------------------------------------------------


async def test_with_scope_judges_a_call_by_its_own_scope_not_the_buss(project):
    bus = FakeBus()
    bus.tool_scope = _in("list_notes")
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}

    child = with_scope(handlers["add_note"], _in("add_note"))
    assert not (await child(ARGS["add_note"])).get("is_error")

    refused = with_scope(handlers["list_notes"], _in("add_note"))
    assert text_of(await refused(ARGS["list_notes"])) == scope_refusal("list_notes")

    unrestricted = with_scope(handlers["clear_notes"], None)
    assert not (await unrestricted(ARGS["clear_notes"])).get("is_error")


async def test_with_scope_does_not_leak_into_the_next_call(project):
    bus = FakeBus()
    bus.tool_scope = _in("list_notes")
    handlers = {t.name: t.handler for t in build_toy_tools(bus, project).tools}
    await with_scope(handlers["add_note"], None)(ARGS["add_note"])
    assert tools_module._SCOPE_OVERRIDE.get() is tools_module._UNSET
    assert (await handlers["add_note"](ARGS["add_note"]))["is_error"] is True


# ---------------------------------------------------------------------------
# scoped_host_tool_table
# ---------------------------------------------------------------------------


async def test_the_scoped_table_follows_the_bus_scope(project):
    bus = FakeBus()
    server = build_toy_tools(bus, project)
    assert list(server.scoped_host_tool_table()) == ALL
    bus.tool_scope = _in("get_view", "add_note")
    assert list(server.scoped_host_tool_table()) == ["get_view", "add_note"]
    bus.tool_scope = None
    assert list(server.scoped_host_tool_table()) == ALL
