"""The request a Codex Responses call sends: input items, body and headers.

The caller supplies the literal, ordered ``input`` list and this module
serialises it as given. It never resizes, re-encodes, merges or reorders an
image or a message, because the point of a direct client is that the bytes
the model sees are the bytes the caller chose. ``text_item`` and
``image_item`` produce the two message shapes the endpoint accepts (each a
``{"role": "user", "content": [...]}`` message holding one ``input_text`` or
``input_image`` part), and ``CodexRequest.body`` lays the fields out in the
same order the reference client used, so a request is reproducible from its
journal record.

``redact_images`` exists for the journal. It replaces each inline image data
URI with its MIME type, byte count and SHA-256, so a record names the exact
image sent without carrying it.

Client identity (``originator`` and ``version``) has no default. Those two
headers tell the provider which client is calling, and a library that
invented them would be impersonating one; ``ClientIdentity`` therefore has to
be supplied by whoever owns the integration.
"""

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Printable Latin-1 plus tab: the characters http.client can encode and a
# header line can carry without splitting.
_VALUE = re.compile(r"[\t\x20-\x7e\x80-\xff]*")

# Headers the client itself owns. A caller-supplied header with one of these
# names is refused rather than allowed to replace it: a silent override of
# Authorization or the framing headers would defeat the guarantees the rest
# of the package makes about what is sent.
_RESERVED = frozenset(
    {
        "authorization",
        "chatgpt-account-id",
        "openai-beta",
        "originator",
        "version",
        "session-id",
        "thread-id",
        "x-client-request-id",
        "accept",
        "accept-encoding",
        "content-type",
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "host",
    }
)


def sniff_image_mime(raw):
    """Return the MIME type of PNG, JPEG, WebP or GIF bytes from their magic.

    The type is read from the leading bytes, never from a file name, because
    the data URI must describe what the bytes are. Anything else raises
    ``ValueError``: the endpoint is not offered formats this client cannot
    name.
    """
    for magic, mime in _MAGIC:
        if raw.startswith(magic):
            return mime
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("image must be a PNG, JPEG, WebP or GIF file")


def text_item(text):
    """One user message holding a single ``input_text`` part."""
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def image_item(path_or_bytes, *, detail):
    """One user message holding a single ``input_image`` part.

    ``path_or_bytes`` is a file path (``str`` or ``os.PathLike``) or the image
    bytes themselves. The bytes are embedded unchanged as a base64 data URI;
    ``detail`` is passed through to the provider as given.
    """
    if not isinstance(detail, str) or not detail:
        raise ValueError("detail must be a non-empty str")
    if isinstance(path_or_bytes, (str, os.PathLike)):
        raw = Path(path_or_bytes).read_bytes()
    elif isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        raw = bytes(path_or_bytes)
    else:
        raise TypeError("image must be a path or bytes")
    uri = "data:%s;base64,%s" % (sniff_image_mime(raw), base64.b64encode(raw).decode("ascii"))
    return {
        "role": "user",
        "content": [{"type": "input_image", "detail": detail, "image_url": uri}],
    }


@dataclass(frozen=True)
class ClientIdentity:
    """The ``originator`` and ``version`` headers; the caller must supply both."""

    originator: str
    version: str

    def __post_init__(self):
        for name in ("originator", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or not _VALUE.fullmatch(value):
                raise ValueError("%s must be a non-empty header-safe str" % name)


@dataclass(frozen=True)
class CodexRequest:
    """The body of one ``POST /codex/responses`` call.

    ``input`` is the literal ordered item list. ``effort`` (``reasoning``),
    ``include`` and ``prompt_cache_key`` are omitted from the body when unset.
    The body always streams and carries no sampling or output-length fields,
    since the endpoint chooses those and a client that set them would be
    changing what the caller measures.
    """

    model: str
    instructions: str
    input: tuple
    effort: str | None = None
    include: tuple = ()
    prompt_cache_key: str | None = None
    store: bool = False

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model must be a non-empty str")
        if not isinstance(self.instructions, str):
            raise TypeError("instructions must be a str")
        items = tuple(self.input)
        if not items or not all(isinstance(item, dict) for item in items):
            raise ValueError("input must be a non-empty sequence of item dicts")
        object.__setattr__(self, "input", items)
        object.__setattr__(self, "include", tuple(self.include))

    def body(self):
        """The request body as a dict, in the reference client's field order."""
        body = {
            "model": self.model,
            "instructions": self.instructions,
            "input": list(self.input),
            "store": self.store,
            "stream": True,
        }
        if self.effort is not None:
            body["reasoning"] = {"effort": self.effort}
        if self.include:
            body["include"] = list(self.include)
        if self.prompt_cache_key is not None:
            body["prompt_cache_key"] = self.prompt_cache_key
        return body

    def body_bytes(self):
        """The exact bytes sent: ``json.dumps`` defaults, as the reference client sent."""
        return json.dumps(self.body()).encode("utf-8")


def check_header(name, value):
    if not _TOKEN.fullmatch(name):
        raise ValueError("invalid header name %r" % name)
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        # The value is never echoed: it may be a credential.
        raise ValueError("header %r has an invalid value" % name)


def validate_extra_headers(extra_headers):
    """Return a checked copy of caller headers, refusing any the client owns."""
    checked = {}
    for name, value in (extra_headers or {}).items():
        check_header(name, value)
        if name.lower() in _RESERVED:
            raise ValueError("header %r is set by the client and cannot be overridden" % name)
        checked[name] = value
    return checked


def build_headers(
    *, access_token, account_id, identity, attempt_id, session_id, thread_id, extra_headers=None
):
    """The full header set for one attempt, ``Authorization`` first.

    ``x-client-request-id`` is the attempt id, so a provider-side log line can
    be tied to exactly one journal record. A token or account id that cannot
    be carried in a header raises ``ValueError`` naming the header only.
    """
    headers = {
        "Authorization": "Bearer " + access_token,
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": identity.originator,
        "version": identity.version,
        "session-id": session_id,
        "thread-id": thread_id,
        "x-client-request-id": attempt_id,
        "accept": "text/event-stream",
        "content-type": "application/json",
        "accept-encoding": "identity",
    }
    for name, value in headers.items():
        check_header(name, value)
    if not access_token or not account_id:
        raise ValueError("access token and account id must be non-empty")
    headers.update(validate_extra_headers(extra_headers))
    return headers


def _data_uri_image(uri):
    head, _, payload = uri.partition(",")
    mime = head[5:].split(";", 1)[0] or None
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError:
        # Not decodable, but still never copied into a record.
        digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        return {"mime_type": mime, "bytes": len(uri), "sha256": digest, "undecodable": True}, None
    return {"mime_type": mime, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, raw


def redact_images(body):
    """Return ``(body, images)`` with inline image data replaced by metadata.

    Every ``input_image`` part whose ``image_url`` is a ``data:`` URI has the
    URI replaced by ``{"mime_type", "bytes", "sha256"}``. ``images`` lists a
    ``(metadata, raw bytes)`` pair per replacement, in request order, so a
    journal can store the blob by digest. The input is not modified.
    """
    found = []

    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        uri = node.get("image_url")
        inline = (
            node.get("type") == "input_image" and isinstance(uri, str) and uri.startswith("data:")
        )
        meta = None
        if inline:
            meta, raw = _data_uri_image(uri)
            found.append((meta, raw))
        return {k: meta if inline and k == "image_url" else walk(v) for k, v in node.items()}

    return walk(body), found
