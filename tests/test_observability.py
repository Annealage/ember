"""The shared metrics registry: its definitions cannot conflict or leak identities, and
``/metrics`` is protected, once per server, labelled by route template."""

import pytest
from conftest import DEFAULT_PORT, TEST_HOST, create_toy_app, make_test_client

from annealage_ember import observability

pytestmark = pytest.mark.asyncio

TOKEN = "browser-token-for-metrics_123"
AGENT_TOKEN = "agent-token-for-metrics_456"


def test_the_same_definition_is_the_same_metric_and_a_different_one_is_refused():
    first = observability.counter("annealage_test_widgets_total", "Widgets", ("outcome",))
    assert observability.counter("annealage_test_widgets_total", "Widgets", ("outcome",)) is first
    for conflicting in (
        lambda: observability.gauge("annealage_test_widgets_total", "Widgets", ("outcome",)),
        lambda: observability.counter("annealage_test_widgets_total", "Other help", ("outcome",)),
        lambda: observability.counter(
            "annealage_test_widgets_total", "Widgets", ("outcome", "surface")
        ),
    ):
        with pytest.raises(ValueError):
            conflicting()


@pytest.mark.parametrize("label", ["session_id", "project", "path", "model", "user", "payload"])
def test_a_label_that_would_be_an_identity_is_refused(label):
    with pytest.raises(ValueError):
        observability.counter("annealage_test_bad_total", "Bad", (label,))


def test_a_name_outside_the_namespace_is_refused_and_unknown_values_are_clamped():
    with pytest.raises(ValueError):
        observability.gauge("widgets", "No prefix")
    assert (
        observability.bounded("message_update", observability.RPC_EVENT_TYPES) == "message_update"
    )
    assert observability.bounded("/p/secret-id/x", observability.RPC_EVENT_TYPES) == "other"


def _app(served_dir, **kwargs):
    return create_toy_app(
        served_dir,
        token=TOKEN,
        agent_token=AGENT_TOKEN,
        host=TEST_HOST,
        port=DEFAULT_PORT,
        **kwargs,
    )


async def test_metrics_open_only_to_the_browser_credential_never_the_agent_token(served_dir):
    client = make_test_client(_app(served_dir))
    for query in ("", "?t=" + AGENT_TOKEN, "?t=wrong"):
        assert (await client.get("/metrics" + query)).status_code == 403
    res = await client.get("/metrics?t=" + TOKEN)
    assert res.status_code == 200
    assert b"annealage_process_threads" in res.body and b"process_resident_memory_bytes" in res.body


async def test_a_short_metrics_token_is_refused_at_mount(served_dir):
    with pytest.raises(ValueError):
        _app(served_dir, metrics_token="short")


async def test_the_environment_supplies_the_metrics_token(served_dir, monkeypatch):
    monkeypatch.setenv("ANNEALAGE_METRICS_TOKEN", "e" * 40)
    client = make_test_client(_app(served_dir))
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer " + "e" * 40})
    ).status_code == 200
    assert (await client.get("/metrics?t=" + TOKEN)).status_code == 403


async def test_a_dedicated_metrics_token_opens_metrics_and_nothing_else(served_dir):
    client = make_test_client(
        _app(served_dir, metrics_token="scrape-only-token-0123456789-abcdefghijkl")
    )
    ok = await client.get(
        "/metrics", headers={"Authorization": "Bearer scrape-only-token-0123456789-abcdefghijkl"}
    )
    assert ok.status_code == 200
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer nope"})
    ).status_code == 403
    assert (await client.get("/metrics?t=" + TOKEN)).status_code == 403
    assert (
        await client.get(
            "/agent/logs",
            headers={"Authorization": "Bearer scrape-only-token-0123456789-abcdefghijkl"},
        )
    ).status_code == 403


async def test_request_timing_uses_the_route_template_and_an_unmatched_path_is_one_series(
    served_dir,
):
    client = make_test_client(_app(served_dir))
    await client.get("/agent/logs/some-name?t=" + TOKEN)
    await client.get("/no-such-thing-" + "a1b2c3")
    body = (await client.get("/metrics?t=" + TOKEN)).body.decode()
    assert 'route="/agent/logs/<path:name>"' in body
    assert 'annealage_http_unmatched_total{method="GET",status_class="4xx"}' in body
    assert "a1b2c3" not in body and "some-name" not in body


async def test_metrics_can_be_left_off_and_a_second_mount_is_a_no_op(served_dir):
    client = make_test_client(_app(served_dir, metrics=False))
    assert (await client.get("/metrics?t=" + TOKEN)).status_code == 404
    app = _app(served_dir)
    before = len(app.url_map)
    observability.mount(app, auth=app.agent_auth)
    assert len(app.url_map) == before
