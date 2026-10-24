import json
import os

import pytest

from annealage_ember.hosted_profile import hosted_profile, models_document, proxy_model


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
