"""The page's provider login, over ``provider_login.ProviderLogin``.

Registered only for an app given a ``provider_login`` and never in hosted
mode (a service's provider credentials are the operator's, not a browser's):

    GET  /agent/login/providers   [{id, name, available, authenticated}]
    GET  /agent/login/state       the flow under way: state, url, prompt, error
    POST /agent/login/begin       {"provider_id"}: start, answer with the state
    POST /agent/login/submit      {"code"}: the pasted code or redirect URL
    POST /agent/login/cancel
    GET  /agent/login/accounts?provider_id=...
    POST /agent/login/logout      {"provider_id", "credential_id"?}

Every route is behind the app's ``BrowserAuth`` and refuses like ``/ws``. A
pasted ``code`` is read from the body, handed on, and appears in no response,
log line or error.
"""

import asyncio

from ..provider_login import LoginError
from . import read_json_body
from .ws import refusal


def register_provider_login_routes(app, *, login, auth):
    async def _run(func, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, func, *args), None
        except LoginError as exc:
            return None, ({"ok": False, "error": str(exc)}, 502)

    async def _body(req):
        data, error = await read_json_body(req)
        if error is not None:
            return None, error
        if not isinstance(data, dict):
            return None, ({"ok": False, "error": "body must be a JSON object"}, 400)
        return data, None

    def _name(data, key):
        value = data.get(key)
        return value if isinstance(value, str) and value and len(value) <= 200 else None

    @app.get("/agent/login/providers")
    async def providers(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        rows, failure = await _run(login.providers)
        return failure or ({"ok": True, "providers": rows}, 200)

    @app.get("/agent/login/state")
    async def state(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        return {"ok": True, **login.state()}, 200

    @app.post("/agent/login/begin")
    async def begin(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        data, error = await _body(req)
        if error is not None:
            return error
        provider_id = _name(data, "provider_id")
        if provider_id is None:
            return {"ok": False, "error": "provider_id is required"}, 400
        result, failure = await _run(login.begin, provider_id)
        return failure or ({"ok": True, **result}, 200)

    @app.post("/agent/login/submit")
    async def submit(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        data, error = await _body(req)
        if error is not None:
            return error
        code = data.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 8192:
            return {"ok": False, "error": "code is required"}, 400
        result, failure = await _run(login.submit, code)
        return failure or ({"ok": True, **result}, 200)

    @app.post("/agent/login/cancel")
    async def cancel(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        result, failure = await _run(login.cancel)
        return failure or ({"ok": True, **result}, 200)

    @app.get("/agent/login/accounts")
    async def accounts(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        provider_id = req.args.get("provider_id")
        if not provider_id:
            return {"ok": False, "error": "provider_id is required"}, 400
        rows, failure = await _run(login.accounts, provider_id)
        return failure or ({"ok": True, "accounts": rows}, 200)

    @app.post("/agent/login/logout")
    async def logout(req):
        if auth.authenticate(req, None) is None:
            return refusal()
        data, error = await _body(req)
        if error is not None:
            return error
        provider_id = _name(data, "provider_id")
        if provider_id is None:
            return {"ok": False, "error": "provider_id is required"}, 400
        _, failure = await _run(login.logout, provider_id, _name(data, "credential_id"))
        return failure or ({"ok": True}, 200)
