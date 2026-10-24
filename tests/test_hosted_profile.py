import json
import os

import pytest

from annealage_ember.hosted_profile import (
    AUTH_FILE_ENV,
    AUTH_FILE_NAME,
    DEFAULT_AUTH_FILE,
    HostedProvider,
    auth_file_from_env,
    hosted_profile,
    models_document,
    provider_model,
    providers_document,
    proxy_model,
    proxy_provider,
)


def test_models_document_is_proxy_only():
    doc = json.loads(models_document("http://p:1", usage_label="trace"))
    assert doc == {"providers": {"annealage": {
        "baseUrl": "http://p:1/v1", "api": "openai-completions",
        "apiKey": "ANNEALAGE_INFERENCE_TOKEN",
        "headers": {"X-Annealage-Usage-Label": "trace"},
        "discovery": {"type": "proxy"}}}}


def test_profile_is_temp_and_cleaned(tmp_path):
    with hosted_profile("http://p:1", usage_label="x", parent=tmp_path) as d:
        assert d.parent == tmp_path
        assert os.listdir(d) == ["models.yml"]
    assert not d.exists()
    with pytest.raises(RuntimeError):
        with hosted_profile("http://p:1", usage_label="x", parent=tmp_path) as d:
            raise RuntimeError
    assert not d.exists()


def test_proxy_model():
    assert proxy_model("annealage", {"a": "m"}, "a") == "annealage/m"
    with pytest.raises(KeyError):
        proxy_model("annealage", {}, "a")


# --- multiple providers and the operator's auth file -------------------------------------------------


def test_single_proxy_document_is_byte_identical():
    # The exact text a single-proxy product (Trace) has always produced.
    assert models_document("http://p:1", usage_label="trace") == (
        '{"providers": {"annealage": {"baseUrl": "http://p:1/v1", "api": "openai-completions", '
        '"apiKey": "ANNEALAGE_INFERENCE_TOKEN", "headers": {"X-Annealage-Usage-Label": "trace"}, '
        '"discovery": {"type": "proxy"}}}}'
    )
    one = providers_document([proxy_provider("http://p:1")], usage_label="trace")
    assert one == models_document("http://p:1", usage_label="trace")


def _two():
    return [
        proxy_provider("http://p:1", models={"agent": "canvas-agent"}),
        HostedProvider("lab", "https://lab.example/v1", api="openai-responses", token_env="LAB_TOKEN",
                       models={"review": "big", "fast": "big"}, discovery=False, label=False),
    ]


def test_two_providers_document():
    doc = json.loads(providers_document(_two(), usage_label="canvas"))["providers"]
    assert list(doc) == ["annealage", "lab"]
    assert doc["annealage"]["discovery"] == {"type": "proxy"}
    assert doc["lab"] == {
        "baseUrl": "https://lab.example/v1", "api": "openai-responses", "apiKey": "LAB_TOKEN",
        "models": [{"id": "big", "name": "big"}],
    }
    assert "headers" not in doc["lab"]  # no usage label to a third party


def test_unauthenticated_provider_and_validation():
    entry = HostedProvider("local", "http://h/v1", token_env=None).entry("x")
    assert entry["auth"] == "none" and "apiKey" not in entry
    with pytest.raises(ValueError):
        HostedProvider("a/b", "http://h/v1")
    with pytest.raises(ValueError):
        HostedProvider("x", "http://h/v1", discovery=False)
    with pytest.raises(ValueError):
        providers_document([], usage_label="x")
    with pytest.raises(ValueError):
        providers_document([proxy_provider("http://a"), proxy_provider("http://b")], usage_label="x")


def test_provider_model_takes_first_provider_with_the_tier():
    providers = _two()
    assert provider_model(providers, "agent") == "annealage/canvas-agent"
    assert provider_model(providers, "review") == "lab/big"
    with pytest.raises(KeyError):
        provider_model(providers, "other")


def test_profile_with_providers_is_temp_and_cleaned(tmp_path):
    with hosted_profile(providers=_two(), usage_label="canvas", parent=tmp_path) as d:
        assert os.listdir(d) == ["models.yml"]
        assert set(json.loads((d / "models.yml").read_text())["providers"]) == {"annealage", "lab"}
    assert not d.exists()
    with pytest.raises(ValueError):
        with hosted_profile(usage_label="x"):
            pass
    with pytest.raises(ValueError):
        with hosted_profile("http://p:1", providers=_two(), usage_label="x"):
            pass


def test_auth_file_present_is_linked_and_target_survives_cleanup(tmp_path):
    auth = tmp_path / "operator" / "agent.db"
    auth.parent.mkdir()
    auth.write_bytes(b"credentials")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with hosted_profile("http://p:1", usage_label="x", parent=scratch, auth_file=auth) as d:
        link = d / AUTH_FILE_NAME
        assert link.is_symlink() and link.resolve() == auth.resolve()
        assert link.read_bytes() == b"credentials"
        assert sorted(os.listdir(d)) == [AUTH_FILE_NAME, "models.yml"]
    assert not d.exists()
    assert auth.read_bytes() == b"credentials"  # cleanup removed the link, never the operator's file
    with pytest.raises(RuntimeError):
        with hosted_profile("http://p:1", usage_label="x", parent=scratch, auth_file=auth) as d:
            raise RuntimeError
    assert not d.exists() and auth.read_bytes() == b"credentials"


def test_auth_file_absent_links_nothing(tmp_path):
    with hosted_profile("http://p:1", usage_label="x", parent=tmp_path,
                        auth_file=tmp_path / "missing.db") as d:
        assert os.listdir(d) == ["models.yml"]
    with hosted_profile("http://p:1", usage_label="x", parent=tmp_path, auth_file=None) as d:
        assert os.listdir(d) == ["models.yml"]
    with pytest.raises(ValueError):
        with hosted_profile("http://p:1", usage_label="x", parent=tmp_path, auth_file="relative.db"):
            pass


def test_auth_file_from_env(monkeypatch, tmp_path):
    assert auth_file_from_env({}) is None
    assert auth_file_from_env({AUTH_FILE_ENV: "  "}) is None
    assert auth_file_from_env({AUTH_FILE_ENV: str(tmp_path / "a.db")}) == tmp_path / "a.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert auth_file_from_env({AUTH_FILE_ENV: "default"}) == tmp_path / ".omp/agent/agent.db"
    monkeypatch.setenv(AUTH_FILE_ENV, "default")
    assert auth_file_from_env() == tmp_path / ".omp/agent/agent.db"
    assert str(DEFAULT_AUTH_FILE) == "~/.omp/agent/agent.db"
