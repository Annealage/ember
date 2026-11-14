"""The agent's reply renderer (``static/markdown.js``), run under Node.

Checks what a reader sees (tables, lists, links, code) and, as important, that
nothing the model writes becomes live markup beyond the allowlisted tags.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

from annealage_ember import product

MD_JS = pathlib.Path(product.__file__).resolve().parent / "static" / "markdown.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

HARNESS = r"""
import { pathToFileURL } from "node:url";
const { renderMarkdown } = await import(pathToFileURL(process.argv[1]).href);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(renderMarkdown)));
"""


def render(*texts):
    out = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS, str(MD_JS), json.dumps(list(texts))],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_table_renders_with_alignment_and_inline_markup():
    (html,) = render("| Part | Qty |\n|:--|--:|\n| **U1** | `2` |\n| R1 | 4 |")
    assert "<table>" in html and "<th" in html
    assert '<th style="text-align:right">Qty</th>' in html
    assert '<td style="text-align:left"><strong>U1</strong></td>' in html
    assert '<td style="text-align:right"><code>2</code></td>' in html
    assert html.count("<tr>") == 3


def test_headings_lists_quotes_rules_and_tasks():
    (html,) = render(
        "## Title\n\n- a\n- b\n  - nested\n\n1. one\n2. two\n\n> quoted\n\n---\n\n- [x] done\n- [ ] todo"
    )
    assert "<h2>Title</h2>" in html
    assert "<li>b<ul><li>nested</li></ul></li>" in html
    assert "<ol><li>one</li><li>two</li></ol>" in html
    assert "<blockquote><p>quoted</p></blockquote>" in html
    assert "<hr>" in html
    assert "\u2611 done" in html and "\u2610 todo" in html


def test_code_is_literal_and_an_unclosed_fence_keeps_streaming_text():
    a, b = render("```py\nx = '<b>' * 2\n```\nafter", "```\npartial **text**")
    assert '<pre class="code">x = &#39;&lt;b&gt;&#39; * 2</pre>' in a
    assert "<p>after</p>" in a
    assert b == '<pre class="code">partial **text**</pre>'


def test_links_keep_only_safe_schemes():
    a, b = render(
        "see [docs](https://example.com/a?x=1&y=2) and https://example.org/z.",
        "[x](javascript:alert(1)) [y](data:text/html,hi)",
    )
    assert (
        '<a href="https://example.com/a?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">docs</a>'
        in a
    )
    assert 'href="https://example.org/z"' in a and a.rstrip("</p>").endswith(".")
    assert "<a" not in b


def test_allowed_html_renders_and_the_rest_is_inert():
    (html,) = render(
        '<table><tr><td colspan=2 onclick="x()">cell</td></tr></table>\n\n'
        "<script>alert(1)</script>\n\n"
        '<p>a <img src=x onerror=alert(1)> <a href="javascript:evil()">l</a> <b>bold</b></p>\n\n'
        "<div>unclosed"
    )
    assert '<td colspan="2">cell</td>' in html
    assert "onclick" not in html
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "<img" not in html and "&lt;img" in html
    assert "href" not in html
    assert "<b>bold</b>" in html
    assert html.count("<div>") == html.count("</div>")


def test_table_with_centered_single_dash_separator():
    (html,) = render("| a | b | c |\n|:-:|-|--:|\n| 1 | 2 | 3 |")
    assert "<table>" in html
    assert '<th style="text-align:center">a</th>' in html
    assert "|" not in html
