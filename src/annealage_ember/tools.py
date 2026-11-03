"""Assembly of a product's tool server, and the two policies every tool obeys.

A product supplies its tools as ``claude_agent_sdk`` ``@tool`` definitions
(its ``Product.build_tools``) together with a ``Grading`` that sorts every one
of them into three grades by **what a mistake would cost**. This module turns
the two into one in-process MCP server, ``ToolServer``, and owns everything
about that server that is not the product's own: the refusal to build a set
whose grading does not match it, the pause gate, the mapping of viewer
failures to messages a model can act on, the pre-allowed name list every
backend receives, and the transport-neutral ``tool_table`` the non-Claude
backends build their own servers from. A product may also declare remote MCP
servers beside its own tools (``remote.py``); the server proxies their tools
under grades the product gives them, through the same gate and mapping.

``read`` changes nothing. Reading the view, a list of the project's parts, the
comments or a screenshot (Mesh's read tools) leaves the project and the view
exactly as they were, so these are
pre-allowed and never interrupt anyone.

``view`` changes only what is on the screen, and does so in front of the
human, who is looking at that screen. These are pre-allowed too. The
reasoning is not that they are harmless in the abstract, it is that an
approval card is the wrong control for them: the loop a product's tools exist
for has the model reframing something it has just changed, several times a
turn, and a card per view change (in Mesh, per camera move) would either be
clicked without reading or
turned off with one standing grant, which is worse than not asking. The
control that fits is the pause switch, which refuses all of them at once for
as long as the human wants the view to hold still, and that is what it is
for.

``write`` leaves something behind after the page is closed: in Mesh, a
callout in a file the human's own tooling reads, an image in the project directory, or a
transcript that carries whatever was said about the hardware under review.
These are deliberately **absent** from every allow list, which is what makes
them reach the broker and therefore the human as a card. Adding one of these
names to a pre-allowed list anywhere would silently remove that card.

So two derived sets follow, and they are not the same set:
``Grading.pre_allowed`` is read plus view, and ``Grading.pause_gated`` is view
plus write. The product's grading is the only place any of this is written
down, and ``_verify`` refuses to build a server whose tools do not match it
exactly, so a tool added to a product without being graded fails at startup
rather than defaulting into a posture nobody chose.

One kind of tool is graded by the agent layer instead: one whose handler asks
the human itself, per call, with a question only it can phrase (resolving one
of the human's review comments). ``asks_the_human`` marks it, and the server
treats it as read-grade whatever the product said, so no transport asks a
vaguer question first; the product must still grade it, like any tool.

Every handler returns one of ``ok`` or ``fail`` and nothing else. A tool
result reaches the model as text, so what a handler returns is prose it will
read: ``ok`` renders a payload as indented JSON, because coordinates and part
names are what these tools are for and JSON is the shape a model reads them
out of most reliably, and ``fail`` returns a sentence saying what went wrong
and what to do instead. That second half matters more than it looks: a deny's
message reaches the model verbatim (plan section 2a, fact 15), so "no viewer
connected; ask the human to open <url>" is worth more than a status code.

A session may supply one synchronous ``call_recorder`` to ``ToolServer``.
It receives a bounded, value-free terminal summary for each call reaching
the wrapper, SDK middleware, or Ember's transport permission gates. Value
summaries use a private, ephemeral key shared within that server, so equal
values correlate within a session but not across servers. It is not a
permission gate or replay log. Calls refused by a backend before it invokes
those boundaries are not visible. Recorder failures do not change tool execution.

Importing this module imports ``claude_agent_sdk``, so only agent-mode code
paths import it; a viewer-only run never does.
"""

import asyncio
import contextvars
import copy
import dataclasses
import functools
import hmac
import itertools
import json
import math
import re
import secrets
import sys
import time
from collections import namedtuple
from typing import get_origin
from urllib.parse import unquote

from claude_agent_sdk import create_sdk_mcp_server
from jsonschema import Draft7Validator, Draft202012Validator
from pydantic import TypeAdapter, create_model
from referencing import Registry

from . import product
from .viewers import CallError, NoViewerConnected, ViewerGone, ViewerTimeout


def namespaced(server_name, name):
    """The model-visible name of tool ``name`` on the MCP server
    ``server_name``: ``mcp__<server>__<tool>``. A bare name in an allow list,
    a deny list or a hook matcher silently matches nothing (plan section 2,
    fact 1), so nothing writes one by hand: it goes through here."""
    return "mcp__%s__%s" % (server_name, name)


def host_tool_name(server_name, name):
    """The name omp registers remote server ``server_name``'s tool ``name``
    under as a host tool: ``<server>__<tool>``. A product's own tools keep
    their bare names there (``ToolServer.host_tool_table``), except one named
    like an omp tool, which ``session/omp.py``'s ``_omp_name`` prefixes the
    same way."""
    return "%s__%s" % (server_name, name)


def ok(payload=None, *, text=None, end_turn=False):
    """A successful tool result: ``payload`` as JSON, or ``text`` verbatim.

    ``end_turn`` ends the agent's turn once this result has reached it
    (``ViewerBus.request_end_turn``): for a tool whose point is to hand
    control back to the human, Annealage Loom's ``checkpoint``. The key never
    reaches a backend (``_wrap`` takes it off), and each backend stops the
    turn in its own way, or cannot (see the README), so the text should also
    tell the model to stop and wait."""
    if text is None:
        text = json.dumps(payload, indent=2, default=str)
    result = {"content": [{"type": "text", "text": text}]}
    if end_turn:
        result["end_turn"] = True
    return result


