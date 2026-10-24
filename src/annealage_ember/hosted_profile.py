"""A hosted run's omp inference profile: the platform's providers only, per run, never the operator's.

A hosted product gives each model run one throwaway omp agent directory whose
content is a ``models.yml`` naming the platform's inference provider(s), plus,
only when the operator configures it, a symlink to the operator's omp provider
auth file. Nothing else is read from, or written to, any standalone or operator
profile (no ``~/.omp`` settings, sessions or history): omp's agent directory is
the temp one, so its settings, model list and sessions are the run's own.

``apiKey`` is the *name* of an environment variable (omp resolves it from the
worker's environment), never the secret itself, and every request carries an
``X-Annealage-Usage-Label`` header so the proxy can attribute spend.

One proxy provider (what a product uses by default)::

    with hosted_profile(url, usage_label="trace", parent=scratch) as agent_dir:
        await run_prompt(prompt, omp_agent_dir=agent_dir, ...)

    model = proxy_model("annealage", models, "review")   # "annealage/<alias>"

Several providers, each with its own base URL, API, token variable name and
tier -> alias table, and the operator's provider logins::

    providers = [
        HostedProvider("annealage", f"{url}/v1", models={"agent": "canvas-agent"}),
        HostedProvider("lab", "https://lab.example/v1", token_env="LAB_TOKEN",
                       models={"review": "big"}, discovery=False),
    ]
    with hosted_profile(providers=providers, usage_label="canvas", parent=scratch,
                        auth_file=auth_file_from_env()) as agent_dir:
        model = provider_model(providers, "review")      # "lab/big"

The auth file is the operator's omp credential store (``agent.db``, see
``DEFAULT_AUTH_FILE``): it is *linked*, not copied, so a token omp refreshes is
refreshed once for everyone. A symlink cannot make the target read-only; the
caller that needs that mounts or permissions the target itself. The link is
removed with the directory and never followed by the cleanup.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence, Union

#: Default provider id the generated ``models.yml`` registers the proxy under.
DEFAULT_PROVIDER = "annealage"
#: Default name of the environment variable holding the per-job proxy token.
DEFAULT_TOKEN_ENV = "ANNEALAGE_INFERENCE_TOKEN"
USAGE_LABEL_HEADER = "X-Annealage-Usage-Label"
#: omp's auth store (provider logins, OAuth and API keys) is this file in its agent directory.
AUTH_FILE_NAME = "agent.db"
#: The operator's omp auth file on a default install (``~/.omp/agent/agent.db``), a SQLite database in WAL mode;
#: it also holds omp's own settings and usage tables, so link it only into an operator-owned worker.
DEFAULT_AUTH_FILE = Path("~/.omp/agent") / AUTH_FILE_NAME
#: Environment variable naming the auth file to link; ``default`` selects ``DEFAULT_AUTH_FILE``; unset or empty links nothing.
AUTH_FILE_ENV = "ANNEALAGE_OMP_AUTH_FILE"


@dataclass(frozen=True)
class HostedProvider:
    """One provider the run's ``models.yml`` registers.

    ``base_url`` is exactly what omp sends to (it includes any ``/v1``). ``token_env`` is the *name* of the environment
    variable holding the bearer token; ``None`` declares the endpoint unauthenticated. ``models`` maps a product tier to
    the model alias on this provider. ``discovery`` asks omp to list the endpoint's models (a proxy's ``/v1/models``);
    without it the ``models`` aliases are registered statically. ``label`` adds the usage-label header (a platform proxy
    attributes spend by it; leave it off for a third-party endpoint).
    """

    name: str
    base_url: str
    api: str = "openai-completions"
    token_env: Optional[str] = DEFAULT_TOKEN_ENV
    models: Mapping[str, str] = field(default_factory=dict)
    discovery: bool = True
    label: bool = True

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name:
            raise ValueError("a provider needs a name without '/'")
        if not self.base_url:
            raise ValueError(f"provider {self.name!r} needs a base_url")
        if not self.discovery and not self.models:
            raise ValueError(f"provider {self.name!r} without discovery needs model aliases")

    def entry(self, usage_label: str) -> dict:
        entry: dict = {"baseUrl": self.base_url, "api": self.api}
        if self.token_env:
            entry["apiKey"] = self.token_env
        else:
            entry["auth"] = "none"
        if self.label:
            entry["headers"] = {USAGE_LABEL_HEADER: usage_label}
        if self.discovery:
            entry["discovery"] = {"type": "proxy"}
        else:
            entry["models"] = [{"id": alias, "name": alias} for alias in dict.fromkeys(self.models.values())]
        return entry


def proxy_provider(
    inference_url: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    token_env: str = DEFAULT_TOKEN_ENV,
    models: Optional[Mapping[str, str]] = None,
) -> HostedProvider:
    """The platform's inference proxy as a provider: ``<inference_url>/v1``, discovered models."""
    return HostedProvider(provider, f"{inference_url}/v1", token_env=token_env, models=models or {})


