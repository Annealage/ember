"""An append-only, fsynced record of every attempt to call the Codex endpoint.

The journal exists so that what happened to a paid request is never a matter
of memory. Each attempt leaves records in ``attempts.jsonl`` in a directory
the caller names, and a raw copy of the response stream beside it. A record
is written whole with ``O_APPEND``, then flushed and ``fsync``ed before the
next step runs. That ordering is the point: ``started`` is durable before the
connection opens and ``sent`` is durable before the first request byte is
written, so after a crash the journal can say which of "never left the
process" and "may have been billed" is true, and never has to guess.

Record kinds, each with ``kind``, ``attempt_id`` (a uuid4) and ``ts`` (UTC):

``started``
    Written after the credential lease succeeded and immediately before the
    connection opens. Carries ``retry_of``, ``url``, ``model``, ``request``
    (the exact body sent, with each inline image replaced by
    ``{"mime_type", "bytes", "sha256"}``), ``request_bytes`` and
    ``request_sha256`` (of the body as sent), ``headers`` (every request
    header except ``Authorization`` and other credential-bearing ones, whose
    names are listed in ``headers_omitted``) and ``credential``
    (``credential_id``, ``identity_key``, ``token_digest``, ``generation``,
    ``account_id``). The access token itself is never recorded, only its
    digest.
``sent``
    Written immediately before the first request byte. Its presence means the
    provider may have received the request.
``finished``
    Closes a ``started`` attempt. Carries ``outcome`` (``not_sent``,
    ``http_rejected``, ``sent_no_response``, ``stream_incomplete``,
    ``failed_event`` or ``completed``), ``status``, ``reason`` and ``detail``
    (``connect_failed`` and its cause, for a ``not_sent`` outcome only),
    ``response_headers`` (``[name, value]`` pairs, every header except
    ``Set-Cookie``), ``response_body`` and ``response_body_truncated`` (a
    non-2xx reply, at most 64 KiB), ``billing``, ``served_model``,
    ``terminal_event``, ``text`` (the output text, for convenience only),
    ``raw`` (see below), ``error``, ``error_phase`` and ``elapsed_ms``.
``not_sent``
    A whole attempt that never reached ``started``: no credential could be
    leased (``reason`` and ``detail`` say why), or the credential cannot be
    carried in a header. Carries ``outcome: "not_sent"``, ``billing`` and the
    request.
``replay_refused``
    A retry the replay gate stopped before ``started``: the lease holds the
    same token that ``retry_of`` was rejected with (HTTP 401 or 403).

``billing`` is always ``{"state", "usage"}`` with ``state`` either
``usage_reported`` (the provider returned a usage object, stored verbatim) or
``unattested`` (``usage`` is ``None``). Nothing the client observes, short of
a usage object, is evidence about billing, so no record infers it.

A 2xx response body is written to ``<attempt_id>.sse`` as it arrives. The file
is the bytes received, verbatim, with one exception: the access token (and its
JSON-escaped form) is replaced by ``[redacted]`` before it reaches disk, so a
server that echoes the credential cannot leave it in the journal even if the
process dies. To catch a token split across chunks, the last ``len(token) -
1`` bytes of the stream are held in memory until the next chunk or until
``finish``/``abort`` writes them. A process killed outright (``SIGKILL``) can
therefore lose up to that many trailing bytes of a partial stream; anything
earlier is on disk, because each chunk is written through without buffering.
When a stream ends partway through the token, the held-back tail is flushed
with a trailing token prefix of 8 bytes or more replaced too, so most of the
credential is not left behind.
``<attempt_id>.chunks.jsonl`` has one ``{"t_ms", "nbytes", "stored"}`` line
per received chunk: ``nbytes`` is what was received, ``stored`` what that
call appended to the ``.sse`` file after redaction and holdback (so the
``stored`` values sum to the file size, and the ``nbytes`` values to
``received_bytes``). A final line with ``"flush": true`` and ``nbytes`` 0
accounts for held-back bytes written at the end. ``finished.raw`` holds the
file names, ``bytes`` and ``sha256`` (both of the stored file),
``received_bytes``, the chunk count and ``redacted`` (true if the token was
replaced). The raw file is the evidence; the parsed ``text`` is a convenience.

Durability: the directory is fsynced when the two evidence files are created,
so a ``finished`` record never names a file whose directory entry is missing.
Received chunks are safe against a process kill as soon as they are written,
but are fsynced to disk only when the attempt finishes or aborts, so after a
power loss a partial stream may be shorter than what was received.

``Journal.claim`` reserves an attempt id by creating ``claims/<attempt_id>``
with ``O_CREAT | O_EXCL`` (file and directory fsynced), so concurrent threads
or processes sharing a directory cannot both use one id.

``Journal.unfinished`` lists attempts that have ``started`` but no
``finished`` (a crash or a kill): ``sent_unknown`` if ``sent`` was written,
else ``abandoned_before_send``, both with unattested billing. Readers skip any
line that is not a complete JSON object, which is how a record cut short by a
crash appears; the next append starts on a fresh line.
"""

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .request import redact_images

