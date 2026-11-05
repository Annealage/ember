"""Actionable, value-free diagnostics for nested anyOf/oneOf failures.

A failed union used to surface only as "Input does not satisfy anyOf". The
diagnostic now names the branch the input was aiming at when the schema makes
that unambiguous, lists the alternatives side by side when it does not, and
never echoes the candidate's values.
"""

from annealage_ember.tools import _compile_schema, _input_diagnostic

SECRET = "sk-live-do-not-echo"

DOC = {
    "type": "object",
    "properties": {
        "kind": {"const": "doc"},
        "document_id": {"type": "string"},
        "page": {"type": "integer"},
    },
    "required": ["kind", "document_id", "page"],
}
NET = {
    "type": "object",
    "properties": {"kind": {"const": "net"}, "net": {"type": "string"}},
    "required": ["kind", "net"],
}


def _diagnose(schema, args):
    validator, names = _compile_schema(schema)
    return _input_diagnostic(validator, names, args)


def _wrap(union):
    return {
        "type": "object",
        "properties": {"source": union, "ref": {"type": "string"}},
        "required": ["source"],
    }


def test_a_discriminated_union_reports_the_matching_branch_only():
    schema = _wrap({"anyOf": [DOC, NET]})
    diagnostic = _diagnose(schema, {"source": {"kind": "doc", "document_id": SECRET}})
    assert [(i["location"], i["code"]) for i in diagnostic["issues"]] == [(["source"], "required")]
    assert "page" in diagnostic["issues"][0]["message"]
    assert "net" not in diagnostic["issues"][0]["message"]
    assert SECRET not in str(diagnostic)


def test_oneof_with_a_discriminator_keyword_behaves_the_same():
    schema = _wrap({"oneOf": [DOC, NET]})
    diagnostic = _diagnose(schema, {"source": {"kind": "net"}})
    assert [i["code"] for i in diagnostic["issues"]] == ["required"]
    assert "net" in diagnostic["issues"][0]["message"]


def test_an_ambiguous_union_lists_alternatives_and_chooses_none():
    first = {"type": "object", "required": ["a"], "properties": {"a": {}}}
    second = {"type": "object", "required": ["b"], "properties": {"b": {}}}
    diagnostic = _diagnose(_wrap({"anyOf": [first, second]}), {"source": {}})
    [issue] = diagnostic["issues"]
    assert issue["location"] == ["source"]
    assert issue["code"] == "anyOf"
    assert "alternative 1 requires a" in issue["message"]
    assert "alternative 2 requires b" in issue["message"]


def test_a_union_no_branch_can_describe_stays_generic():
    schema = _wrap({"anyOf": [{"type": "string"}, {"type": "integer"}]})
    diagnostic = _diagnose(schema, {"source": {"x": SECRET}})
    [issue] = diagnostic["issues"]
    assert issue["code"] == "anyOf"
    assert issue["message"] == "Input does not satisfy anyOf"
    assert SECRET not in str(diagnostic)


def test_a_wrong_discriminator_value_does_not_pick_a_branch():
    schema = _wrap({"anyOf": [DOC, NET]})
    diagnostic = _diagnose(schema, {"source": {"kind": "other"}})
    [issue] = diagnostic["issues"]
    assert issue["code"] == "anyOf"
    assert issue["message"] == "Input does not satisfy anyOf"


def test_identical_issues_are_reported_once():
    schema = {
        "allOf": [
            {"type": "object", "required": ["a"], "properties": {"a": {}}},
            {"type": "object", "required": ["a"], "properties": {"a": {}}},
        ]
    }
    diagnostic = _diagnose(schema, {})
    assert len(diagnostic["issues"]) == 1


def test_the_issue_bound_still_holds_with_expanded_unions():
    branch = {
        "type": "object",
        "properties": {"kind": {"const": "doc"}, **{"f%d" % n: {} for n in range(40)}},
        "required": ["kind"] + ["f%d" % n for n in range(40)],
    }
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"anyOf": [branch, NET]}}},
    }
    args = {"items": [{"kind": "doc"} for _ in range(40)]}
    diagnostic = _diagnose(schema, args)
    assert len(diagnostic["issues"]) <= 16


def _closed(**fields):
    return {
        "type": "object",
        "properties": dict(fields),
        "required": list(fields),
        "additionalProperties": False,
    }


