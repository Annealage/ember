"""A hosted run's omp profile: the platform's one mounted omp auth store, and nothing else.

A hosted worker is given two mounts and names them in its environment:

``ANNEALAGE_OMP_AUTH_FILE``
    omp's auth store (``agent.db``: provider logins, OAuth and API keys) in an
    omp auth directory mounted read-write, e.g. ``/annealage/omp-auth/agent.db``.
``ANNEALAGE_USAGE_DIR``
    a directory the worker may write, e.g. ``/annealage/usage``. It is omp's
    ``--session-dir``, so the omp session JSONL (one record per provider
    request) lands there and the platform reads it after the job. Ember writes no
    usage file of its own.

Both names are reserved. ``hosted_profile`` builds one throwaway omp agent
directory per run holding only a symlink to that auth file (a refreshed token is
refreshed once for everyone; a symlink cannot make the target read-only, so the
mount does that), and yields it with the session directory::

    with hosted_profile(parent=scratch) as profile:
        await run_prompt(prompt, omp_agent_dir=profile.agent_dir,
                         omp_session_dir=profile.session_dir, ...)
    # or launch.build_session(..., omp_agent_dir=profile.agent_dir,
    #                         omp_session_dir=profile.session_dir)

The model is the caller's ``provider/model`` reference against the providers the
auth store holds; there is no ``models.yml``, proxy URL or token variable.

Every other credential source is refused with ``HostedProfileError`` rather
than ignored: an auth file outside the mount, a relative, missing or non-file
path, a symlink out of the mount, the retired proxy token, and the well-known
provider key variables in the environment (``AMBIENT_CREDENTIAL_ENV``; omp would
use them, so a worker must not carry them). The list is the known names, not a
proof that nothing else in the environment is a credential.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional, Union

#: Environment variable naming the mounted omp auth file.
AUTH_FILE_ENV = "ANNEALAGE_OMP_AUTH_FILE"
#: Environment variable naming the directory omp keeps its session JSONL in.
USAGE_DIR_ENV = "ANNEALAGE_USAGE_DIR"
#: omp's auth store is this file in its agent directory.
AUTH_FILE_NAME = "agent.db"
#: Where the platform mounts omp's auth directory; the auth file must resolve inside it.
AUTH_MOUNT = Path("/annealage/omp-auth")
#: Credential environment variables omp would pick up; none may be present in a hosted worker.
#: ``ANNEALAGE_INFERENCE_TOKEN`` is the retired proxy token.
AMBIENT_CREDENTIAL_ENV = (
    "ANNEALAGE_INFERENCE_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "CEREBRAS_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "PI_CODING_AGENT_DIR",
)


class HostedProfileError(ValueError):
    """The worker's environment or mounts do not give a hosted run its one credential source."""


@dataclass(frozen=True)
class HostedProfile:
    """What a run passes to omp: its throwaway agent directory and the session directory."""

    agent_dir: Path
    session_dir: Path


def refuse_ambient_credentials(env: Optional[Mapping[str, str]] = None) -> None:
    """Raise when ``env`` (default ``os.environ``) holds a credential variable omp would use."""
    present = [name for name in AMBIENT_CREDENTIAL_ENV if (os.environ if env is None else env).get(name)]
    if present:
        raise HostedProfileError("a hosted worker must not carry credentials in its environment: " + ", ".join(present))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def auth_file_from_env(env: Optional[Mapping[str, str]] = None, *, mount: Union[str, Path] = AUTH_MOUNT) -> Path:
    """The mounted auth file named by ``ANNEALAGE_OMP_AUTH_FILE``, resolved; ``HostedProfileError`` unless it is
    set, absolute, an existing regular file, and (symlinks resolved) inside ``mount``."""
    value = (os.environ if env is None else env).get(AUTH_FILE_ENV, "").strip()
    if not value:
        raise HostedProfileError(f"{AUTH_FILE_ENV} is not set; a hosted run has no other credential source")
    path = Path(value)
    if not path.is_absolute():
        raise HostedProfileError(f"{AUTH_FILE_ENV} must be an absolute path")
    if path.name != AUTH_FILE_NAME:
        raise HostedProfileError(f"{AUTH_FILE_ENV} must name {AUTH_FILE_NAME}")
    root = Path(mount).resolve()
    resolved = path.resolve()
    if not _inside(resolved, root):
        raise HostedProfileError(f"{AUTH_FILE_ENV} is outside the omp auth mount {root}")
    if not resolved.is_file():
        raise HostedProfileError(f"{AUTH_FILE_ENV} is not an existing file")
    return resolved


def usage_dir_from_env(env: Optional[Mapping[str, str]] = None) -> Path:
    """The existing directory named by ``ANNEALAGE_USAGE_DIR``, for omp's ``--session-dir``."""
    value = (os.environ if env is None else env).get(USAGE_DIR_ENV, "").strip()
    if not value:
        raise HostedProfileError(f"{USAGE_DIR_ENV} is not set")
    path = Path(value)
    if not path.is_absolute():
        raise HostedProfileError(f"{USAGE_DIR_ENV} must be an absolute path")
    if not path.is_dir():
        raise HostedProfileError(f"{USAGE_DIR_ENV} is not an existing directory")
    return path.resolve()


@contextlib.contextmanager
def hosted_profile(
    env: Optional[Mapping[str, str]] = None,
    *,
    mount: Union[str, Path] = AUTH_MOUNT,
    parent: Optional[Path] = None,
    prefix: str = "omp-agent-",
) -> Iterator[HostedProfile]:
    """Yield a fresh temp omp agent directory holding only the link to the mounted auth file, with the usage
    directory; remove the directory (never the link's target) on exit, normally or not. ``env`` defaults to
    ``os.environ``; ``mount`` to ``AUTH_MOUNT``; ``parent`` is where the directory is created (a run's scratch)."""
    refuse_ambient_credentials(env)
    auth = auth_file_from_env(env, mount=mount)
    session_dir = usage_dir_from_env(env)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as directory:
        agent_dir = Path(directory)
        (agent_dir / AUTH_FILE_NAME).symlink_to(auth)
        yield HostedProfile(agent_dir=agent_dir, session_dir=session_dir)