def _safe_text(value, limit=240):
    """Bound diagnostic text and remove common credential and URL forms."""
    text = str(value)
    text = re.sub(r"https?://\S+", "[redacted URL]", text, flags=re.I)
    text = re.sub(
        r"(?i)\b(bearer\s+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*)\S+",
        r"\1[redacted]",
        text,
    )
    return text[:limit]


def fail(message, *, diagnostic=None):
    """Return a native failure; optional structured diagnostics travel in text.

    Diagnostic callers supply only safe structural information, never candidate
    values or arbitrary upstream data. The renderer bounds every field.
    """
    if diagnostic is not None:
        issues = diagnostic.get("issues", ())
        rendered = []
        truncated = max(0, len(issues) - 16) + max(0, int(diagnostic.get("truncated", 0)))

        def bounded(value, limit):
            nonlocal truncated
            text = _safe_text(value, limit)
            if len(_safe_text(value, limit + 1)) > limit:
                truncated += 1
            return text

        for issue in issues[:16]:
            location = issue.get("location", ())
            truncated += max(0, len(location) - 12)
            rendered.append(
                {
                    "location": [bounded(part, 80) for part in location[:12]],
                    "code": bounded(issue.get("code", "invalid"), 64),
                    "message": bounded(issue.get("message", ""), 240),
                }
            )
        code = bounded(diagnostic.get("code", "tool_error"), 64)
        reason = bounded(message, 240)
        document = {
            "code": code,
            "message": reason,
            "issues": rendered,
            "truncated": truncated,
            "structural_correction": bool(diagnostic.get("structural_correction", False)),
        }
        if diagnostic.get("incomplete"):
            # This is a lower bound on omitted issues or validation work.
            document["incomplete"] = True
        text = json.dumps(document, ensure_ascii=True)
    else:
        text = message
    return {"content": [{"type": "text", "text": text}], "is_error": True}


class Grading(namedtuple("Grading", "read view write")):
    """A product's tool names in their three grades, each a tuple of bare
    names (see this module's docstring for what each grade means).

    The tuples keep the product's own order, so the allow list derived from
    them can be read against whatever plan lists the tools line by line.
    """

    __slots__ = ()

    @property
    def pre_allowed(self):
        """What never prompts, as the model sees it: read plus view."""
        return self.read + self.view

    @property
    def pause_gated(self):
        """What the pause switch refuses: everything that changes anything,
        whether the change is to the screen or to the project. Deliberately not
        the same set as what prompts, because the two questions are different:
        a card asks "may this happen at all", and the pause switch says "not
        right now, I am working"."""
        return self.view + self.write


#: One already-``_wrap``-gated tool, in the shape a non-Claude MCP bridge
#: (``http/routes_mcp.py``) or host-tool driver (``session/omp.py``) needs to
#: build its own server from - ``ToolServer.tool_table()``'s values. ``write``
#: folds in write-grade membership so ``routes_mcp`` can gate a write-grade
#: call through ``PermissionBroker`` (Codex's app-server has no equivalent of
#: Claude's own ``can_use_tool`` hook for a generic MCP tool call - see that
#: module's docstring).
ToolSpec = namedtuple("ToolSpec", "schema description handler write")


def _tool_json_schema(input_schema):
    """Derive shorthand from its types, or preserve a full JSON Schema."""
    if isinstance(input_schema, dict):
        shorthand = all(
            isinstance(value, type) or get_origin(value) is not None
            for value in input_schema.values()
        )
        if shorthand:
            model = create_model(
                "ToolArguments",
                **{name: (annotation, ...) for name, annotation in input_schema.items()},
            )
            schema = model.model_json_schema()
            if set(schema.get("properties", {})) != set(input_schema):
                raise ValueError("tool shorthand contains unsupported field names")
        else:
            schema = copy.deepcopy(input_schema)
    else:
        schema = TypeAdapter(input_schema).json_schema()
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("tool input schema must declare an object")
    # The public Claude SDK recognises a full schema only when this key exists.
    # An empty properties keyword imposes no additional constraint.
    schema.setdefault("properties", {})
    return schema


