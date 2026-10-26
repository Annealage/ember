"""Logging omp in to a model provider, from the page.

omp's RPC mode already knows how: ``get_login_providers`` lists the providers
and whether each has a credential, ``login {providerId}`` runs omp's own OAuth
flow, ``get_logout_accounts`` / ``logout`` list and remove stored credentials.
A login shows up on the wire as ``extension_ui_request`` frames: ``open_url``
(the page to sign in on), then an ``input`` asking for the code or the redirect
URL that page ends on. This module drives that and nothing else; omp keeps the
credential, in the profile it was started with.

It does not use the agent's own omp child. That child will not start at all
when the profile has no usable model, which is exactly when a login is wanted,
and one that does start is not something to keep waiting on a person for up to
ten minutes. Every operation here starts a short-lived omp of its own on the
same profile (``agent_dir`` / ``config_dir`` / ``binary``, the same arguments a
product gives ``launch.build_session``), told a model by name so it starts
without one that works, and stops it again when the operation ends.

Limits that are omp's, not ours: RPC login refuses a provider that prompts
before it has shown an address, and any prompt flagged secret, which is how a
provider that just wants an API key asks. Those come back as omp's own message
(it says to use the terminal UI) and nothing is written for them here.

Nothing a person pastes is logged, stored or echoed back: the value goes from
``submit`` to omp's stdin and no further, and an error message never carries it.
"""

import threading
from typing import Callable, Optional

#: The model a login helper is started with. It only has to let omp start with
#: an empty profile; nothing is ever asked of it.
BOOT_MODEL = "anthropic/claude-sonnet-4-5"

#: omp gives a login this long; the helper is given a little more.
LOGIN_SECONDS = 600.0

#: How long ``begin`` waits for the sign-in address (or a refusal).
BEGIN_SECONDS = 30.0

#: A code omp keeps asking again for is a code it does not accept. The flow
#: ends after this many tries rather than looping for the whole ten minutes.
MAX_ATTEMPTS = 3

IDLE, STARTING, AWAITING, DONE, FAILED = "idle", "starting", "awaiting_code", "done", "failed"


class LoginError(Exception):
    """A login operation the page should show as a message."""


def _default_client(**kwargs):
    from omp_rpc import RpcClient

    return RpcClient(**kwargs)


class ProviderLogin:
    """The provider login of one omp profile. Thread-safe; blocking calls
    (``providers``, ``begin`` ...) belong in an executor."""

    def __init__(
        self,
        *,
        agent_dir: Optional[str] = None,
        config_dir: Optional[str] = None,
        binary: Optional[str] = None,
        cwd: Optional[str] = None,
        client_factory: Optional[Callable] = None,
        login_seconds: float = LOGIN_SECONDS,
        begin_seconds: float = BEGIN_SECONDS,
    ):
        self._env = {}
        if agent_dir:
            self._env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        if config_dir:
            self._env["PI_CONFIG_DIR"] = str(config_dir)
        self._binary = binary or "omp"
        self._cwd = cwd
        self._factory = client_factory or _default_client
        self._login_seconds = login_seconds
        self._begin_seconds = begin_seconds
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._flow = None
        self._state = self._idle_state()

    # -- one-shot operations --------------------------------------------------

    def _client(self, *, request_timeout: float, on_ui_request=None):
        client = self._factory(
            executable=self._binary,
            model=BOOT_MODEL,
            cwd=self._cwd,
            env=dict(self._env),
            no_session=True,
            tools=[],
            request_timeout=request_timeout,
            extra_args=("--no-extensions",),
        )
        if on_ui_request is not None:
            client.on_ui_request(on_ui_request)
        client.start()
        return client

    def _once(self, command: str, **payload):
        try:
            client = self._client(request_timeout=30.0)
        except Exception as exc:
            raise LoginError(
                "omp could not be started to %s: %s" % (command, _brief(exc))
            ) from None
        try:
            return client.request_raw(command, **payload)
        except Exception as exc:
            raise LoginError("omp refused %s: %s" % (command, _brief(exc))) from None
        finally:
            _stop(client)

    def providers(self) -> list:
        """``[{id, name, available, authenticated}]``."""
        reply = self._once("get_login_providers")
        return [
            {
                "id": str(row.get("id")),
                "name": str(row.get("name") or row.get("id")),
                "available": bool(row.get("available", True)),
                "authenticated": bool(row.get("authenticated")),
            }
            for row in (reply.get("providers") or [])
            if isinstance(row, dict) and row.get("id")
        ]

    def accounts(self, provider_id: str) -> list:
        """The stored credentials for a provider, as omp reports them."""
        reply = self._once("get_logout_accounts", providerId=provider_id)
        return list(reply.get("accounts") or [])

    def logout(self, provider_id: str, credential_id: Optional[str] = None) -> None:
        payload = {"providerId": provider_id}
        if credential_id is not None:
            payload["credentialId"] = credential_id
        self._once("logout", **payload)

    # -- the login flow -------------------------------------------------------

    @staticmethod
    def _idle_state() -> dict:
        return {"state": IDLE, "provider_id": None}

    def state(self) -> dict:
        """What the page shows now. Never carries anything pasted."""
        with self._lock:
            return dict(self._state)

    def begin(self, provider_id: str) -> dict:
        """Start a login and return once it has an address to show, or has
        ended. A flow already under way is ended first: one at a time."""
        self.cancel()
        flow = _Flow(self, provider_id)
        with self._lock:
            self._flow = flow
            self._state = {"state": STARTING, "provider_id": provider_id}
        flow.launch()
        with self._changed:
            self._changed.wait_for(
                lambda: self._flow is not flow or self._state["state"] != STARTING,
                timeout=self._begin_seconds,
            )
            if self._flow is flow and self._state["state"] == STARTING:
                flow.finish(FAILED, "omp did not give a sign-in address in time")
            return dict(self._state)

    def submit(self, text: str) -> dict:
        """Hand omp the pasted code or redirect URL."""
        with self._lock:
            flow = self._flow
            awaiting = self._state["state"] == AWAITING
        if flow is None or not awaiting:
            raise LoginError("there is no login waiting for a code")
        value = str(text or "").strip()
        if not value:
            raise LoginError("paste the code or the address the sign-in ended on")
        flow.answer(value)
        with self._changed:
            # The answer is taken, then omp either finishes, asks again, or fails.
            self._changed.wait_for(
                lambda: (
                    self._flow is not flow or self._state["state"] != AWAITING or flow.asked_again()
                ),
                timeout=self._begin_seconds,
            )
            return dict(self._state)

    def cancel(self) -> dict:
        """End any login now (stopping the omp it runs in)."""
        with self._lock:
            flow = self._flow
        if flow is not None:
            flow.finish(IDLE, None)
        return self.state()

    def close(self) -> None:
        self.cancel()

    # called by the flow, under no lock
    def _set(self, flow, state: dict) -> None:
        with self._changed:
            if self._flow is not flow:
                return
            self._state = state
            self._changed.notify_all()


