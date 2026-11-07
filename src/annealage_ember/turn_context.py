"""Turning a message's ``context`` blocks into what the agent reads.

A product page attaches context to a message (``static/chat.js``'s
``setContext`` and ``sendMessage``): a chip with a ``label`` the person reads
and ``data``, the product's own JSON object. The turn frame carries each as a
``{"type": "context", "label", "data"}`` block (``protocol.py`` bounds it),
and the event log keeps the blocks exactly as sent, so the conversation draws
the chip, live and after a reload, and a click on it hands ``data`` back to the
page.

The backends know nothing of the block. ``agent_blocks`` replaces each one,
before the turn is counted and handed on, with what the installed product's
``resolve_context`` made of it: one text block, marked as context the
product owner attached and naming the chip, then an ``image_path`` block for
each image the product returned, written under ``images/`` like an upload so
``turn_images`` expands it on every backend the way it expands an attached
picture. The message's own text and images keep their places in front.

Nothing is ever dropped silently. A product with no ``resolve_context``, or
one that raises, makes ``agent_blocks`` raise ``ContextRefused`` with a
sentence for the person, and the caller refuses the message back to the page
(``http/ws.py``), which puts it, chips included, back in the composer. A turn
sent again after a restart (``app.py``'s redelivery) cannot be refused to
anyone, so it gets ``refusal_notes=True``: a context that will not resolve
becomes a text block saying so in its place.
"""

import asyncio
import os
import sys

from . import files, product

#: How the agent is told where a context attachment starts and ends. Every
#: backend reads it as part of the person's turn, so it says who attached it.
_OPEN = "[Context the product owner attached to this message: %s]"
_CLOSE = "[End of attached context]"

#: The ``images/`` slug a resolved context's image is written under.
IMAGE_SLUG = "context"


class ContextRefused(Exception):
    """A message's context could not be resolved; the message is the sentence
    the person who sent it is shown."""


def has_context(blocks):
    """Whether ``blocks`` carries any ``context`` block."""
    return any(isinstance(b, dict) and b.get("type") == "context" for b in blocks or ())


async def agent_blocks(blocks, *, bus, serve_dir, refusal_notes=False):
    """``blocks`` with every ``context`` block replaced by what the agent reads
    (this module's docstring). A list with no context block comes back as it
    is, without the product being asked anything.

    Raises ``ContextRefused`` when the product has no ``resolve_context`` or
    it fails, unless ``refusal_notes``, which puts a text block saying why in
    the context's place instead.
    """
    if not has_context(blocks):
        return blocks
    resolver = product.current().resolve_context
    loop = asyncio.get_running_loop()
    out = []
    for block in blocks:
        if not (isinstance(block, dict) and block.get("type") == "context"):
            out.append(block)
            continue
        label = str(block.get("label", "")).strip()
        try:
            if resolver is None:
                raise ContextRefused(
                    "%s does not take attached context, so this message was not sent"
                    % product.current().title
                )
            out.extend(
                await loop.run_in_executor(
                    None, _resolve_one, resolver, bus, serve_dir, label, block.get("data")
                )
            )
        except ContextRefused as exc:
            if not refusal_notes:
                raise
            out.append(_note(label, str(exc)))
    return out


def _note(label, reason):
    return {
        "type": "text",
        "text": "%s\n(It could not be resolved: %s)\n%s" % (_OPEN % label, reason, _CLOSE),
    }


def _resolve_one(resolver, bus, serve_dir, label, data):
    """One ``context`` block's replacement blocks, run on an executor thread
    (the product's resolver may read files and crop renders)."""
    try:
        resolved = resolver(bus, serve_dir, data)
    except product.ContextError as exc:
        raise ContextRefused("%s: %s" % (label, exc)) from exc
    except Exception as exc:
        sys.stderr.write("warning: resolving context %r failed: %r\n" % (label, exc))
        raise ContextRefused(
            "%s could not be resolved (%s: %s)" % (label, type(exc).__name__, exc)
        ) from exc
    if not isinstance(resolved, product.ResolvedContext):
        raise ContextRefused(
            "%s could not be resolved: the product answered %r" % (label, type(resolved).__name__)
        )
    text = str(resolved.text).strip()
    out = [{"type": "text", "text": "%s\n%s\n%s" % (_OPEN % label, text, _CLOSE)}]
    for data in resolved.images:
        out.append({"type": "image_path", "path": _write_image(serve_dir, label, data)})
    return out


def _write_image(serve_dir, label, data):
    """Write one resolved image under ``images/`` and return its ``image_path``
    path; ``ContextRefused`` when it is not a PNG, JPEG or WEBP or cannot be
    written."""
    sniffed = files.sniff_image(bytes(data[: files.SNIFF_MIN_BYTES]))
    if sniffed is None:
        raise ContextRefused(
            "%s could not be resolved: its image is not a PNG, JPEG or WEBP" % label
        )
    suffix = sniffed[1]
    try:
        created = files.create_unique_image_file(serve_dir, IMAGE_SLUG, suffix)
    except (FileExistsError, OSError) as exc:
        raise ContextRefused("%s: its image could not be written (%s)" % (label, exc)) from exc
    if created is None:
        raise ContextRefused("%s: its image could not be written under images/" % label)
    fd, target = created
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise ContextRefused("%s: its image could not be written (%s)" % (label, exc)) from exc
    return "%s/%s" % (files.IMAGES_DIRNAME, target.name)
