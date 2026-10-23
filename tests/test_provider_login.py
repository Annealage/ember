"""Provider login: omp's own login flow, driven for the page.

A fake omp client stands in for ``omp_rpc.RpcClient``: it answers
``get_login_providers`` and runs ``login`` the way omp does, an ``open_url``
request, then an ``input`` request that blocks on the page's answer. What
matters here is the flow's states, that a wrong code ends the flow rather than
looping, that a cancel stops the omp it started, and that a pasted code never
comes back out of a route.
"""

import threading
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_PORT, TEST_HOST, create_toy_app, make_test_client

from annealage_ember import sessions
from annealage_ember.provider_login import LoginError, ProviderLogin
from annealage_ember.session.fake import FakeSession

TOKEN = "browser-token-for-login_123"
def JSON():
    return {"Content-Type": "application/json"}

SECRET = "super-secret-pasted-code-xyz"


class FakeOmp:
    """What ``RpcClient`` does for a login. ``accept`` decides which pasted
    values omp takes; others make omp ask again, as it does."""

    def __init__(self, accept=lambda value: value == "good", **_kwargs):
        self.accept = accept
        self.handler = None
        self.stopped = threading.Event()
        self.answers = []
        self.sent = threading.Condition()
        self.commands = []

    def on_ui_request(self, handler):
        self.handler = handler

    def start(self):
        return self

    def stop(self):
        self.stopped.set()
        with self.sent:
            self.sent.notify_all()

    def send_ui_value(self, request_id, value):
        with self.sent:
            self.answers.append(value)
            self.sent.notify_all()

    def cancel_ui_request(self, request_id):
        pass

    def _ui(self, **fields):
        base = {
            "id": "r%d" % len(self.commands), "method": "", "url": None, "launch_url": None,
            "instructions": None, "title": None, "requires_response": lambda: False,
        }
        base.update(fields)
        return SimpleNamespace(**base)

    def request_raw(self, command, **payload):
        self.commands.append((command, payload))
        if command == "get_login_providers":
            return {"providers": [
                {"id": "anthropic", "name": "Anthropic", "available": True, "authenticated": True},
                {"id": "zai", "name": "Z.AI", "available": True, "authenticated": False},
            ]}
        if command == "get_logout_accounts":
            return {"accounts": [{"id": "cred-1"}]}
        if command == "logout":
            return {}
        assert command == "login"
        self.handler(self._ui(
            method="open_url", url="https://claude.ai/oauth/authorize?x=1",
            launch_url="http://localhost:54545/launch", instructions="Sign in.",
        ))
        while True:
            self.handler(self._ui(
                method="input", title="Paste the code", requires_response=lambda: True,
            ))
            if self.stopped.is_set():
                raise RuntimeError("omp stopped")
            with self.sent:
                value = self.answers.pop(0) if self.answers else None
            if value is not None and self.accept(value):
                return {}
            if value is None:
                raise RuntimeError("omp stopped")


def _login(**kwargs):
    made = []

    def factory(**client_kwargs):
        client = FakeOmp(**kwargs)
        made.append((client, client_kwargs))
        return client

    return ProviderLogin(agent_dir="/profile", client_factory=factory, begin_seconds=5), made


def test_the_providers_come_from_omp_on_the_agents_profile():
    login, made = _login()
    rows = login.providers()
    assert [(r["id"], r["authenticated"]) for r in rows] == [("anthropic", True), ("zai", False)]
    client, kwargs = made[0]
    assert kwargs["env"] == {"PI_CODING_AGENT_DIR": "/profile"}
    assert client.stopped.is_set()  # a one-shot omp is stopped again


def test_a_login_gives_the_address_then_takes_the_code_and_finishes():
    login, made = _login()
    shown = login.begin("anthropic")
    assert shown["state"] == "awaiting_code"
    assert shown["url"] == "https://claude.ai/oauth/authorize?x=1"
    assert shown["launch_url"] == "http://localhost:54545/launch"
    assert shown["prompt"] == "Paste the code"

    done = login.submit("good")
    assert done["state"] == "done"
    assert login.state()["state"] == "done"
    assert made[0][0].stopped.wait(2)


