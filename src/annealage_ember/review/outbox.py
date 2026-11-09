"""Durable, explicitly requested ordinary human turns.

The owner starts ``run`` with the app holder's background tasks. One runtime
owns a ledger; its file lock also protects readers and atomic updates. A fence
is persisted before a turn can reach the session: a crash after that point is
uncertain, never an invitation to replay it.
"""

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

from .. import files
from .model import ReviewError, file_lock
from .notifications import NotificationError, NotificationStore, _human, _Reply, _sender

_MAX_ERROR_CHARS = 512
_MAX_TURN = 10**20 - 1
# Receipts cannot be discarded without making an old operation replayable.
# Bound their storage and refuse new intent before its receipt can exceed it.
_MAX_RECEIPTS = 16_384
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024

_STATUS_CODES = {"queued": "q", "dispatching": "d", "sent": "s", "uncertain": "u"}
_CODE_STATUSES = {code: status for status, code in _STATUS_CODES.items()}


def _payload(scope, records):
    return json.dumps(
        {"version": 1, "scope": scope, "records": records},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _compact_payload(scope, records):
    # The legacy ledger repeats every ID and spells out every status. Preserve
    # all other fields while eliminating that duplication when a legacy file
    # needs just enough room to persist a dispatch fence.
    compact = {
        key: [
            _STATUS_CODES[row["status"]],
            {field: value for field, value in row.items() if field not in {"id", "status"}},
        ]
        for key, row in records.items()
    }
    return json.dumps(
        {"v": 2, "s": scope, "r": compact}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _disk_payload(scope, records):
    payload = _payload(scope, records)
    if len(payload) > files._MAX_FIXED_FILE_BYTES:
        return _compact_payload(scope, records)
    return payload


def _fit_payload(scope, records):
    payload = _disk_payload(scope, records)
    if len(payload) <= files._MAX_FIXED_FILE_BYTES:
        return payload
    # Legacy records did not reserve transition space. Keep their drafts and
    # uncertain fences, but reclaim optional diagnostic details if needed.
    excess = len(payload) - files._MAX_FIXED_FILE_BYTES
    for field in ("turn", "error"):
        for row in records.values():
            if row["status"] == "uncertain" and field in row:
                value = row.pop(field)
                # Compact rows retain sender and blocks, so removing this
                # property also removes exactly one comma.
                excess -= (
                    len(
                        json.dumps(
                            {field: value}, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    - 1
                )
                if excess <= 0:
                    return _disk_payload(scope, records)
    return _disk_payload(scope, records)


def _next_fence(records):
    projected = {key: dict(row) for key, row in records.items()}
    for row in projected.values():
        if row["status"] == "queued":
            row.update(status="dispatching", turn=_MAX_TURN)
            break
    return projected


def _project(records, *, errors):
    projected = {}
    for key, row in records.items():
        if row["status"] in {"queued", "dispatching"}:
            row = {**row, "status": "dispatching", "turn": _MAX_TURN}
            if errors:
                row["error"] = "\0" * _MAX_ERROR_CHARS
        projected[key] = row
    return projected


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fingerprint(sender, blocks):
    actor = {key: value for key, value in sender.items() if key != "claims"}
    payload = json.dumps(
        [actor, blocks],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _TurnStore(NotificationStore):
    def _load(self):
        if files.safe_fixed_file(self.path.parent, self.path.name) is None:
            raise NotificationError(
                "The turn outbox is not a plain file; repair it before retrying."
            )
        raw = files.read_fixed_file(self.path.parent, self.path.name)
        if raw is None:
            if self.path.exists():
                raise NotificationError(
                    "The turn outbox cannot be read; repair it before retrying."
                )
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise NotificationError(
                "The turn outbox is unreadable; repair it before retrying."
            ) from None
        if isinstance(data, dict) and data.get("v") == 2 and data.get("s") == self.scope:
            compact = data.get("r")
            if not isinstance(compact, dict):
                raise NotificationError("The turn outbox has invalid compact records.")
            records = {}
            for key, value in compact.items():
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or not isinstance(value[0], str)
                    or value[0] not in _CODE_STATUSES
                    or not isinstance(value[1], dict)
                    or "id" in value[1]
                    or "status" in value[1]
                ):
                    raise NotificationError("The turn outbox has invalid compact records.")
                records[key] = {
                    **value[1],
                    "id": key,
                    "status": _CODE_STATUSES[value[0]],
                }
        elif (
            isinstance(data, dict)
            and data.get("version") == 1
            and data.get("scope") == self.scope
            and isinstance(data.get("records"), dict)
        ):
            records = data["records"]
        else:
            raise NotificationError("The turn outbox has an invalid scope, version or records.")
        for key, row in records.items():
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(row, dict)
                or row.get("id") != key
                or not isinstance(row.get("status"), str)
                or row["status"] not in {"queued", "dispatching", "sent", "uncertain"}
                or (
                    "fingerprint" in row
                    and (
                        row["status"] != "sent"
                        or not isinstance(row["fingerprint"], str)
                        or len(row["fingerprint"]) != 64
                        or any(c not in "0123456789abcdef" for c in row["fingerprint"])
                    )
                )
                or (
                    "fingerprint" not in row
                    and (
                        not isinstance(row.get("sender"), dict)
                        or not isinstance(row.get("blocks"), list)
                    )
                )
            ):
                raise NotificationError("The turn outbox contains an invalid record.")
            if "fingerprint" not in row:
                try:
                    _human(row["sender"])
                except (KeyError, TypeError):
                    raise NotificationError(
                        "The turn outbox contains invalid attribution."
                    ) from None
        return records

    def _receipt_prefix(self):
        ledger = hashlib.sha256(os.fsencode(self.path.name)).hexdigest()[:32]
        return ".turn-receipt-" + ledger + "-"

    def _receipt_path(self, operation_id):
        identity = json.dumps(operation_id, ensure_ascii=True).encode("ascii")
        return self.path.parent / (self._receipt_prefix() + hashlib.sha256(identity).hexdigest())

    def _receipt_inventory(self):
        names = set()
        size = 0
        prefix = self._receipt_prefix()
        try:
            entries = os.scandir(self.path.parent)
        except FileNotFoundError:
            return names, size
        with entries:
            for entry in entries:
                if entry.name.startswith(prefix):
                    names.add(entry.name)
                    size += entry.stat(follow_symlinks=False).st_size
        return names, size

    def _receipt_size(self, operation_id):
        receipt = {
            "version": 1,
            "scope": self.scope,
            "id": operation_id,
            "status": "sent",
            "fingerprint": "0" * 64,
        }
        return len(json.dumps(receipt, ensure_ascii=False).encode("utf-8"))

    def _reserve_receipts(self, records, sent):
        # Count existing receipts and every operation that can still need one.
        # A crash during migration can leave both copies, counted only once.
        names, size = self._receipt_inventory()
        count = len(names)
        for key in set(sent) | {
            key
            for key, row in records.items()
            if row["status"] in {"queued", "dispatching", "sent"}
        }:
            name = self._receipt_path(key).name
            if name in names:
                continue
            count += 1
            size += self._receipt_size(key)
        if count > _MAX_RECEIPTS or size > _MAX_RECEIPT_BYTES:
            raise NotificationError(
                "Turn receipt capacity reached; preserve the outbox and receipts, "
                "and increase the receipt limits and available storage before retrying."
            )

    def _receipt(self, operation_id):
        path = self._receipt_path(operation_id)
        if files.safe_fixed_file(path.parent, path.name) is None:
            raise NotificationError(
                "The turn receipt is not a plain file; repair it before retrying."
            )
        raw = files.read_fixed_file(path.parent, path.name)
        if raw is None:
            if os.path.lexists(path):
                raise NotificationError(
                    "The turn receipt cannot be read; repair it before retrying."
                )
            return None
        try:
            data = json.loads(raw)
            fingerprint = data["fingerprint"]
            if (
                data["version"] != 1
                or data["scope"] != self.scope
                or data["id"] != operation_id
                or not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in fingerprint)
            ):
                raise ValueError
        except (ValueError, UnicodeDecodeError, TypeError, KeyError):
            raise NotificationError(
                "The turn receipt is invalid; repair it before retrying."
            ) from None
        return {"id": operation_id, "status": "sent", "fingerprint": fingerprint}

    @staticmethod
    def _check_receipt(receipt, existing):
        if existing is None:
            return
        fingerprint = existing.get("fingerprint")
        if fingerprint is None:
            fingerprint = _fingerprint(existing["sender"], existing["blocks"])
        if existing["status"] == "queued" or fingerprint != receipt["fingerprint"]:
            raise NotificationError("The turn outbox conflicts with its receipt.")

    def get(self, operation_id):
        with file_lock(self.path):
            records = self._load()
            receipt = self._receipt(operation_id)
            if receipt is not None:
                self._check_receipt(receipt, records.get(operation_id))
                return receipt
            return records.get(operation_id)

    def change(self, update, *, operation_id=None, reserve=False, transition_id=None):
        with file_lock(self.path):
            records = self._load()
            if operation_id is not None:
                receipt = self._receipt(operation_id)
                if receipt is not None:
                    self._check_receipt(receipt, records.get(operation_id))
                    records[operation_id] = receipt
            existing_queued = (
                operation_id in records and records[operation_id]["status"] == "queued"
            )
            had_full_reservation = (
                reserve
                and existing_queued
                and len(_payload(self.scope, _project(records, errors=True)))
                <= files._MAX_FIXED_FILE_BYTES
            )
            result = update(records)
            # Each receipt is durable before its ledger entry is removed. If a
            # crash interrupts the cutover, either copy prevents another send.
            sent = {}
            completed = [(key, row) for key, row in records.items() if row["status"] == "sent"]
            names, size = self._receipt_inventory() if completed else (set(), 0)
            for key, row in completed:
                receipt = {
                    "id": key,
                    "status": "sent",
                    "fingerprint": row.get("fingerprint")
                    or _fingerprint(row["sender"], row["blocks"]),
                }
                name = self._receipt_path(key).name
                receipt_size = self._receipt_size(key)
                if name in names or (
                    len(names) < _MAX_RECEIPTS and size + receipt_size <= _MAX_RECEIPT_BYTES
                ):
                    sent[key] = receipt
                    del records[key]
                    if name not in names:
                        names.add(name)
                        size += receipt_size
                else:
                    # A pre-quota ledger can already contain more completions
                    # than the limit. Keep their identities in the ledger.
                    records[key] = receipt
            payload = _fit_payload(self.scope, records)
            if reserve and result.get("status") == "queued" and existing_queued:
                # Newer intents retain their full reservation. A legacy retry
                # needs only the next FIFO fence, not all fences at once.
                if had_full_reservation:
                    serialized = _payload(self.scope, _project(records, errors=True))
                else:
                    serialized = _fit_payload(self.scope, _next_fence(records))
                if len(serialized) > files._MAX_FIXED_FILE_BYTES:
                    raise NotificationError("The turn outbox is full; no changes were saved.")
            if reserve and result.get("status") == "queued" and not existing_queued:
                self._reserve_receipts(records, sent)
                # Every pending row may independently acquire a turn number
                # and an error before it leaves the ledger. NUL has the largest
                # JSON encoding of any single Unicode character (six bytes).
                if (
                    len(_payload(self.scope, _project(records, errors=True)))
                    > files._MAX_FIXED_FILE_BYTES
                ):
                    raise NotificationError("The turn outbox is full; no changes were saved.")
            if len(payload) > files._MAX_FIXED_FILE_BYTES and transition_id is not None:
                # Ledgers written before reservations may have no room for the
                # full error. Preserve their payload and the stopped/uncertain
                # state rather than letting the FIFO get stuck on the error text.
                row = records.get(transition_id)
                if row is not None and row.get("error"):
                    if row["status"] == "uncertain":
                        row.pop("error")
                    elif row["status"] == "queued":
                        row["error"] = "Turn refused; retry this operation."
                    payload = _disk_payload(self.scope, records)
                    if len(payload) > files._MAX_FIXED_FILE_BYTES and row["status"] == "queued":
                        row["error"] = "!"
                        payload = _disk_payload(self.scope, records)
            if len(payload) > files._MAX_FIXED_FILE_BYTES:
                raise NotificationError("The turn outbox is full; no changes were saved.")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for key, row in sent.items():
                old = self._receipt(key)
                if old is not None and old != row:
                    raise NotificationError("The turn outbox conflicts with its receipt.")
                if old is None:
                    receipt = {"version": 1, "scope": self.scope, **row}
                    files.atomic_replace(
                        self._receipt_path(key),
                        json.dumps(receipt, ensure_ascii=False).encode("utf-8"),
                        default_mode=0o600,
                    )
            if sent:
                _sync_directory(self.path.parent)
            files.atomic_replace(self.path, payload, default_mode=0o600)
            _sync_directory(self.path.parent)
            return result


class OrdinaryTurnOutbox:
    """FIFO outbox of idempotent operations delivered through ordinary admission.

    ``enqueue`` is synchronous so a product can hold the bus submission lock
    across its own busy check and durable enqueue. Reusing an ID with different
    blocks or a different actor is an error; a queued retry may refresh claims.
    The caller owns the durable operation ID and must retain it across retries.
    """

    def __init__(self, app, *, path, scope, auth, recorder, required_ops=("project.exec",)):
        self.app = app
        self.ledger = _TurnStore(path, scope)
        self.auth = auth
        self.recorder = recorder
        self.required_ops = tuple(required_ops)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self.last_error = None
        app.agent_status_listeners.append(self._wake.set)

        # A dispatching record left by a previous runtime may have reached the
        # provider. Persist the recovery decision before accepting new work.
        def recover(rows):
            for row in rows.values():
                if row["status"] == "dispatching":
                    row["status"] = (
                        "sent" if self.ledger._receipt(row["id"]) is not None else "uncertain"
                    )

        if any(row["status"] == "dispatching" for row in self.ledger.read().values()):
            self.ledger.change(recover)

    @staticmethod
    def _uncertain_message():
        return "Delivery is uncertain; check the conversation before sending a separate message. This operation will not be replayed."

    @staticmethod
    def _public(row):
        status = "uncertain" if row["status"] == "dispatching" else row["status"]
        error = row.get("error")
        if status == "uncertain" and not error:
            error = OrdinaryTurnOutbox._uncertain_message()
        return {"id": row["id"], "status": status, **({"error": error} if error else {})}

    def status(self, operation_id):
        row = self.ledger.get(operation_id)
        return self._public(row) if row is not None else None

    def enqueue(self, operation_id, human, blocks):
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("An operation ID is required.")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("A nonempty list of turn blocks is required.")
        # Validate and snapshot now, not after the caller mutates its input.
        blocks = json.loads(json.dumps(blocks, ensure_ascii=False, allow_nan=False))
        sender = _sender(human)

        def add(rows):
            row = rows.get(operation_id)
            if row is not None:
                matches = (
                    row["fingerprint"] == _fingerprint(sender, blocks)
                    if "fingerprint" in row
                    else (
                        {k: v for k, v in row["sender"].items() if k != "claims"}
                        == {k: v for k, v in sender.items() if k != "claims"}
                        and row["blocks"] == blocks
                    )
                )
                if not matches:
                    raise NotificationError(
                        "The operation ID already belongs to a different turn or actor."
                    )
                if row["status"] == "queued":
                    row["sender"] = sender
                    row.pop("error", None)
            else:
                row = {"id": operation_id, "sender": sender, "blocks": blocks, "status": "queued"}
                rows[operation_id] = row
            return self._public(row)

        result = self.ledger.change(add, operation_id=operation_id, reserve=True)
        self._wake.set()
        return result

    def _update(self, operation_id, **changes):
        if "error" in changes:
            error = changes["error"]
            changes["error"] = (
                error[:_MAX_ERROR_CHARS].encode("utf-8", "replace").decode("utf-8")
                if isinstance(error, str)
                else "The agent did not accept this turn."
            )
        if "turn" in changes and (
            type(changes["turn"]) is not int or not 0 <= changes["turn"] <= _MAX_TURN
        ):
            raise NotificationError("The turn number is out of range; no changes were saved.")

        def update(rows):
            rows[operation_id].update(changes)

        self.ledger.change(update, transition_id=operation_id)

    async def deliver(self):
        from ..http.ws import _submit_turn_locked, turn_refusal

        async with self._lock:
            app = self.app
            holder = app.agent_holder
            bus = app.agent_bus
            if getattr(holder, "_stopped", False) or bus.paused:
                return
            rows = self.ledger.read()
            row = next((r for r in rows.values() if r["status"] == "queued"), None)
            if row is None or row.get("error"):
                return
            # Like an explicit browser turn, queued work can resume an idle-closed
            # holder, but it must not steer a live turn or bypass a permission.
            if holder.session is None and getattr(holder, "closed", False):
                await holder.ensure(retry=True)
            async with bus.turn_submission_lock:
                session = holder.session
                if (
                    getattr(holder, "_stopped", False)
                    or bus.paused
                    or session is None
                    or holder.status().get("turn_running")
                    or turn_refusal(session, bus)
                ):
                    return
                broker = bus.broker
                if broker is not None and broker.pending_requests():
                    return
                # Enqueue may refresh credentials while admission waits.
                rows = self.ledger.read()
                row = next((r for r in rows.values() if r["status"] == "queued"), None)
                if row is None or row.get("error"):
                    return
                human = _human(row["sender"])
                if not all(self.auth.permits(human, op) for op in self.required_ops):
                    self._update(
                        row["id"], error="Turn authorisation expired; retry while signed in."
                    )
                    return
                reply = _Reply()
                admitted = False
                dispatch_turn = bus.turn + 1
                delivery_error = None

                def observe(event):
                    nonlocal delivery_error
                    if admitted and event.get("kind") == "agent_error":
                        delivery_error = self._uncertain_message()
                    if event.get("turn") == dispatch_turn and (
                        event.get("kind") == "input_state"
                        and event.get("state") in {"rejected", "uncertain"}
                        or event.get("kind") == "turn_end"
                        and event.get("stop_reason")
                        in {"rejected", "not_sent", "error", "backend_exited"}
                    ):
                        delivery_error = self._uncertain_message()

                def admit():
                    nonlocal admitted
                    if (
                        getattr(holder, "_stopped", False)
                        or bus.paused
                        or not all(self.auth.permits(human, op) for op in self.required_ops)
                    ):
                        raise NotificationError(
                            "Turn admission changed; retry while signed in and unpaused."
                        )
                    self._update(row["id"], status="dispatching", turn=dispatch_turn)
                    admitted = True

                app.agent_event_log.observers.append(observe)
                try:
                    await _submit_turn_locked(
                        reply,
                        SimpleNamespace(human=human, tab_id=None),
                        app.agent_registry,
                        app.agent_event_log,
                        {"blocks": row["blocks"]},
                        session,
                        bus,
                        self.recorder,
                        on_admitted=admit,
                    )
                    if admitted:
                        error = reply.error or delivery_error
                        self._update(
                            row["id"],
                            status="uncertain" if error else "sent",
                            **({"error": error} if error else {}),
                        )
                    elif reply.error:
                        self._update(row["id"], error=reply.error)
                except BaseException:
                    if admitted:
                        self._update(row["id"], status="uncertain", error=self._uncertain_message())
                    raise
                finally:
                    app.agent_event_log.observers.remove(observe)

    async def run(self):
        while True:
            self._wake.clear()
            try:
                await self.deliver()
                self.last_error = None
            except (ReviewError, OSError, ValueError) as exc:
                self.last_error = str(exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
