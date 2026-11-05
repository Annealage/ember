"""Tests for ``codex_direct.request``: items, body and headers are exact."""

import base64
import hashlib
import json

import pytest
from codex_direct_fakes import GIF, JPEG, PNG, TOKEN, WEBP

from annealage_ember.codex_direct import ClientIdentity, CodexRequest, image_item, text_item
from annealage_ember.codex_direct.request import (
    build_headers,
    redact_images,
    sniff_image_mime,
    validate_extra_headers,
)

IDENTITY = ClientIdentity("test-client", "9.9.9")


def uri(mime, raw):
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def test_text_item_is_one_user_message_with_one_input_text_part():
    assert text_item("hi") == {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}


def test_text_item_rejects_non_text():
    with pytest.raises(TypeError):
        text_item(b"hi")


@pytest.mark.parametrize(
    ("raw", "mime"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (GIF, "image/gif")],
)
def test_image_item_embeds_the_bytes_unchanged_with_the_sniffed_mime(raw, mime):
    item = image_item(raw, detail="original")
    assert item == {
        "role": "user",
        "content": [{"type": "input_image", "detail": "original", "image_url": uri(mime, raw)}],
    }
    # Decoding the data URI returns the very bytes given: nothing was re-encoded.
    payload = item["content"][0]["image_url"].split(",", 1)[1]
    assert base64.b64decode(payload) == raw


def test_image_item_reads_a_path_to_the_same_item_as_the_bytes(tmp_path):
    path = tmp_path / "x.png"
    path.write_bytes(PNG)
    assert image_item(path, detail="low") == image_item(PNG, detail="low")
    assert image_item(str(path), detail="low") == image_item(PNG, detail="low")


def test_image_item_passes_detail_through_untouched():
    for detail in ("original", "high", "low", "auto", "something-new"):
        assert image_item(PNG, detail=detail)["content"][0]["detail"] == detail


def test_image_item_refuses_unknown_formats_and_bad_arguments():
    with pytest.raises(ValueError):
        image_item(b"not an image at all", detail="high")
    with pytest.raises(ValueError):
        image_item(PNG, detail="")
    with pytest.raises(TypeError):
        image_item(12, detail="high")
    with pytest.raises(TypeError):
        image_item(PNG)  # detail is keyword-only and required


def test_sniff_requires_the_webp_form_tag_not_just_riff():
    with pytest.raises(ValueError):
        sniff_image_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ")