JOURNAL_FILE = "attempts.jsonl"
REDACTED = "[redacted]"
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MIN_PREFIX = 8

# Request headers whose values are credentials: omitted from the journal.
_SENSITIVE = re.compile(r"authorization|cookie|token|secret|password|api-?key", re.IGNORECASE)


@dataclass(frozen=True)
class Billing:
    """``usage_reported`` with the provider's usage object, or ``unattested`` with none."""

    state: str
    usage: dict | None = None

    def __post_init__(self):
        if self.state == "usage_reported":
            valid = isinstance(self.usage, dict) and bool(self.usage)
        else:
            valid = self.state == "unattested" and self.usage is None
        if not valid:
            raise ValueError("billing is usage_reported with usage, or unattested without")

    @classmethod
    def from_usage(cls, usage):
        """``usage_reported`` for a non-empty usage dict, otherwise ``unattested``."""
        if isinstance(usage, dict) and usage:
            return cls("usage_reported", usage)
        return cls("unattested")

    def as_dict(self):
        return {"state": self.state, "usage": self.usage}


UNATTESTED = Billing("unattested")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_all(target, data):
    view = memoryview(data)
    while view:
        view = view[target.write(view) :]


def _create(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb", buffering=0)


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Journal:
    """The journal directory: ``attempts.jsonl``, raw streams and optional image blobs.

    ``store_images`` also writes each inline image to ``blobs/<sha256>`` when
    a request is recorded, so a record's image digest can be resolved to the
    bytes. It is off by default because images are large and often private.
    Appends are serialised within a process by a lock and across processes by
    ``O_APPEND``.
    """

    def __init__(self, directory, *, store_images=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / JOURNAL_FILE
        self.store_images = store_images
        self._lock = threading.Lock()
        fresh = not self.path.exists()
        os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        if fresh:
            _fsync_dir(self.directory)

    def raw_path(self, attempt_id):
        return self.directory / (attempt_id + ".sse")

    def chunks_path(self, attempt_id):
        return self.directory / (attempt_id + ".chunks.jsonl")

    def _append(self, record, secrets=()):
        line = json.dumps(record, separators=(",", ":"))
        for secret in secrets:
            if secret:
                for form in {secret, json.dumps(secret)[1:-1]}:
                    line = line.replace(form, REDACTED)
        data = line.encode("utf-8") + b"\n"
        with self._lock:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    # A crash cut the last record short; keep this one on its own line.
                    data = b"\n" + data
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)

    def _request_fields(self, url, body):
        redacted, images = redact_images(json.loads(body))
        if self.store_images:
            self._store_blobs(images)
        return {
            "url": url,
            "model": redacted.get("model"),
            "request": redacted,
            "request_bytes": len(body),
            "request_sha256": hashlib.sha256(body).hexdigest(),
        }

    def _store_blobs(self, images):
        blobs = self.directory / "blobs"
        blobs.mkdir(exist_ok=True, mode=0o700)
        for meta, raw in images:
            target = blobs / meta["sha256"]
            if raw is None or target.exists():
                continue
            temp = blobs / (".%s.%d.tmp" % (meta["sha256"], threading.get_ident()))
            with _create(temp) as handle:
                _write_all(handle, raw)
                os.fsync(handle.fileno())
            os.replace(temp, target)
        _fsync_dir(blobs)

    def claim(self, attempt_id):
        """Reserve ``attempt_id`` for one attempt; ``FileExistsError`` if it is taken.

        Creates ``claims/<attempt_id>`` with ``O_CREAT | O_EXCL`` and fsyncs the
        file and its directory, so of any number of threads or processes
        sharing this directory exactly one gets the id. An id that already has
        records (a journal from before claims) counts as taken too.
        """
        if not _ID.fullmatch(attempt_id):
            raise ValueError("attempt_id must be a lowercase uuid string")
        claims = self.directory / "claims"
        fresh = not claims.exists()
        claims.mkdir(exist_ok=True, mode=0o700)
        os.close(os.open(claims / attempt_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        _fsync_dir(claims)
        if fresh:
            _fsync_dir(self.directory)
        if self.history(attempt_id):
            raise FileExistsError(attempt_id)

    def start(self, *, attempt_id, retry_of, url, body, headers, credential, secrets):
        """Write ``started`` and return the ``AttemptLog`` for the attempt.

        ``body`` is the exact request bytes. ``headers`` may include
        ``Authorization``; credential-bearing headers are left out of the
        record. ``secrets`` are strings that must never reach a record.
        """
        kept = {k: v for k, v in headers.items() if not _SENSITIVE.search(k)}
        record = {
            "kind": "started",
            "attempt_id": attempt_id,
            "retry_of": retry_of,
            "ts": _now(),
            **self._request_fields(url, body),
            "headers": kept,
            "headers_omitted": [k for k in headers if k not in kept],
            "credential": credential,
        }
        self._append(record, secrets)
        return AttemptLog(self, attempt_id, secrets)

    def not_sent(
        self, *, attempt_id, retry_of, url, body, reason, detail, credential=None, secrets=()
    ):
        """Write a ``not_sent`` record for an attempt that never reached ``started``."""
        self._append(
            {
                "kind": "not_sent",
                "attempt_id": attempt_id,
                "retry_of": retry_of,
                "ts": _now(),
                **self._request_fields(url, body),
                "outcome": "not_sent",
                "reason": reason,
                "detail": detail,
                "credential": credential,
                "billing": UNATTESTED.as_dict(),
            },
            secrets,
        )

    def replay_refused(self, *, attempt_id, retry_of, credential, prior_status):
        """Write a ``replay_refused`` record."""
        self._append(
            {
                "kind": "replay_refused",
                "attempt_id": attempt_id,
                "retry_of": retry_of,
                "ts": _now(),
                "credential": credential,
                "prior_status": prior_status,
                "reason": "same_token_rejected",
            }
        )

    def records(self):
        """Every complete record, in order; a line cut short by a crash is skipped."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        records = []
        for line in data.split(b"\n"):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and "kind" in record and "attempt_id" in record:
                records.append(record)
        return records

    def history(self, attempt_id):
        """The records of one attempt, in order."""
        return [r for r in self.records() if r["attempt_id"] == attempt_id]

    def unfinished(self):
        """Attempts that started but never finished, oldest first.

        Each entry is ``{"attempt_id", "outcome", "billing", "started", "raw"}``.
        ``outcome`` is ``sent_unknown`` if a ``sent`` record exists, else
        ``abandoned_before_send``; ``billing`` is unattested either way; ``raw``
        is ``{"file", "bytes"}`` for whatever stream bytes reached disk, or
        ``None``. An attempt still running in another thread or process is
        listed too.
        """
        by_attempt = {}
        for record in self.records():
            by_attempt.setdefault(record["attempt_id"], []).append(record)
        result = []
        for attempt_id, records in by_attempt.items():
            kinds = {r["kind"] for r in records}
            if "started" not in kinds or "finished" in kinds:
                continue
            raw = self.raw_path(attempt_id)
            result.append(
                {
                    "attempt_id": attempt_id,
                    "outcome": "sent_unknown" if "sent" in kinds else "abandoned_before_send",
                    "billing": UNATTESTED.as_dict(),
                    "started": next(r for r in records if r["kind"] == "started"),
                    "raw": {"file": raw.name, "bytes": raw.stat().st_size}
                    if raw.exists()
                    else None,
                }
            )
        return result


class AttemptLog:
    """The journal's side of one started attempt: ``sent``, raw stream, ``finished``."""

    def __init__(self, journal, attempt_id, secrets):
        self._journal = journal
        self.attempt_id = attempt_id
        self.raw_path = journal.raw_path(attempt_id)
        self.chunks_path = journal.chunks_path(attempt_id)
        self._secrets = tuple(s for s in secrets if s)
        forms = {f for s in self._secrets for f in (s, json.dumps(s)[1:-1])}
        self._needles = [f.encode("utf-8") for f in forms]
        # The longest prefix of a secret that can sit at the end of a chunk
        # without being a whole one: held in memory until the next chunk shows
        # whether it completes.
        self._holdback = max((len(n) for n in self._needles), default=1) - 1
        self._raw = self._chunks = None
        self._pending = b""
        self._digest, self._size, self._received, self._count = hashlib.sha256(), 0, 0, 0
        self._redacted, self._last_t = False, 0.0

    def mark_sent(self):
        """Write ``sent``; the transport calls this before the first request byte."""
        self._journal._append(
            {"kind": "sent", "attempt_id": self.attempt_id, "ts": _now()}, self._secrets
        )

    def _log(self, t_ms, nbytes, stored, **extra):
        line = {"t_ms": round(t_ms, 3), "nbytes": nbytes, "stored": stored, **extra}
        _write_all(self._chunks, json.dumps(line, separators=(",", ":")).encode("ascii") + b"\n")

    def _store(self, data):
        _write_all(self._raw, data)
        self._digest.update(data)
        self._size += len(data)

    def chunk(self, t_ms, data):
        """Redact, then append, a received body chunk; log it in the chunk log.

        The credential is replaced with ``[redacted]`` before anything reaches
        disk. A chunk may end partway through it, so the last
        ``len(credential) - 1`` bytes are held back and written with the next
        chunk, or by ``finish``/``abort``. Each chunk-log line is
        ``{"t_ms", "nbytes", "stored"}``: ``nbytes`` is what was received and
        ``stored`` what this call appended to the raw file, so ``stored``
        summed over the log equals the file size.
        """
        if self._raw is None:
            self._raw, self._chunks = _create(self.raw_path), _create(self.chunks_path)
            _fsync_dir(self._journal.directory)  # the evidence files exist after power loss
        window = self._pending + data
        for needle in self._needles:
            if needle in window:
                window = window.replace(needle, REDACTED.encode("ascii"))
                self._redacted = True
        cut = max(len(window) - self._holdback, 0)
        self._store(window[:cut])
        self._pending = window[cut:]
        self._received += len(data)
        self._count += 1
        self._last_t = t_ms
        self._log(t_ms, len(data), cut)

    def _hide_partial_token(self):
        """Replace a token prefix at the end of the held-back bytes.

        A stream cut off partway through the credential would otherwise leave
        most of it on disk. Only a prefix of at least ``_MIN_PREFIX`` bytes
        counts, so a chance one-byte match does not rewrite real output.
        """
        for needle in self._needles:
            for size in range(min(len(needle) - 1, len(self._pending)), _MIN_PREFIX - 1, -1):
                if self._pending.endswith(needle[:size]):
                    self._pending = self._pending[:-size] + REDACTED.encode("ascii")
                    self._redacted = True
                    return

    def _close_files(self):
        if self._raw is None or self._raw.closed:
            return
        self._hide_partial_token()
        try:
            if self._pending:
                self._store(self._pending)
                self._log(self._last_t, 0, len(self._pending), flush=True)
                self._pending = b""
            for handle in (self._raw, self._chunks):
                os.fsync(handle.fileno())
        finally:
            self._raw.close()
            self._chunks.close()

    def _raw_record(self):
        if self._raw is None:
            return None
        self._close_files()
        return {
            "file": self.raw_path.name,
            "bytes": self._size,
            "sha256": self._digest.hexdigest(),
            "received_bytes": self._received,
            "chunks_file": self.chunks_path.name,
            "chunks": self._count,
            "redacted": self._redacted,
        }

    def abort(self):
        """Flush the held-back bytes and close the raw files; no ``finished`` is written.

        Best effort: the error that caused the abort is the one to report, so
        a failure to flush here is not raised.
        """
        try:
            self._close_files()
        except OSError:
            pass

    def finish(
        self,
        *,
        outcome,
        billing,
        status=None,
        reason=None,
        detail=None,
        headers=(),
        body=b"",
        body_truncated=False,
        served_model=None,
        text="",
        terminal_event=None,
        error=None,
        error_phase=None,
        elapsed_ms=None,
    ):
        """Close the raw files and write ``finished``; returns the record."""
        record = {
            "kind": "finished",
            "attempt_id": self.attempt_id,
            "ts": _now(),
            "outcome": outcome,
            "status": status,
            "reason": reason,
            "detail": detail,
            "response_headers": [[k, v] for k, v in headers if k.lower() != "set-cookie"],
            "response_body": body.decode("utf-8", "replace") if body else None,
            "response_body_truncated": body_truncated,
            "billing": billing.as_dict(),
            "served_model": served_model,
            "terminal_event": terminal_event,
            "text": text,
            "raw": self._raw_record(),
            "error": error,
            "error_phase": error_phase,
            "elapsed_ms": elapsed_ms,
        }
        self._journal._append(record, self._secrets)
        return record