EVIDENCE_SOURCES = [
    _closed(document_id={"type": "string"}, page={"type": "integer", "minimum": 1}),
    _closed(artifact={"type": "string"}, pointer={"type": "string"}),
    _closed(
        artifact={"type": "string"}, start_line={"type": "integer"}, end_line={"type": "integer"}
    ),
    _closed(artifact={"type": "string"}, image={"const": True}),
    _closed(calculation_id={"type": "string"}),
]
EVIDENCE = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "ref": {"type": "string"},
        "source": {"anyOf": EVIDENCE_SOURCES},
    },
    "required": ["type", "ref", "source"],
    "additionalProperties": False,
}


def test_an_undiscriminated_five_way_union_names_the_only_branch_the_keys_fit():
    args = {"type": "datasheet", "ref": "U1", "source": {"document_id": "a" * 64}}
    diagnostic = _diagnose(EVIDENCE, args)
    [issue] = diagnostic["issues"]
    assert issue["location"] == ["source"]
    assert issue["code"] == "required"
    assert "page" in issue["message"]
    assert "artifact" not in issue["message"]
    assert "a" * 64 not in str(diagnostic)
    assert diagnostic["truncated"] == 0


def test_a_five_way_union_shared_key_lists_only_the_alternatives_that_fit():
    args = {"type": "datasheet", "ref": "U1", "source": {"artifact": SECRET}}
    diagnostic = _diagnose(EVIDENCE, args)
    [issue] = diagnostic["issues"]
    assert issue["code"] == "anyOf"
    assert issue["message"].count("; alternative") == 3
    assert "pointer" in issue["message"] and "start_line" in issue["message"]
    assert "calculation_id" not in issue["message"] and "document_id" not in issue["message"]
    assert SECRET not in str(diagnostic)


def test_an_empty_five_way_union_stays_generic_and_admits_the_omission():
    diagnostic = _diagnose(EVIDENCE, {"type": "datasheet", "ref": "U1", "source": {}})
    [issue] = diagnostic["issues"]
    assert issue["message"] == "Input does not satisfy anyOf"
    assert diagnostic["truncated"] >= 1


def test_alternatives_list_each_required_name_once_in_schema_order():
    first = {"type": "object", "required": ["a", "b"], "properties": {"a": {}, "b": {}}}
    second = {"type": "object", "required": ["c", "d"], "properties": {"c": {}, "d": {}}}
    [issue] = _diagnose(_wrap({"anyOf": [first, second]}), {"source": {}})["issues"]
    assert "alternative 1 requires a, b" in issue["message"]
    assert "alternative 2 requires c, d" in issue["message"]


def test_a_sole_branch_with_more_issues_than_the_cap_reports_the_omission():
    names = ["f%d" % n for n in range(20)]
    branch = {
        "type": "object",
        "properties": {"kind": {"const": "doc"}, **{n: {"type": "integer"} for n in names}},
    }
    schema = _wrap({"anyOf": [branch, NET]})
    args = {"source": {"kind": "doc", **dict.fromkeys(names, "x")}}
    diagnostic = _diagnose(schema, args)
    assert len(diagnostic["issues"]) == 16
    assert diagnostic["truncated"] >= 4


def test_a_union_nested_beyond_the_expansion_depth_admits_the_omission():
    schema = {"anyOf": [DOC, NET]}
    for _ in range(4):
        schema = {"type": "object", "properties": {"x": {"anyOf": [schema, {"type": "null"}]}}}
    leaf = {"kind": "doc"}
    args = {"x": {"x": {"x": {"x": leaf}}}}
    diagnostic = _diagnose(schema, args)
    assert diagnostic["issues"]
    assert diagnostic["truncated"] >= 1


def test_format_is_an_annotation_that_is_not_enforced():
    """Datum publishes ``format: uuid`` on job ids; the server checks them, Ember passes them on."""
    schema = {
        "type": "object",
        "properties": {"job_id": {"type": "string", "format": "uuid"}},
        "required": ["job_id"],
    }
    validator, _ = _compile_schema(schema)
    assert validator.is_valid({"job_id": "not-a-uuid"})
    assert not validator.is_valid({"job_id": 3})


def test_other_content_keywords_are_still_refused():
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string", "contentEncoding": "base64"}},
    }
    try:
        _compile_schema(schema)
    except ValueError as exc:
        assert "contentEncoding" in str(exc)
    else:
        raise AssertionError("contentEncoding was accepted")