def test_body_has_the_reference_field_order_and_no_sampling_fields():
    items = [image_item(PNG, detail="original"), text_item("context"), text_item("question")]
    request = CodexRequest(
        "gpt-x",
        "You read schematics.",
        items,
        effort="low",
        include=["reasoning.encrypted_content"],
        prompt_cache_key="cache-1",
    )
    body = request.body()
    assert list(body) == [
        "model",
        "instructions",
        "input",
        "store",
        "stream",
        "reasoning",
        "include",
        "prompt_cache_key",
    ]
    assert body["store"] is False and body["stream"] is True
    assert body["reasoning"] == {"effort": "low"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert not {"temperature", "top_p", "max_output_tokens", "max_tokens"} & set(body)
    assert request.body_bytes() == json.dumps(body).encode("utf-8")


def test_optional_fields_are_omitted_when_unset():
    body = CodexRequest("m", "i", [text_item("q")]).body()
    assert list(body) == ["model", "instructions", "input", "store", "stream"]


def test_input_order_and_content_are_exactly_what_the_caller_gave():
    items = [text_item("b"), image_item(PNG, detail="high"), text_item("a"), text_item("b")]
    request = CodexRequest("m", "i", items)
    assert request.body()["input"] == items
    # The request owns its list: later edits by the caller do not change what is sent.
    sent = request.body_bytes()
    items.reverse()
    assert request.body_bytes() == sent


def test_request_rejects_empty_or_malformed_input():
    with pytest.raises(ValueError):
        CodexRequest("m", "i", [])
    with pytest.raises(ValueError):
        CodexRequest("m", "i", ["not a dict"])
    with pytest.raises(ValueError):
        CodexRequest("", "i", [text_item("q")])


def test_client_identity_has_no_defaults():
    with pytest.raises(TypeError):
        ClientIdentity()
    with pytest.raises(TypeError):
        ClientIdentity("only-originator")
    with pytest.raises(ValueError):
        ClientIdentity("", "1")
    with pytest.raises(ValueError):
        ClientIdentity("bad\nname", "1")


def headers(**overrides):
    args = {
        "access_token": TOKEN,
        "account_id": "acct-1",
        "identity": IDENTITY,
        "attempt_id": "attempt-1",
        "session_id": "sess-1",
        "thread_id": "thread-1",
    }
    args.update(overrides)
    return build_headers(**args)


def test_headers_are_exactly_the_documented_set():
    assert headers() == {
        "Authorization": "Bearer " + TOKEN,
        "chatgpt-account-id": "acct-1",
        "OpenAI-Beta": "responses=experimental",
        "originator": "test-client",
        "version": "9.9.9",
        "session-id": "sess-1",
        "thread-id": "thread-1",
        "x-client-request-id": "attempt-1",
        "accept": "text/event-stream",
        "content-type": "application/json",
        "accept-encoding": "identity",
    }


def test_extra_headers_are_appended():
    merged = headers(extra_headers={"x-codex-routing-hint": "model=m"})
    assert merged["x-codex-routing-hint"] == "model=m"
    assert merged["Authorization"] == "Bearer " + TOKEN


@pytest.mark.parametrize(
    "name",
    ["authorization", "Authorization", "CHATGPT-ACCOUNT-ID", "Accept-Encoding", "content-length",
     "host", "session-id", "x-client-request-id", "originator", "Transfer-Encoding"],
)  # fmt: skip
def test_extra_headers_cannot_override_client_owned_ones(name):
    with pytest.raises(ValueError):
        validate_extra_headers({name: "x"})


def test_invalid_header_values_are_refused_without_echoing_them():
    secret = "line1\r\nInjected: " + TOKEN
    with pytest.raises(ValueError) as caught:
        validate_extra_headers({"x-thing": secret})
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    with pytest.raises(ValueError) as caught:
        headers(access_token="bad\ntoken-" + TOKEN)
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    with pytest.raises(ValueError):
        headers(account_id="")


def test_redact_images_replaces_data_uris_with_mime_bytes_and_digest():
    items = [text_item("q"), image_item(PNG, detail="original"), image_item(JPEG, detail="high")]
    body = CodexRequest("m", "i", items).body()
    redacted, images = redact_images(body)
    first = redacted["input"][1]["content"][0]
    assert first == {
        "type": "input_image",
        "detail": "original",
        "image_url": {
            "mime_type": "image/png",
            "bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest(),
        },
    }
    assert redacted["input"][2]["content"][0]["image_url"]["mime_type"] == "image/jpeg"
    assert [raw for _, raw in images] == [PNG, JPEG]
    # Text and order survive; the input body was not modified.
    assert redacted["input"][0] == text_item("q")
    assert body["input"][1]["content"][0]["image_url"].startswith("data:image/png;base64,")
    assert base64.b64encode(PNG).decode("ascii") not in json.dumps(redacted)


def test_redact_images_leaves_remote_urls_and_never_copies_undecodable_data():
    remote = {"role": "user", "content": [{"type": "input_image", "image_url": "https://x/y.png"}]}
    broken = {
        "role": "user",
        "content": [{"type": "input_image", "image_url": "data:image/png;base64,@@@"}],
    }
    redacted, images = redact_images({"input": [remote, broken]})
    assert redacted["input"][0] == remote
    meta = redacted["input"][1]["content"][0]["image_url"]
    assert meta["undecodable"] is True and "@@@" not in json.dumps(redacted)
    assert images[0][1] is None