def providers_document(providers: Sequence[HostedProvider], *, usage_label: str) -> str:
    """The ``models.yml`` text (JSON, which omp reads as YAML) registering every provider."""
    if not providers:
        raise ValueError("at least one provider is required")
    names = [p.name for p in providers]
    if len(set(names)) != len(names):
        raise ValueError("provider names must be unique")
    return json.dumps({"providers": {p.name: p.entry(usage_label) for p in providers}})


def models_document(
    inference_url: str,
    *,
    usage_label: str,
    provider: str = DEFAULT_PROVIDER,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> str:
    """The ``models.yml`` text for the one proxy provider."""
    return providers_document(
        [proxy_provider(inference_url, provider=provider, token_env=token_env)], usage_label=usage_label
    )


def auth_file_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The operator's auth file to link, from ``ANNEALAGE_OMP_AUTH_FILE``: a path, ``default`` for
    ``DEFAULT_AUTH_FILE``, or ``None`` when unset or empty (nothing is linked)."""
    value = (os.environ if env is None else env).get(AUTH_FILE_ENV, "").strip()
    if not value:
        return None
    return DEFAULT_AUTH_FILE.expanduser() if value == "default" else Path(value).expanduser()


def _link_auth_file(agent_dir: Path, auth_file: Union[str, Path]) -> bool:
    """Symlink ``auth_file`` as the agent directory's auth store; ``False`` (nothing linked) when it is absent."""
    target = Path(auth_file).expanduser()
    if not target.is_absolute():
        raise ValueError("the omp auth file must be an absolute path")
    if not target.is_file():
        return False
    (agent_dir / AUTH_FILE_NAME).symlink_to(target.resolve())
    return True


@contextlib.contextmanager
def hosted_profile(
    inference_url: Optional[str] = None,
    *,
    usage_label: str,
    provider: str = DEFAULT_PROVIDER,
    token_env: str = DEFAULT_TOKEN_ENV,
    providers: Optional[Sequence[HostedProvider]] = None,
    auth_file: Union[str, Path, None] = None,
    parent: Optional[Path] = None,
    prefix: str = "omp-agent-",
) -> Iterator[Path]:
    """Yield a fresh temp omp agent directory holding the ``models.yml`` and, when ``auth_file`` names an existing
    file, the link to it; remove it on exit, normally or not. Name either the proxy by ``inference_url`` (with
    ``provider``/``token_env``) or ``providers``. ``parent`` is where it is created (a run's scratch dir)."""
    if (inference_url is None) == (providers is None):
        raise ValueError("give either inference_url or providers")
    document = (
        providers_document(providers, usage_label=usage_label)
        if providers is not None
        else models_document(inference_url, usage_label=usage_label, provider=provider, token_env=token_env)
    )
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as directory:
        agent_dir = Path(directory)
        (agent_dir / "models.yml").write_text(document)
        if auth_file is not None:
            _link_auth_file(agent_dir, auth_file)
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


def provider_model(providers: Sequence[HostedProvider], tier: str) -> str:
    """``<provider>/<alias>`` for a tier from the first provider (in order) that maps it; ``KeyError`` naming the
    tier when none does."""
    for p in providers:
        if p.models.get(tier):
            return proxy_model(p.name, p.models, tier)
    raise KeyError(tier)