def test_a_code_omp_does_not_take_is_asked_for_again_and_then_the_flow_ends():
    login, made = _login()
    login.begin("anthropic")
    again = login.submit("bad-1")
    assert (again["state"], again["attempts"]) == ("awaiting_code", 2)
    login.submit("bad-2")
    ended = login.submit("bad-3")
    assert ended["state"] == "failed"
    assert "did not accept" in ended["error"]
    assert made[0][0].stopped.wait(2)


def test_cancelling_stops_the_omp_it_started():
    login, made = _login()
    login.begin("anthropic")
    assert login.cancel() == {"state": "idle", "provider_id": None}
    assert made[0][0].stopped.wait(2)
    with pytest.raises(LoginError):
        login.submit("good")


def test_a_second_login_replaces_the_first():
    login, made = _login()
    login.begin("anthropic")
    login.begin("zai")
    assert made[0][0].stopped.wait(2)
    assert login.state()["provider_id"] == "zai"
    login.cancel()


def test_an_omp_that_cannot_start_is_a_reason_not_a_hang():
    def factory(**_kwargs):
        raise RuntimeError("No models available")

    login = ProviderLogin(client_factory=factory, begin_seconds=5)
    with pytest.raises(LoginError, match="No models available"):
        login.providers()
    assert login.begin("anthropic")["state"] == "failed"


def _app(served_dir, login):
    sid = sessions.create_session(served_dir)
    return create_toy_app(
        served_dir, token=TOKEN, agent_token="agent-token-for-login_456",
        host=TEST_HOST, port=DEFAULT_PORT, session_id=sid,
        build_session=lambda on_event, *, bus: FakeSession(on_event, session_id=sid),
        provider_login=login,
    )


@pytest.mark.asyncio
async def test_the_routes_run_a_login_and_never_echo_the_code(served_dir, capsys):
    login, _made = _login()
    client = make_test_client(_app(served_dir, login))
    q = "?t=" + TOKEN

    res = await client.get("/agent/login/providers" + q)
    assert [p["id"] for p in res.json["providers"]] == ["anthropic", "zai"]

    res = await client.post("/agent/login/begin" + q, body='{"provider_id": "anthropic"}', headers=JSON())
    assert res.json["state"] == "awaiting_code"

    res = await client.post("/agent/login/submit" + q, body='{"code": "%s"}' % SECRET, headers=JSON())
    assert res.json["state"] == "awaiting_code" and res.json["attempts"] == 2
    res = await client.post("/agent/login/submit" + q, body='{"code": "good"}', headers=JSON())
    assert res.json["state"] == "done"

    seen = capsys.readouterr()
    assert SECRET not in seen.out + seen.err
    assert SECRET not in (await client.get("/agent/login/state" + q)).text


@pytest.mark.asyncio
async def test_the_routes_refuse_the_agent_token_and_a_foreign_page(served_dir):
    client = make_test_client(_app(served_dir, _login()[0]))
    for path in ("/agent/login/providers", "/agent/login/state"):
        for query in ("", "?t=agent-token-for-login_456", "?t=wrong"):
            assert (await client.get(path + query)).status_code == 403
        res = await client.get(path + "?t=" + TOKEN, headers={"Origin": "http://evil.example"})
        assert res.status_code == 403
    res = await client.post("/agent/login/begin?t=agent-token-for-login_456",
                            body='{"provider_id": "anthropic"}', headers=JSON())
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_an_app_without_a_provider_login_has_no_such_routes(served_dir):
    client = make_test_client(_app(served_dir, None))
    assert (await client.get("/agent/login/providers?t=" + TOKEN)).status_code == 404


@pytest.mark.asyncio
async def test_logout_names_each_stored_credential(served_dir):
    login, made = _login()
    client = make_test_client(_app(served_dir, login))
    q = "?t=" + TOKEN
    res = await client.get("/agent/login/accounts" + q + "&provider_id=anthropic")
    assert res.json["accounts"] == [{"id": "cred-1"}]
    res = await client.post("/agent/login/logout" + q,
                            body='{"provider_id": "anthropic", "credential_id": "cred-1"}', headers=JSON())
    assert res.json == {"ok": True}
    assert ("logout", {"providerId": "anthropic", "credentialId": "cred-1"}) in [
        c for client_, _ in made for c in client_.commands
    ]
