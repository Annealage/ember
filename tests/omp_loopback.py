"""A scripted stand-in for the model behind a real ``omp`` process.

It speaks the ``openai-codex-responses`` wire over plain HTTP on loopback, the
transport omp's own provider uses with ``PI_CODEX_WEBSOCKET=0``. Each request
gets the next scripted step: either one tool call (``("call", name, args)``) or
a closing text (``("text", "...")``). Every request body is kept, so a test can
read what omp sent the model: the tool schemas it advertised, and the result of
the previous call. No credentials are involved: the token is an unsigned JWT.
"""

import base64
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROVIDER = "loop"
MODEL = "loop/stub"
TOKEN_ENV = "ANNEALAGE_LOOPBACK_JWT"


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


JWT = ".".join(
    [
        _b64({"alg": "none"}),
        _b64(
            {
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-loopback"},
                "exp": 4102444800,
            }
        ),
        "sig",
    ]
)


def _sse(kind, data):
    return ("event: %s\ndata: %s\n\n" % (kind, json.dumps(dict(data, type=kind)))).encode()


def _response(output, status="completed"):
    return {
        "id": "resp_1",
        "object": "response",
        "status": status,
        "model": "stub",
        "output": output,
        "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    }


class LoopbackModel:
    """Serves the scripted steps; ``requests`` holds each decoded request body."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                body = json.loads(raw or b"{}")
                with outer._lock:
                    index = len(outer.requests)
                    outer.requests.append(body)
                step = outer.script[index] if index < len(outer.script) else ("text", "done")
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                if step[0] == "stall":
                    # Headers sent, then nothing: omp's first-event watchdog fires.
                    time.sleep(step[1])
                    self.close_connection = True
                    return
                if "messages" in body:
                    # Chat completions (the "openai-completions" api), text steps only.
                    chunk = {"id": "c", "object": "chat.completion.chunk", "model": "stub"}
                    for delta, finish in (
                        ({"role": "assistant", "content": step[1]}, None),
                        ({}, "stop"),
                    ):
                        payload = dict(
                            chunk, choices=[{"index": 0, "delta": delta, "finish_reason": finish}]
                        )
                        self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                for chunk in outer._events(step, index):
                    self.wfile.write(chunk)
                self.wfile.flush()
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @staticmethod
    def _events(step, index):
        created = _sse("response.created", {"response": _response([], "in_progress")})
        if step[0] == "text":
            item = {
                "type": "message",
                "id": "msg_%d" % index,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": step[1], "annotations": []}],
            }
            return [
                created,
                _sse(
                    "response.output_item.added",
                    {"output_index": 0, "item": dict(item, status="in_progress", content=[])},
                ),
                _sse(
                    "response.content_part.added",
                    {
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                ),
                _sse(
                    "response.output_text.delta",
                    {
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": step[1],
                    },
                ),
                _sse("response.output_item.done", {"output_index": 0, "item": item}),
                _sse("response.completed", {"response": _response([item])}),
            ]
        _, name, args = step
        arguments = json.dumps(args)
        item = {
            "type": "function_call",
            "id": "fc_%d" % index,
            "call_id": "call_%d" % index,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
        return [
            created,
            _sse(
                "response.output_item.added",
                {"output_index": 0, "item": dict(item, status="in_progress", arguments="")},
            ),
            _sse(
                "response.function_call_arguments.delta",
                {"item_id": item["id"], "output_index": 0, "delta": arguments},
            ),
            _sse(
                "response.function_call_arguments.done",
                {"item_id": item["id"], "output_index": 0, "arguments": arguments},
            ),
            _sse("response.output_item.done", {"output_index": 0, "item": item}),
            _sse("response.completed", {"response": _response([item])}),
        ]

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def tool_names(self, request_index=0):
        """The tool names the request advertised to the model."""
        return [tool.get("name") for tool in self.requests[request_index].get("tools", [])]

    def tool_output(self, call_id):
        """The text omp sent back for one scripted call, across every request."""
        for request in reversed(self.requests):
            for item in request.get("input", []):
                if item.get("type") == "function_call_output" and item.get("call_id") == call_id:
                    output = item.get("output")
                    return output if isinstance(output, str) else json.dumps(output)
        return None


def write_agent_dir(port, retry=None, settings=None, api="openai-codex-responses"):
    """A throwaway omp agent dir whose only provider is the loopback model.
    ``retry`` replaces the default of no retries (omp's ``retry`` settings);
    ``settings`` adds to the rest of ``config.yml``."""
    directory = tempfile.mkdtemp(prefix="ember-loopback-agent-")
    models = {
        "providers": {
            PROVIDER: {
                "baseUrl": "http://127.0.0.1:%d" % port,
                "api": api,
                "apiKey": TOKEN_ENV,
                "models": [
                    {
                        "id": "stub",
                        "name": "stub",
                        "input": ["text"],
                        "contextWindow": 200000,
                        "maxTokens": 4096,
                    }
                ],
            }
        }
    }
    with open(os.path.join(directory, "models.yml"), "w") as handle:
        json.dump(models, handle)
    with open(os.path.join(directory, "config.yml"), "w") as handle:
        json.dump(
            {"retry": retry or {"enabled": False, "modelFallback": False}, **(settings or {})},
            handle,
        )
    return directory


def loopback_env(agent_dir):
    return dict(
        os.environ,
        PI_CODING_AGENT_DIR=agent_dir,
        PI_CODEX_WEBSOCKET="0",
        **{TOKEN_ENV: JWT},
    )
