"""A direct, journalled client for the ChatGPT Codex Responses endpoint.

This package sends one request to ``POST <base>/codex/responses`` and records
exactly what happened, using an access token that omp's auth broker already
keeps fresh. It is independent of the omp launched sessions in ``session`` and
``headless``: nothing there changes, and nothing here imports them.

Three rules shape every module in it. Ember only reads credentials: it never
refreshes a token, holds a refresh token or writes omp's vault, and the broker
client refuses any route but the two read-only ones before it opens a socket
(``credentials``). One attempt is at most one HTTP POST, with no retry,
redirect, proxy or alternative encoding, so a second send is always a new,
explicit call (``transport``, ``client``). And billing is never inferred: an
attempt is ``usage_reported`` only when the provider returned a usage object,
and otherwise ``unattested``, however it ended (``journal``).

    request.py      input items, the body and the headers
    transport.py    the one-shot POST and the incremental SSE decoder
    journal.py      the append-only fsynced record of each attempt
    client.py       ``send``: lease, replay gate, post, classify, journal
    credentials.py  read-only credential leases from the omp auth broker
"""

from .client import DEFAULT_BASE_URL, AttemptResult, ReplayRefused, send
from .credentials import (
    BrokerCredentialSource,
    CredentialLease,
    CredentialSource,
    CredentialUnavailable,
)
from .journal import UNATTESTED, AttemptLog, Billing, Journal
from .request import ClientIdentity, CodexRequest, image_item, text_item
from .transport import (
    NotSent,
    SseEvent,
    SseLimitExceeded,
    SseParser,
    TransportResult,
    post_sse,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "UNATTESTED",
    "AttemptLog",
    "AttemptResult",
    "Billing",
    "BrokerCredentialSource",
    "ClientIdentity",
    "CodexRequest",
    "CredentialLease",
    "CredentialSource",
    "CredentialUnavailable",
    "Journal",
    "NotSent",
    "ReplayRefused",
    "SseEvent",
    "SseLimitExceeded",
    "SseParser",
    "TransportResult",
    "image_item",
    "post_sse",
    "send",
    "text_item",
]
