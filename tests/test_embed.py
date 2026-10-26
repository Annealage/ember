"""The embeddable chat page: every element the shared modules look up is
there, nothing in it needs an inline script or style, and the prefix and
frame policy are what a product mounting it relies on."""

import re
from pathlib import Path

import pytest

from annealage_ember import embed

STATIC = Path(embed.__file__).resolve().parent / "static"
# Looked up by other modules than the pane (pause, settings, ui, ws) or by the
# bootstrap itself.
PAGE_LEVEL = {
    "pauseBtn",
    "settingsBtn",
    "settingsModal",
    "toast",
    "err",
    "connIndicator",
    "chatWorkingTime",
    "chatMore",
    "sessionStart",
}


def pane_ids():
    source = (STATIC / "chat.js").read_text(encoding="utf-8")
    block = source[source.index("export const DEFAULT_IDS") :]
    return set(re.findall(r':\s*"([A-Za-z]+)"', block[: block.index("});")]))


def ids_in(page):
    return set(re.findall(r'\bid="([^"]+)"', page))


def test_the_page_has_every_element_the_shared_modules_look_up():
    page = embed.render_agent_page(product="x", title="X")
    missing = (pane_ids() | PAGE_LEVEL) - ids_in(page)
    assert not missing


def test_a_pane_without_a_model_control_drops_only_that_row():
    page = embed.render_agent_page(product="x", title="X", model_control=False)
    assert "chatModelInput" not in ids_in(page)
    assert 'data-ember-model-control="false"' in page
    assert (pane_ids() - {"modelInput", "chatModelInput"}) <= ids_in(page)


def test_the_page_needs_no_inline_script_or_style():
    page = embed.render_agent_page(
        product="x", title="X", hosted=True, session_url="/p/a/agent/session"
    )
    assert not re.search(r"<script\b(?![^>]*\bsrc=)[^>]*>", page)
    assert not re.search(r"<style\b|\sstyle=", page)
    assert "'unsafe-inline'" not in embed.agent_page_csp()
    assert "script-src 'self';" in embed.agent_page_csp()


def test_assets_are_linked_under_the_mount_prefix_and_theme_order_is_kept():
    page = embed.render_agent_page(
        product="x",
        title="X",
        url_prefix="/p/demo/",
        scripts=["/s/scheme.js"],
        tokens=["/s/tokens.css"],
        stylesheets=["/s/theme.css"],
    )
    hrefs = re.findall(r'(?:href|src)="([^"]+)"', page)
    assert hrefs == [
        "/s/scheme.js",
        "/s/tokens.css",
        "/p/demo/agent/static/agent.css",
        "/p/demo/agent/static/agent-page.css",
        "/s/theme.css",
        "/p/demo/agent/static/agent-page.js",
    ]


def test_a_page_with_its_own_entry_replaces_the_shared_one():
    page = embed.render_agent_page(product="x", title="X", script="/x/chat.js")
    assert 'src="/x/chat.js"' in page and "agent-page.js" not in page


def test_hosted_configuration_is_data_on_the_pane_and_escaped():
    page = embed.render_agent_page(
        product='x"y',
        title="<b>",
        hosted=True,
        session_url='/p/a"b/agent/session',
    )
    assert 'data-ember-hosted="true"' in page
    assert 'data-ember-session-url="/p/a&quot;b/agent/session"' in page
    assert "<title>&lt;b&gt;</title>" in page and 'data-product="x&quot;y"' in page
    standalone = embed.render_agent_page(product="x", title="X")
    assert "data-ember-hosted" not in standalone and "data-ember-session-url" not in standalone


def test_the_frame_policy_is_the_callers_and_the_rest_is_the_apps():
    from annealage_ember import app

    csp = embed.agent_page_csp("'self' https://studio.example")
    assert csp.endswith("frame-ancestors 'self' https://studio.example")
    assert csp.rsplit("; ", 1)[0] == app.policy(()).rsplit("; ", 1)[0]
    assert app.content_security_policy(Path("/absent.html")).endswith("frame-ancestors 'none'")


def test_an_unknown_layout_is_refused():
    with pytest.raises(ValueError):
        embed.render_agent_page(product="x", title="X", layout="rows")
