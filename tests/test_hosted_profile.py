import pytest

from annealage_ember.hosted_profile import (
    AUTH_FILE_ENV,
    MODELS_FILE_ENV,
    USAGE_DIR_ENV,
    HostedProfileError,
    hosted_profile,
)


@pytest.fixture
def mounts(tmp_path):
    mount = tmp_path / "omp-auth"
    mount.mkdir()
    (mount / "agent.db").write_bytes(b"credentials")
    usage = tmp_path / "usage"
    usage.mkdir()
    env = {AUTH_FILE_ENV: str(mount / "agent.db"), USAGE_DIR_ENV: str(usage)}
    return mount, usage, env


def test_profile_links_only_the_mounted_auth_file_and_names_the_usage_dir(tmp_path, mounts):
    mount, usage, env = mounts
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with hosted_profile(env, mount=mount, parent=scratch) as profile:
        assert [p.name for p in profile.agent_dir.iterdir()] == ["agent.db"]
        assert (profile.agent_dir / "agent.db").is_symlink()
        assert (profile.agent_dir / "agent.db").read_bytes() == b"credentials"
        assert profile.session_dir == usage.resolve()
        assert profile.agent_dir.parent == scratch
    assert not profile.agent_dir.exists()
    assert (mount / "agent.db").read_bytes() == b"credentials"  # the target survives cleanup


def test_profile_is_cleaned_when_the_body_raises(tmp_path, mounts):
    mount, _, env = mounts
    with pytest.raises(RuntimeError):
        with hosted_profile(env, mount=mount, parent=tmp_path) as profile:
            raise RuntimeError("boom")
    assert not profile.agent_dir.exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda mount, usage, env: env.pop(AUTH_FILE_ENV),
        lambda mount, usage, env: env.update({AUTH_FILE_ENV: ""}),
        lambda mount, usage, env: env.update({AUTH_FILE_ENV: "agent.db"}),
        lambda mount, usage, env: env.update({AUTH_FILE_ENV: str(mount / "missing" / "agent.db")}),
        lambda mount, usage, env: env.update({AUTH_FILE_ENV: str(mount / "other.db")}),
        lambda mount, usage, env: env.pop(USAGE_DIR_ENV),
        lambda mount, usage, env: env.update({USAGE_DIR_ENV: "usage"}),
        lambda mount, usage, env: env.update({USAGE_DIR_ENV: str(usage / "absent")}),
    ],
)
def test_missing_or_malformed_mounts_are_refused(tmp_path, mounts, change):
    mount, usage, env = mounts
    change(mount, usage, env)
    with pytest.raises(HostedProfileError):
        with hosted_profile(env, mount=mount, parent=tmp_path):
            pass


def test_an_auth_file_outside_the_mount_is_refused_even_through_a_symlink(tmp_path, mounts):
    mount, _, env = mounts
    elsewhere = tmp_path / "operator"
    elsewhere.mkdir()
    (elsewhere / "agent.db").write_bytes(b"operator")
    direct = {**env, AUTH_FILE_ENV: str(elsewhere / "agent.db")}
    with pytest.raises(HostedProfileError, match="outside"):
        with hosted_profile(direct, mount=mount, parent=tmp_path):
            pass
    (mount / "sub").mkdir()
    (mount / "sub" / "agent.db").symlink_to(elsewhere / "agent.db")
    linked = {**env, AUTH_FILE_ENV: str(mount / "sub" / "agent.db")}
    with pytest.raises(HostedProfileError, match="outside"):
        with hosted_profile(linked, mount=mount, parent=tmp_path):
            pass


@pytest.mark.parametrize(
    "name",
    ["ANNEALAGE_INFERENCE_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PI_CODING_AGENT_DIR"],
)
def test_ambient_credentials_are_refused_not_ignored(tmp_path, mounts, name):
    mount, _, env = mounts
    with pytest.raises(HostedProfileError, match=name):
        with hosted_profile({**env, name: "x"}, mount=mount, parent=tmp_path):
            pass


def test_defaults_read_the_process_environment(tmp_path, mounts, monkeypatch):
    mount, usage, env = mounts
    from annealage_ember.hosted_profile import AMBIENT_CREDENTIAL_ENV

    for name in AMBIENT_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with hosted_profile(mount=mount, parent=tmp_path) as profile:
        assert profile.session_dir == usage.resolve()


def test_a_mounted_models_file_is_linked_beside_the_auth_file(tmp_path, mounts):
    mount, usage, env = mounts
    (mount / "models.yml").write_text("providers: {}\n")
    with hosted_profile(
        {**env, MODELS_FILE_ENV: str(mount / "models.yml")}, mount=mount, parent=tmp_path
    ) as profile:
        assert sorted(p.name for p in profile.agent_dir.iterdir()) == ["agent.db", "models.yml"]
        assert (profile.agent_dir / "models.yml").read_text() == "providers: {}\n"
    assert (mount / "models.yml").exists()


def test_a_models_file_in_a_sibling_mount_needs_that_mount_named(tmp_path, mounts):
    mount, usage, env = mounts
    sibling = tmp_path / "omp-config"
    sibling.mkdir()
    (sibling / "models.yml").write_text("x")
    env = {**env, MODELS_FILE_ENV: str(sibling / "models.yml")}
    with pytest.raises(HostedProfileError):
        with hosted_profile(env, mount=mount, parent=tmp_path):
            pass
    with hosted_profile(env, mount=mount, models_mount=sibling, parent=tmp_path) as profile:
        assert (profile.agent_dir / "models.yml").is_symlink()


@pytest.mark.parametrize("where", ["relative", "misnamed", "missing", "outside", "escape"])
def test_a_bad_models_file_is_refused(tmp_path, mounts, where):
    mount, usage, env = mounts
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "models.yml").write_text("x")
    (mount / "other.yml").write_text("x")
    (mount / "link.yml").mkdir()
    (mount / "link.yml" / "models.yml").symlink_to(elsewhere / "models.yml")
    value = {
        "relative": "models.yml",
        "misnamed": str(mount / "other.yml"),
        "missing": str(mount / "models.yml"),
        "outside": str(elsewhere / "models.yml"),
        "escape": str(mount / "link.yml" / "models.yml"),
    }[where]
    with pytest.raises(HostedProfileError):
        with hosted_profile({**env, MODELS_FILE_ENV: value}, mount=mount, parent=tmp_path):
            pass
