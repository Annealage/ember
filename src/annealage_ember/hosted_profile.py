"""A hosted run's omp inference profile: proxy-only, per run, never the operator's.

A hosted product gives each model run one throwaway omp agent directory whose
only content is a ``models.yml`` naming the platform's inference proxy as the
sole provider. Nothing is read from, or written to, any standalone or operator
profile (no ``~/.omp``, no provider login): omp's agent directory is the temp
one, so its auth store, settings and model list are the run's own.

``apiKey`` is the *name* of an environment variable (omp resolves it from the
worker's environment), never the secret itself, and every request carries an
``X-Annealage-Usage-Label`` header so the proxy can attribute spend.

Use::

    with hosted_profile(url, usage_label="trace", parent=scratch) as agent_dir:
        await run_prompt(prompt, omp_agent_dir=agent_dir, ...)

    model = proxy_model("annealage", models, "review")   # "annealage/<alias>"
"""
from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from typing import Iterator, Mapping, Optional

#: Default provider id the generated ``models.yml`` registers the proxy under.
DEFAULT_PROVIDER = "annealage"
#: Default name of the environment variable holding the per-job proxy token.
DEFAULT_TOKEN_ENV = "ANNEALAGE_INFERENCE_TOKEN"
USAGE_LABEL_HEADER = "X-Annealage-Usage-Label"


def models_document(
    inference_url: str,
    *,
    usage_label: str,
    provider: str = DEFAULT_PROVIDER,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> str:
    """The ``models.yml`` text (JSON, which omp reads as YAML) for the proxy."""
    entry = {
        "baseUrl": f"{inference_url}/v1",
        "api": "openai-completions",
        "apiKey": token_env,
        "headers": {USAGE_LABEL_HEADER: usage_label},
        "discovery": {"type": "proxy"},
    }
    return json.dumps({"providers": {provider: entry}})


@contextlib.contextmanager
def hosted_profile(
    inference_url: str,
    *,
    usage_label: str,
    provider: str = DEFAULT_PROVIDER,
    token_env: str = DEFAULT_TOKEN_ENV,
    parent: Optional[Path] = None,
    prefix: str = "omp-agent-",
) -> Iterator[Path]:
    """Yield a fresh temp omp agent directory holding only the proxy
    ``models.yml``; remove it on exit, normally or not. ``parent`` is where it
    is created (a run's scratch dir)."""
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as directory:
        agent_dir = Path(directory)
        (agent_dir / "models.yml").write_text(
            models_document(
                inference_url,
                usage_label=usage_label,
                provider=provider,
                token_env=token_env,
            )
        )
        yield agent_dir


def proxy_model(
    provider: str, models: Optional[Mapping[str, str]], tier: str
) -> str:
    """``<provider>/<alias>`` for a tier from the ``ANNEALAGE_INFERENCE_MODELS``
    map; ``KeyError`` naming the tier when the map has no alias for it."""
    alias = (models or {}).get(tier)
    if not alias:
        raise KeyError(tier)
    return f"{provider}/{alias}"