def _compile_schema(schema):
    """Check the supported dialect and every local reference before publication."""
    dialect = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    dialects = {
        "https://json-schema.org/draft/2020-12/schema": Draft202012Validator,
        "http://json-schema.org/draft-07/schema#": Draft7Validator,
    }
    if dialect not in dialects:
        raise ValueError("unsupported JSON Schema dialect")
    validator_type = dialects[dialect]
    validator_type.check_schema(schema)
    annotations = {
        "$schema",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "$defs",
        "definitions",
    }
    mappings = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    singles = {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "additionalItems",
        "contains",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    refs = []
    names = set()
    visited = set()
    discriminators = []

    def inspect(node):
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError("invalid subschema")
        visited.add(id(node))
        for key, value in node.items():
            if key == "$schema" and node is not schema:
                raise ValueError("nested JSON Schema dialects are unsupported")
            if key in {
                "$id",
                "$anchor",
                "$dynamicAnchor",
                "$dynamicRef",
                "$recursiveRef",
                "$recursiveAnchor",
                "format",
                "contentSchema",
                "contentEncoding",
                "contentMediaType",
            }:
                raise ValueError("unsupported JSON Schema keyword: " + key)
            if key == "discriminator":
                if "oneOf" not in node or not isinstance(value, dict):
                    raise ValueError("discriminator requires oneOf")
                mapping = value.get("mapping")
                if (
                    set(value) != {"propertyName", "mapping"}
                    or not isinstance(value["propertyName"], str)
                    or not isinstance(mapping, dict)
                    or not mapping
                ):
                    raise ValueError("unsupported discriminator")
                for ref in mapping.values():
                    if not isinstance(ref, str) or not ref.startswith("#/"):
                        raise ValueError("only local discriminator references are supported")
                    refs.append(ref)
                discriminators.append((node, value))
                continue
            if key not in validator_type.VALIDATORS and key not in annotations and key != "$ref":
                raise ValueError("unsupported JSON Schema keyword: " + key)
            if key == "$ref":
                if not isinstance(value, str) or not (value == "#" or value.startswith("#/")):
                    raise ValueError("only local JSON pointer references are supported")
                refs.append(value)
            elif key in mappings:
                if key == "properties":
                    names.update(value)
                for child in value.values():
                    inspect(child)
            elif key in singles:
                if isinstance(value, list):
                    for child in value:
                        inspect(child)
                else:
                    inspect(value)
            elif key in arrays:
                for child in value:
                    inspect(child)
            elif key == "dependencies":
                for child in value.values():
                    if isinstance(child, (dict, bool)):
                        inspect(child)

    inspect(schema)

    def resolve(ref):
        target = schema
        try:
            for part in unquote(ref[2:]).split("/") if ref != "#" else ():
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
            if not isinstance(target, bool) and id(target) not in visited:
                raise ValueError("reference target is not a schema")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("unresolvable local JSON Schema reference") from exc
        return target

    for ref in refs:
        resolve(ref)
    for node, discriminator in discriminators:
        mapping = discriminator["mapping"]
        branches = node["oneOf"]
        if any(not isinstance(branch, dict) or set(branch) != {"$ref"} for branch in branches):
            raise ValueError("unsupported discriminator branches")
        if set(mapping.values()) != {branch["$ref"] for branch in branches}:
            raise ValueError("discriminator mapping does not match oneOf")
        field = discriminator["propertyName"]
        for tag, ref in mapping.items():
            target = resolve(ref)
            if not isinstance(tag, str) or not isinstance(target, dict):
                raise ValueError("unsupported discriminator target")
            prop = target.get("properties", {}).get(field, {})
            if (
                field not in target.get("required", ())
                or not isinstance(prop, dict)
                or not (prop.get("const") == tag or prop.get("enum") == [tag])
            ):
                raise ValueError("discriminator is not enforced by its branches")
    return validator_type(schema, registry=Registry()), names


def _input_diagnostic(validator, names, args, *, resource_only=False, trusted_schema=True):
    """Bound the candidate before optionally validating it against its schema."""
    issues = []
    omitted = 0

    def safe_entry(value):
        # Entries are identifiers or values, not prose: never turn an altered
        # or shortened entry into a purportedly valid correction.
        return (
            isinstance(value, str)
            and len(value) <= 80
            and value.isprintable()
            and _safe_text(value, 81) == value
        )

    def location_part(part):
        nonlocal omitted
        if isinstance(part, int):
            return part
        if part in names and safe_entry(part):
            return part
        if part in names:
            omitted += 1
        return "<field>"

    def entries_message(message, label, entries):
        nonlocal omitted
        selected = []
        skipped = max(0, len(entries) - 16)
        for entry in itertools.islice(entries, 16):
            if not safe_entry(entry):
                skipped += 1
                continue
            # JSON quoting makes punctuation and whitespace unambiguous.
            shown = entry if entry.isidentifier() else json.dumps(entry, ensure_ascii=True)
            candidate = selected + [shown]
            if len(message) + len(label) + len(", ".join(candidate)) + 16 > 220:
                skipped += 1
                continue
            selected.append(shown)
        if selected:
            message += label + ", ".join(selected)
        if skipped:
            message += " (some omitted)"
            omitted += skipped
        return message

    def append_description(message, description):
        nonlocal omitted
        if not isinstance(description, str) or not description:
            return message
        text = _safe_text(description, 120)
        if len(message) + 2 + len(text) > 240:
            omitted += 1
            return message
        return message + "; " + text

    def incomplete(message, *, omitted=1):
        return {
            "code": "invalid_input",
            "issues": issues or [{"location": (), "code": "input_limit", "message": message}],
            "truncated": omitted,
            "incomplete": True,
            "structural_correction": True,
        }

    # Iterators, rather than a stack of children, keep memory proportional to
    # depth even for wide objects. Reject cycles and oversized inputs before
    # handing them to jsonschema, which expects a finite JSON tree.
    stack = [(iter((((), args),)), None)]
    active = set()
    nodes = 0
    text_size = 0
    while stack:
        children, container_id = stack[-1]
        try:
            path, value = next(children)
        except StopIteration:
            stack.pop()
            if container_id is not None:
                active.remove(container_id)
            continue
        except ValueError:
            return incomplete("Input is not a bounded JSON object")
        nodes += 1
        if nodes > 10000 or len(path) > 64:
            return incomplete("Input exceeds validation limits")
        if type(value) is str:
            text_size += len(value)
            if len(value) > 1000000 or text_size > 4000000:
                return incomplete("Input exceeds validation limits")
        elif type(value) is int and value.bit_length() > 4096:
            return incomplete("Input exceeds validation limits")
        elif not resource_only and type(value) is float and not math.isfinite(value):
            if len(issues) == 16:
                return incomplete("Additional input issues omitted", omitted=1)
            issues.append(
                {"location": path, "code": "number", "message": "Expected a finite JSON number"}
            )
        elif type(value) in (dict, list):
            identity = id(value)
            if identity in active:
                return incomplete("Input is not a finite JSON tree")
            active.add(identity)
            if type(value) is dict:

                def object_children(obj, parent):
                    nonlocal text_size
                    for key, child in obj.items():
                        if type(key) is not str:
                            if resource_only:
                                yield parent + ("<field>",), child
                                continue
                            raise ValueError("non-string JSON object key")
                        text_size += len(key)
                        if len(key) > 1000000 or text_size > 4000000:
                            raise ValueError("input exceeds validation limits")
                        yield parent + (location_part(key),), child

                iterator = object_children(value, path)
            else:

                def array_children(array, parent):
                    for index, child in enumerate(array):
                        yield parent + (index,), child

                iterator = array_children(value, path)
            stack.append((iterator, identity))
        elif not resource_only and type(value) not in (int, float, bool, type(None)):
            return incomplete("Input is not a JSON value")

    if resource_only:
        return None

    try:
        for error in validator.iter_errors(args):
            if len(issues) == 16:
                return incomplete("Additional input issues omitted", omitted=1)
            location = [location_part(part) for part in error.absolute_path]
            keyword = str(error.validator or "invalid")
            message = "Input does not satisfy " + keyword
            if keyword == "type" and isinstance(error.validator_value, (str, list)):
                message += ": expected " + _safe_text(error.validator_value, 100)
            if keyword == "enum" and isinstance(error.validator_value, list):
                if trusted_schema:
                    message = entries_message(message, ": allowed values ", error.validator_value)
                else:
                    message += " (allowed values omitted)"
                    omitted += len(error.validator_value)
            if keyword == "required" and isinstance(error.instance, dict):
                missing = [name for name in error.validator_value if name not in error.instance]
                message = entries_message(message, ": required fields ", missing)
                properties = (
                    error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
                )
                if trusted_schema and isinstance(properties, dict):
                    for name in missing[:16]:
                        field = properties.get(name)
                        if safe_entry(name) and isinstance(field, dict):
                            description = field.get("description")
                            if isinstance(description, str):
                                message = append_description(message, name + ": " + description)
            if keyword == "additionalProperties" and isinstance(error.schema, dict):
                allowed = error.schema.get("properties", {})
                if isinstance(allowed, dict):
                    message = entries_message(message, ": declared fields ", allowed)
            if trusted_schema and isinstance(error.schema, dict):
                message = append_description(message, error.schema.get("description"))
            issues.append({"location": location, "code": keyword, "message": message})
    except Exception:
        return incomplete("Input could not be safely validated")
    if not issues:
        return None
    return {
        "code": "invalid_input",
        "issues": issues,
        "truncated": omitted,
        "structural_correction": True,
    }


def _verify(tools, grading):
    """Refuse a tool set that does not match ``grading`` exactly."""
    name = product.current().name
    built = [t.name for t in tools]
    duplicated = sorted({n for n in built if built.count(n) > 1})
    if duplicated:
        raise RuntimeError("%s tools declared twice: %s" % (name, ", ".join(duplicated)))
    grades = {"read": grading.read, "view": grading.view, "write": grading.write}
    for first, second in (("read", "view"), ("read", "write"), ("view", "write")):
        overlap = sorted(set(grades[first]) & set(grades[second]))
        if overlap:
            raise RuntimeError(
                "%s tool(s) %s are classified both %s and %s"
                % (name, ", ".join(overlap), first, second)
            )
    classified = set(grading.read) | set(grading.view) | set(grading.write)
    unclassified = sorted(set(built) - classified)
    if unclassified:
        raise RuntimeError(
            "%s tool(s) %s are built but not classified read, view or write in "
            "the product's tool grading; every default is wrong for something, so "
            "there is no default" % (name, ", ".join(unclassified))
        )
    missing = sorted(classified - set(built))
    if missing:
        raise RuntimeError(
            "%s tool(s) %s are classified in the product's tool grading but not "
            "built, so their names are pre-allowed and match nothing" % (name, ", ".join(missing))
        )


_SDK_CALL = contextvars.ContextVar("ember_sdk_call", default=None)
_CALL_RECORD = contextvars.ContextVar("ember_call_record", default=None)


def _record_stage(stage, outcome=None):
    record = _CALL_RECORD.get()
    if record is not None:
        record["stage"] = stage
        if outcome is not None:
            record["outcome"] = outcome


class _CallRecorder:
    """Keep a session-local summary key separate from the emitted events."""

    def __init__(self, callback):
        self.callback = callback
        key = secrets.token_bytes(32)

        def summarize(value):
            try:
                if _input_diagnostic(None, (), value, resource_only=True) is not None:
                    return {"hmac_sha256": None, "size": None}
                encoded = json.dumps(
                    value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
                return {
                    "hmac_sha256": hmac.digest(key, encoded, "sha256").hex(),
                    "size": len(encoded),
                }
            except (TypeError, ValueError, RecursionError, OverflowError):
                return {"hmac_sha256": None, "size": None}

        self.summarize = summarize

    def __call__(self, event):
        self.callback(event)


def _call_recorder(recorder):
    if recorder is None or isinstance(recorder, _CallRecorder):
        return recorder
    return _CallRecorder(recorder)


def _emit_call(recorder, server, tool, arguments, result, outcome, stage):
    if recorder is None:
        return
    try:
        recorder(
            {
                "server": server,
                "tool": tool,
                "outcome": outcome,
                "is_error": (
                    bool(result.get("is_error", result.get("isError", False)))
                    if isinstance(result, dict)
                    else None
                ),
                "stage": stage,
                "arguments": arguments,
                "result": recorder.summarize(result) if result is not None else None,
            }
        )
    except BaseException:
        # Observability is never authority, including during cancellation.
        pass


# The attribute ``asks_the_human`` sets on a tool's handler function.
_ASKS_THE_HUMAN = "_annealage_asks_the_human"


def asks_the_human(tool_def):
    """Mark ``tool_def`` as a tool whose handler asks the human itself, and
    return it.

    Such a tool decides per call whether a call needs a card (resolving one
    of the human's review comments does, resolving the model's own callout
    does not; ``review/tools.py``), and asks the ``PermissionBroker`` itself
    with a request that says what it is about. So ``ToolServer`` treats it as
    read-grade whatever grade the product gave it: pre-allowed, never marked
    write in ``tool_table``, so no transport asks a second, less specific,
    question first. Under the default policy it is not pause-gated, since
    every call that changes anything the human cares about asks them anyway;
    automatic execution instead pause-gates it. Its model-visible and bare
    names are the server's ``never_remembered``: no "always allow" may stand
    in for the question it asks.
    """
    setattr(tool_def.handler, _ASKS_THE_HUMAN, True)
    return tool_def


def _wrap(
    tool_def,
    *,
    bus,
    gated,
    paused_message,
    hosted_key=None,
    trusted_schema=True,
    call_recorder=None,
    server_name=None,
):
    """Apply the pause gate and the failure mapping to one tool.

    Both live here rather than in each handler, which is what lets the handler
    modules be plain argument-validate-call-return code with no ``try`` blocks
    of their own. The mapping is not cosmetic: the four ways a viewer call can
    fail mean four different things to a model, and flattening them to one
    message would leave it retrying a call that will never work or giving up on
    one that would work on the next attempt.

    ``ValueError`` is the handler modules' way of rejecting an argument, and
    its message is written for the model, so it is passed through verbatim.
    Anything else reaching here is a bug in the product: it is logged for the
    human with the tool's name and reported as a failed call, rather than
    raised into the MCP layer, where it would reach the model as an
    infrastructure error that says nothing about which tool broke.

    ``paused_message`` is what a gated tool says while the human has paused
    the view; the product writes it, because what "still possible" means is
    the product's to say. A callable is asked at the time of the call, for a
    message that depends on what the server turned out to hold (which remote
    MCP servers it reached, say).
    """
    call_recorder = _call_recorder(call_recorder)
    name = product.current().name
    schema = _tool_json_schema(tool_def.input_schema)
    validator, names = _compile_schema(schema)

    def input_diagnostic(args):
        return _input_diagnostic(validator, names, args, trusted_schema=trusted_schema)

    async def execute(args):
        if getattr(bus, "hosted_mode", False):
            operation = bus.hosted_tool_ops.get(hosted_key)
            if (
                operation is None
                or not bus.hosted_turn_live
                or time.time() >= bus.hosted_turn_exp
                or operation not in bus.hosted_turn_ops
            ):
                _record_stage("hosted", "rejected")
                return fail("hosted operation is not granted for this tool call")
        automatically_gated = getattr(bus, "auto_approve_included_tools", False) and getattr(
            tool_def.handler, _ASKS_THE_HUMAN, False
        )
        if (gated or automatically_gated) and bus.paused:
            _record_stage("pause", "rejected")
            return fail(paused_message() if callable(paused_message) else paused_message)
        diagnostic = input_diagnostic(args)
        if diagnostic is not None:
            _record_stage("schema", "rejected")
            return fail("Tool input does not match its declared schema", diagnostic=diagnostic)
        try:
            result = await tool_def.handler(args)
        except ValueError as exc:
            _record_stage("handler", "exception")
            return fail(str(exc))
        except NoViewerConnected as exc:
            _record_stage("handler", "exception")
            # Carries plan section 3.3's exact wording, including the URL to
            # ask the human to open, so it is passed through unedited.
            return fail(str(exc))
        except ViewerGone:
            _record_stage("handler", "exception")
            return fail(
                "the view this went to closed before it answered, so %s "
                "did not happen; ask the product owner whether the page is still "
                "open, then try again" % tool_def.name
            )
        except ViewerTimeout:
            _record_stage("handler", "incomplete")
            return fail(
                "the viewer did not answer %s in time, so it may or may "
                "not have happened; the page may be busy or in a "
                "background tab. Read the state back before assuming "
                "either way" % tool_def.name
            )
        except (TimeoutError, asyncio.TimeoutError):
            _record_stage("handler", "incomplete")
            return fail(
                "%s reached a host/tool deadline; its outcome is not established. "
                "Read the relevant state back before assuming completion or "
                "retrying" % tool_def.name
            )
        except CallError as exc:
            _record_stage("handler", "exception")
            error = exc.error or {}
            return fail(
                "the viewer refused %s: %s (%s)"
                % (
                    tool_def.name,
                    error.get("message", "no reason given"),
                    error.get("code", "no code"),
                )
            )
        except Exception as exc:
            _record_stage("handler", "exception")
            sys.stderr.write("error: %s tool %s failed: %r\n" % (name, tool_def.name, exc))
            return fail(
                "%s failed inside %s itself (%s), which is a bug rather "
                "than anything you did; tell the product owner and carry on "
                "without it" % (tool_def.name, name, type(exc).__name__)
            )
        if isinstance(result, dict) and result.get("end_turn"):
            # Off the result before any transport sees it, and to the session
            # through the bus: this handler runs before the result reaches the
            # backend, which is why the session only marks the turn here and
            # stops it once the result has been delivered.
            result = {key: value for key, value in result.items() if key != "end_turn"}
            bus.request_end_turn()
        return result

    async def handler(args):
        marker = _SDK_CALL.get()
        if marker is not None:
            marker["entered"] = True
        if call_recorder is None:
            return await execute(args)
        arguments = call_recorder.summarize(args)
        record = {"stage": "handler", "outcome": None}
        token = _CALL_RECORD.set(record)
        result = None
        try:
            result = await execute(args)
            if record["outcome"] is None:
                record["outcome"] = (
                    "failure"
                    if isinstance(result, dict)
                    and result.get("is_error", result.get("isError", False))
                    else "success"
                )
            return result
        except asyncio.CancelledError:
            record["outcome"] = "cancelled"
            raise
        except BaseException:
            record["outcome"] = "exception"
            raise
        finally:
            _CALL_RECORD.reset(token)
            if marker is not None:
                marker["record"] = (arguments, record["outcome"] or "incomplete", record["stage"])
            else:
                _emit_call(
                    call_recorder,
                    server_name,
                    tool_def.name,
                    arguments,
                    result,
                    record["outcome"] or "incomplete",
                    record["stage"],
                )

    def record_refusal(args, outcome="rejected", result=None):
        if call_recorder is not None:
            _emit_call(
                call_recorder,
                server_name,
                tool_def.name,
                call_recorder.summarize(args),
                result,
                outcome,
                "policy",
            )

    handler._annealage_record_refusal = record_refusal
    handler._annealage_call_recorder = call_recorder
    handler._annealage_input_diagnostic = input_diagnostic
    return dataclasses.replace(tool_def, input_schema=schema, handler=handler)


def _attach_sdk_diagnostics(server, tools, *, call_recorder=None, server_name=None):
    """Attach bounded preflight and safe post-validation diagnostics to an SDK server.

    ``tools`` must be the same wrapped tools supplied to the SDK. In-bounds
    calls still use native SDK validation; the existing compiled validators
    only describe its pre-handler rejection.
    """
    instance = server["instance"]
    middleware = getattr(instance, "middleware", None)
    if not isinstance(middleware, list):
        raise RuntimeError("MCP server middleware is required for safe SDK diagnostics")
    handlers = {tool.name: tool.handler for tool in tools}
    if call_recorder is not None:
        call_recorder = next(
            (
                handler._annealage_call_recorder
                for handler in handlers.values()
                if getattr(handler, "_annealage_call_recorder", None) is not None
            ),
            None,
        ) or _call_recorder(call_recorder)

    def record_refusal(name, args, outcome="rejected", result=None):
        handler = handlers.get(name)
        if handler is not None:
            handler._annealage_record_refusal(args, outcome, result)

    async def safe_rejection(ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params or {}
        supplied_name = params.get("name")
        handler = handlers.get(supplied_name) if isinstance(supplied_name, str) else None
        safe_name = supplied_name if handler is not None else "<unknown>"
        arguments = (
            call_recorder.summarize(params.get("arguments")) if call_recorder is not None else None
        )

        def recorded(result, outcome):
            _emit_call(call_recorder, server_name, safe_name, arguments, result, outcome, "sdk")
            return result

        if _input_diagnostic(None, (), params.get("arguments"), resource_only=True) is not None:
            replacement = fail("Tool input exceeds safe structural limits; no values are echoed.")
            return recorded({"isError": True, "content": replacement["content"]}, "rejected")
        marker = {"entered": False}
        token = _SDK_CALL.set(marker)
        try:
            result = await call_next(ctx)
        except BaseException as exc:
            saved = marker.get("record")
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "incomplete"
            if saved is not None:
                _emit_call(call_recorder, server_name, safe_name, saved[0], None, outcome, saved[2])
            elif not marker["entered"]:
                recorded(None, outcome)
            raise
        finally:
            _SDK_CALL.reset(token)
        if marker["entered"]:
            saved = marker.get("record")
            if saved is not None:
                outcome = saved[1]
                if outcome in ("success", "failure") and isinstance(result, dict):
                    outcome = (
                        "failure"
                        if result.get("isError", result.get("is_error", False))
                        else "success"
                    )
                _emit_call(
                    call_recorder, server_name, safe_name, saved[0], result, outcome, saved[2]
                )
            return result
        if not isinstance(result, dict):
            recorded(None, "incomplete")
            raise RuntimeError("Unexpected MCP middleware result shape")
        if not result.get("isError"):
            return recorded(result, "success")
        if handler is None:
            replacement = fail("Unknown tool.")
            return recorded({"isError": True, "content": replacement["content"]}, "rejected")
        diagnostic = None
        try:
            diagnostic = handler._annealage_input_diagnostic(params.get("arguments") or {})
        except Exception:
            # Never relay an exception or the SDK's potentially value-bearing text.
            pass
        if diagnostic is None:
            replacement = fail("Tool input was rejected before the tool ran; no values are echoed.")
        else:
            replacement = fail(
                "Tool input does not match its declared schema", diagnostic=diagnostic
            )
        return recorded({"isError": True, "content": replacement["content"]}, "rejected")

    safe_rejection._annealage_record_refusal = record_refusal

    middleware.append(safe_rejection)
    return server


#: The most of one remote MCP server's ``initialize`` instructions a session's
#: system prompt carries (``ToolServer.remote_instructions``).
MAX_REMOTE_INSTRUCTIONS = 8192


def _table(tool_defs, grading):
    """``tool_table``'s shape for ``tool_defs``, graded by ``grading``."""
    write = set(grading.write)
    return {
        tool_def.name: ToolSpec(
            schema=tool_def.input_schema,
            description=tool_def.description,
            handler=tool_def.handler,
            write=tool_def.name in write,
        )
        for tool_def in tool_defs
    }


class ToolServer:
    """A product's tool server for one session.

    Built per session rather than at import, because every handler closes over
    the ``ViewerBus`` and the served directory of the run it belongs to.
    ``tools`` are the product's ``@tool`` definitions, ``grading`` their
    grades, ``bus`` the ``ViewerBus`` holding the pause switch, and
    ``paused_message`` what a gated tool answers while it is on (a string,
    or a callable returning one when the call is refused).

    A tool marked ``asks_the_human`` is moved to the read grade whatever
    ``grading`` says (see that function); ``self.grading`` is the grading
    after that move, the one every derived list is built from.

    The server is named after the installed product's ``mcp_server_name``,
    and so is ``mcp_servers``' first key, deliberately: the key is what the
    model-visible ``mcp__<key>__<tool>`` name is built from, so a key that
    disagreed with the name ``pre_allowed`` is built from would leave every
    pre-allowed name matching nothing and every one of those tools prompting.

    ``remote`` are the ``remote.RemoteServer``s the product declares beside
    its own tools, connected to here, once (``remote.py`` says how, and what
    happens to one that cannot be reached). Each one the session reached is
    in ``self.remotes`` and is a server namespace of its own on every
    backend: another key of ``mcp_servers`` (Claude sees
    ``mcp__<remote>__<tool>``), another route, ``/mcp/<remote>``, for the
    Codex bridge (``remote_tables``), and host tools named
    ``<remote>__<tool>`` for omp (``host_tool_table``). Its tools are graded,
    pause-gated and failure-mapped exactly like the product's own.

    A declared remote that could not be reached is in ``self.unreached``
    (``RemoteServer``s), and ``reconnect`` tries those again: ``serve`` does,
    on a timer and on the session's first turn (``app.py``), and hands a
    session that can take new tools mid-session the grown table.

    ``remote_servers`` is every declared remote by name, reached or not, for
    a call the human starts from the page (an upload action, ``uploads.py``),
    which opens its own connection as a proxied call does.
    """

    def __init__(
        self,
        tools,
        *,
        grading,
        bus,
        paused_message,
        remote=(),
        remote_snapshot=None,
        remote_guard=None,
        request_guard=None,
        never_remembered=(),
        call_recorder=None,
    ):
        """Build one session's tool server.

        ``request_guard`` optionally implements ``capture(tool_name, args)``
        and ``validate(token, tool_name, args)``. The permission broker uses
        it to bind a write approval to the authority present when its card was
        requested and reject a stale approval before allowing the call through.

        ``never_remembered`` names product tools whose transport approval must
        be fresh on every call. Unlike ``asks_the_human``, this keeps their
        grading and transport gate unchanged. Bare names are expanded for all
        backend broker names; legacy standing grants are ignored by the broker.
        """
        _verify(tools, grading)
        call_recorder = _call_recorder(call_recorder)
        self.request_guard = (
            request_guard if request_guard is not None else getattr(bus, "request_guard", None)
        )
        installed = product.current()
        self.name = installed.mcp_server_name
        self.asks_the_human = tuple(
            t.name for t in tools if getattr(t.handler, _ASKS_THE_HUMAN, False)
        )
        never_remembered = tuple(never_remembered)
        unknown = set(never_remembered) - {t.name for t in tools}
        if unknown:
            raise ValueError(
                "never_remembered names are not tools: %s" % ", ".join(sorted(unknown))
            )
        self._never_remembered = tuple(dict.fromkeys((*self.asks_the_human, *never_remembered)))
        moved = set(self.asks_the_human)
        self.grading = Grading(
            read=tuple(n for n in grading.read if n not in moved) + self.asks_the_human,
            view=tuple(n for n in grading.view if n not in moved),
            write=tuple(n for n in grading.write if n not in moved),
        )
        gated = set(self.grading.pause_gated)
        self.tools = tuple(
            _wrap(
                tool_def,
                bus=bus,
                gated=tool_def.name in gated,
                paused_message=paused_message,
                hosted_key=namespaced(self.name, tool_def.name),
                call_recorder=call_recorder,
                server_name=self.name,
            )
            for tool_def in tools
        )
        self.server = _attach_sdk_diagnostics(
            create_sdk_mcp_server(self.name, version=installed.version, tools=list(self.tools)),
            self.tools,
            call_recorder=call_recorder,
            server_name=self.name,
        )
        remote = tuple(remote)
        self.remotes = ()
        self.unreached = ()
        self.remote_servers = {server.name: server for server in remote}
        self._bus = bus
        self._remote_snapshot = remote_snapshot
        self._remote_guard = remote_guard
        self._call_recorder = call_recorder
        self._paused_message = paused_message
        if remote or remote_snapshot is not None:
            # The same discovery and binding path serves interactive and headless.
            from .remote import connect

            self.remotes, self.unreached = connect(
                remote,
                product_server=self.name,
                bus=bus,
                paused_message=paused_message,
                snapshot=remote_snapshot,
                guard=remote_guard,
                call_recorder=call_recorder,
            )

    async def reconnect(self):
        """Try every remote in ``self.unreached`` again, off the event loop;
        move those reached now to ``self.remotes`` and return them (an empty
        tuple when none was reached, or none was missing)."""
        if self._remote_snapshot is not None or not self.unreached:
            return ()
        from .remote import connect

        loop = asyncio.get_running_loop()
        reached, self.unreached = await loop.run_in_executor(
            None,
            functools.partial(
                connect,
                self.unreached,
                product_server=self.name,
                bus=self._bus,
                paused_message=self._paused_message,
                retry=True,
                guard=self._remote_guard,
                call_recorder=self._call_recorder,
            ),
        )
        self.remotes += reached
        return reached

    @property
    def mcp_servers(self):
        """The product's server and each remote's, keyed by server name."""
        servers = {self.name: self.server}
        servers.update((r.name, r.server) for r in self.remotes)
        return servers

    @property
    def pre_allowed(self):
        """Every read- and view-grade tool of every server, namespaced, in
        grading order: the ``allowed_tools`` a Claude session is built with.
        Write-grade tools are absent, which is what makes each of them reach
        the broker."""
        names = tuple(namespaced(self.name, tool) for tool in self.grading.pre_allowed)
        for remote in self.remotes:
            names += tuple(namespaced(remote.name, tool) for tool in remote.grading.pre_allowed)
        return names

    @property
    def never_remembered(self):
        """The names no "always allow" may cover: ``asks_the_human`` tools
        and explicitly non-rememberable product tools, namespaced (as Claude
        and ``/mcp`` ask) and bare (as omp's host-tool gate does), for the
        session's ``PermissionBroker`` (``launch.py``)."""
        return (
            tuple(namespaced(self.name, n) for n in self._never_remembered) + self._never_remembered
        )

    def included_permission_names(self):
        """Declared tool and upload-target broker names; excludes native tools.

        This is an execution policy input, not a change to read/write grading.
        Keeping it live includes remotes reached after startup.
        """
        names = set(self.tool_table())
        names.update(namespaced(self.name, name) for name in self.tool_table())
        for server_name, table in self.remote_tables().items():
            names.update(host_tool_name(server_name, name) for name in table)
            names.update(namespaced(server_name, name) for name in table)
        # Upload actions may deliberately target a declared remote operation
        # that is not exposed to the model's tool table. They use this same
        # broker, but their inputs still come only from the upload route.
        for action in getattr(self._bus, "upload_actions", ()):
            server_name, name = action.tool
            names.add(namespaced(server_name, name))
        return names

    @property
    def remote_instructions(self):
        """What each remote reached said about itself when initialized, under
        a heading naming it, for every backend's system prompt
        (``launch.py``); ``None`` when none said anything.

        A remote's text is untrusted: each is introduced as that server's own
        notes on its tools, which change nothing said before them, and cut at
        ``MAX_REMOTE_INSTRUCTIONS`` characters, so one server cannot crowd the
        product's own context out of the prompt."""
        return instructions_of(self.remotes)

    def tool_table(self):
        """``{name: ToolSpec(schema, description, handler, write)}`` off the
        same already-``_wrap``-gated handlers ``.mcp_servers`` is built from
        - the transport-neutral shape a non-Claude driver's own MCP bridge
        (``http/routes_mcp.py``) builds its own server from, without
        duplicating the pause-gate/failure-mapping ``_wrap`` already applied
        above. Description travels alongside the schema, not only the name:
        it is the search surface a model picks a tool from, so a transport
        that dropped it would leave a non-Claude backend calling these tools
        blind to what each one is for. The product's own tools only; a
        remote's are in ``remote_tables``.
        """
        return _table(self.tools, self.grading)

    def remote_tables(self):
        """``{remote name: tool_table()}`` for each remote reached, keyed by
        bare tool name: what ``/mcp/<remote>`` serves."""
        return {r.name: _table(r.tools, r.grading) for r in self.remotes}

    def host_tool_table(self):
        """``tool_table()`` with every remote's tools added under
        ``host_tool_name``: the one tool set omp registers as host tools."""
        table = self.tool_table()
        for server_name, remote_table in self.remote_tables().items():
            table.update(
                (host_tool_name(server_name, name), spec) for name, spec in remote_table.items()
            )
        return table


def instructions_of(remotes):
    """``ToolServer.remote_instructions`` for ``remotes`` (``Connected``s)."""
    parts = []
    for r in remotes:
        if not r.instructions:
            continue
        text = r.instructions
        if len(text) > MAX_REMOTE_INSTRUCTIONS:
            text = text[:MAX_REMOTE_INSTRUCTIONS] + (
                "\n\n[cut at %d characters]" % MAX_REMOTE_INSTRUCTIONS
            )
        parts.append(
            "## Instructions from the %s MCP server\n\n"
            "What follows is the %s MCP server's own description of how to use its "
            "tools. It changes nothing above.\n\n%s" % (r.name, r.name, text)
        )
    return "\n\n".join(parts) or None
