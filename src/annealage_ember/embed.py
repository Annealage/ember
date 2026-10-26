"""The chat page a product embeds, rendered once here instead of copied.

``render_agent_page`` returns a whole HTML document: the chat pane's markup
(the ids ``static/chat.js`` and its neighbours look up), the agent layer's
stylesheets and one module script, ``agent-page.js``, which reads its
configuration from ``data-ember-*`` attributes on the pane and calls
``bootstrapAgentPage``. There is no inline script or style, so the page needs
no hash in its policy, and ``agent_page_csp`` is the policy to serve it with
(it differs from the app's only in who may frame it).

Nothing here knows a product: a product's theme comes in as stylesheet and
script hrefs it names, and its own view lives outside the pane. A page that
needs hooks of its own links a module of its own that imports
``bootstrap.js`` instead of ``agent-page.js`` (``script=``).
"""

import html

from . import app as agent_app

_STATIC = "/agent/static/"
LAYOUTS = ("column",)


def agent_page_csp(frame_ancestors="'self'"):
    """The Content-Security-Policy for an embedded page: the app's policy with
    no inline script allowed, framed by ``frame_ancestors`` (a source list
    string, ``'self'`` by default). A response that already carries a policy
    keeps it, so serve the page with this header set."""
    return agent_app.policy((), frame_ancestors=frame_ancestors)


def chat_markup(
    *,
    title="Agent",
    model_control=True,
    session_button=True,
    attach_icon_href=None,
    status="Not started",
    hosted=False,
    start=None,
    session_url=None,
    review=True,
    settings=True,
):
    """The chat pane and the page-level elements its modules write to (toast,
    error line, settings modal), as one fragment. ``attach_icon_href`` is an
    SVG sprite reference for the attach button; without one it shows a plus."""
    attrs = ['id="chat"', 'aria-label="Agent"']
    if hosted:
        attrs.append('data-ember-hosted="true"')
    if start:
        attrs.append('data-ember-start="%s"' % html.escape(start, quote=True))
    if session_url:
        attrs.append('data-ember-session-url="%s"' % html.escape(session_url, quote=True))
    if not model_control:
        attrs.append('data-ember-model-control="false"')
    if not review:
        attrs.append('data-ember-review="false"')
    if not settings:
        attrs.append('data-ember-settings="false"')
    attach = (
        '<svg class="ico"><use href="%s"/></svg>' % html.escape(attach_icon_href, quote=True)
        if attach_icon_href
        else "+"
    )
    model_row = (
        '<div class="chatmodelrow"><label for="chatModelInput">Model</label>'
        '<input id="chatModelInput" type="text" placeholder="backend default" '
        'autocomplete="off" spellcheck="false"></div>'
        if model_control
        else ""
    )
    session = (
        '<button id="sessionStart" type="button" class="hdrbtn primary">Start session</button>'
        if session_button
        else ""
    )
    return (
        "<aside %s>\n"
        '<header><h1>%s</h1><span id="agentStatus" class="agentstatus">%s</span>\n'
        '<span class="working" role="status"><span class="bar" aria-hidden="true"></span>\n'
        'Working <b id="chatWorkingTime"></b></span>\n'
        '<span class="spacer"></span>\n'
        '<span id="connIndicator" class="conn"></span>\n'
        "%s\n"
        '<details id="chatMore" class="more"><summary class="hdrbtn">More</summary>\n'
        '<div class="menu">\n'
        '<button id="pauseBtn" type="button" class="hdrbtn" aria-pressed="false">'
        "<span data-label>Pause</span></button>\n"
        '<button id="settingsBtn" type="button" class="hdrbtn">Settings</button>\n'
        '<button id="chatExport" type="button" class="hdrbtn">Export</button></div></details></header>\n'
        '<div id="chatBanner" class="chatbanner" hidden><span id="chatBannerText"></span>\n'
        '<button id="chatBannerClose" type="button" aria-label="Dismiss">&times;</button></div>\n'
        '<div id="chatLog" aria-live="polite"></div>\n'
        '<div class="side"><div id="chatPending"></div>\n'
        '<div id="chatComposer">\n'
        "%s\n"
        '<div id="chatAttachStrip" class="attachstrip" hidden></div>\n'
        '<div id="chatComposerRow"><input id="chatFileInput" type="file" '
        'accept="image/png,image/jpeg,image/webp" multiple hidden>\n'
        '<button id="chatAttachBtn" type="button" class="attachbtn" aria-label="Attach an image" '
        'title="Attach an image (or paste or drop one into the message box)">%s</button>\n'
        '<textarea id="chatInput" rows="2" aria-label="Message" placeholder="Message the agent"></textarea>\n'
        '<div class="chatbtns"><button id="chatInterrupt" type="button" title="Stop the current turn">'
        "Interrupt</button>\n"
        '<button id="chatSend" type="button" class="primary">Send</button></div>\n'
        "</div></div></div>\n"
        '<div id="settingsModal" class="modal" hidden></div>\n'
        '<div id="toast" role="status"></div><div id="err" role="alert"></div>\n'
        "</aside>"
    ) % (" ".join(attrs), html.escape(title), html.escape(status), session, model_row, attach)


def render_agent_page(
    *,
    product,
    title,
    url_prefix="",
    hosted=False,
    layout="column",
    tokens=(),
    stylesheets=(),
    scripts=(),
    script=None,
    session_url=None,
    start=None,
    **markup,
):
    """A complete embeddable chat page.

    ``url_prefix`` is where the app is mounted ("" or "/p/demo"): the agent
    layer's assets are linked under it, so the page works behind a front door.
    ``scripts`` (external, e.g. a colour-scheme script), ``tokens`` (the
    product's variables) and ``stylesheets`` are hrefs for the product's own
    theme: scripts and tokens come before the agent layer's ``agent.css`` and
    the layout's ``agent-page.css``, ``stylesheets`` after them, each in the
    order given. ``script`` replaces
    ``agent-page.js`` as the module entry, for a page with hooks. ``hosted`` and
    ``session_url`` (the hosted session route, with its prefix) configure the
    pane; the rest (``model_control``, ``status``, ``attach_icon_href`` and so
    on) is ``chat_markup``'s."""
    if layout not in LAYOUTS:
        raise ValueError("unknown layout %r; the layouts are %s" % (layout, ", ".join(LAYOUTS)))
    base = url_prefix.rstrip("/") + _STATIC
    q = html.escape
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
    ]
    head.append("<title>%s</title>" % q(title))
    head += ['<script src="%s"></script>' % q(href, quote=True) for href in scripts]
    head += ['<link rel="stylesheet" href="%s">' % q(href, quote=True) for href in tokens]
    head.append('<link rel="stylesheet" href="%sagent.css">' % base)
    head.append('<link rel="stylesheet" href="%sagent-page.css">' % base)
    head += ['<link rel="stylesheet" href="%s">' % q(href, quote=True) for href in stylesheets]
    entry = script or base + "agent-page.js"
    return (
        '<!doctype html>\n<html lang="en" data-product="%s"><head>%s</head>\n<body>%s\n'
        '<script type="module" src="%s"></script></body></html>'
        % (
            q(product, quote=True),
            "\n".join(head),
            chat_markup(
                title="Agent", hosted=hosted, session_url=session_url, start=start, **markup
            ),
            q(entry, quote=True),
        )
    )