class _Flow:
    """One login: its omp child, its worker thread, the waiting prompt."""

    def __init__(self, owner: ProviderLogin, provider_id: str):
        self.owner = owner
        self.provider_id = provider_id
        self.client = None
        self.attempts = 0
        self._answer = None
        self._prompt_id = None
        self._wake = threading.Condition()
        self._over = False
        self._seen_attempts = 0
        self.url = None
        self.launch_url = None
        self.instructions = None

    def launch(self):
        threading.Thread(target=self._run, name="provider-login", daemon=True).start()

    def asked_again(self) -> bool:
        return self.attempts > self._seen_attempts

    def _run(self):
        try:
            self.client = self.owner._client(
                request_timeout=self.owner._login_seconds + 30.0,
                on_ui_request=self._on_ui,
            )
        except Exception as exc:
            self.finish(FAILED, "omp could not be started: " + _brief(exc))
            return
        with self._wake:
            over = self._over
        if over:
            _stop(self.client)
            return
        try:
            self.client.request_raw("login", providerId=self.provider_id)
        except Exception as exc:
            if not self._over:
                self.finish(FAILED, _brief(exc))
            return
        self.finish(DONE, None)

    def _shown(self) -> dict:
        # ``url`` is the provider's own sign-in page, which opens from any
        # machine. ``launch_url`` is omp's local launcher for it, which only
        # works in a browser on the machine omp runs on.
        return {
            "url": self.url,
            "launch_url": self.launch_url,
            "instructions": self.instructions,
            "attempts": self.attempts,
        }

    def _publish(self, state: str, **fields):
        self.owner._set(self, {"state": state, "provider_id": self.provider_id, **fields})

    def _on_ui(self, request):
        """omp's reader thread. Blocks while a person is pasting a code."""
        method = request.method
        if method == "open_url":
            self.url = request.url or request.launch_url
            self.launch_url = request.launch_url
            self.instructions = request.instructions
            self._publish(STARTING, **self._shown())
            return
        if method == "input":
            self.attempts += 1
            if self.attempts > MAX_ATTEMPTS:
                self.finish(FAILED, "omp did not accept that code")
                return
            self._prompt_id = request.id
            self._publish(AWAITING, prompt=request.title, **self._shown())
            with self._wake:
                self._wake.wait_for(lambda: self._answer is not None or self._over)
                value, self._answer = self._answer, None
                over = self._over
            if over or value is None:
                return
            try:
                self.client.send_ui_value(request.id, value)
            except Exception as exc:
                self.finish(FAILED, "the code could not be sent to omp: " + _brief(exc))
            return
        if request.requires_response():
            try:
                self.client.cancel_ui_request(request.id)
            except Exception:
                pass

    def answer(self, value: str):
        with self._wake:
            self._seen_attempts = self.attempts
            self._answer = value
            self._wake.notify_all()

    def finish(self, state: str, error: Optional[str]):
        with self._wake:
            if self._over:
                return
            self._over = True
            self._wake.notify_all()
        fields = {"error": error} if error else {}
        if state == IDLE:
            self.owner._set(self, {"state": IDLE, "provider_id": None})
        else:
            self._publish(state, **fields)
        client = self.client
        if client is not None:
            threading.Thread(target=_stop, args=(client,), daemon=True).start()


def _stop(client) -> None:
    try:
        client.stop()
    except Exception:
        pass


def _brief(exc: BaseException) -> str:
    """A one-line reason, from the exception's message only (omp's stderr tail
    is part of it, so it is cut short)."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]
