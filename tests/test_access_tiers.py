"""Access contracts: reach, family expansion and migration preserve boundaries."""

from pathlib import Path

import pytest

from solakit.core.config import Config, Profile
from solakit.tools import Manifest, Registry, ToolError, tier_roots


def test_tier_roots_defaults_overrides_and_write_read_union(config, tmp_path):
    config.repositories = {"site": tmp_path / "site"}
    profile = Profile()
    assert tier_roots(profile, config, write=False) == [
        config.paths.data.resolve(),
        (tmp_path / "site").resolve(),
    ]
    assert tier_roots(profile, config, write=True) == []
    profile = Profile(
        workspace=[f"path:{tmp_path / 'sources'}"],
        write_workspace=[f"path:{tmp_path / 'output'}"],
        write="workspace",
    )
    assert tier_roots(profile, config, write=False) == [
        (tmp_path / "sources").resolve(),
        (tmp_path / "output").resolve(),
    ]
    assert tier_roots(profile, config, write=True) == [(tmp_path / "output").resolve()]
    profile.write = "anywhere"
    assert tier_roots(profile, config, write=False) == [Path("/")]
    profile.write = "none"
    profile.read = "none"
    assert tier_roots(profile, config, write=False) == []


def test_legacy_translation_preserves_separate_write_roots(config, tmp_path):
    values = dict(
        read_scopes=[f"path:{tmp_path / 'sources'}"],
        write_scopes=[f"path:{tmp_path / 'output'}"],
        capabilities=["git.push", "mailbox:personal:read", "mailbox:personal:reply"],
    )
    translated = Profile(**values)
    assert translated.effects == ["push", "send"]
    assert translated.resources == ["mailbox:personal"]
    assert tier_roots(translated, config, write=True) == [
        (tmp_path / "output").resolve()
    ]
    assert "read_scopes" not in translated.model_dump()
    assert Profile.model_validate(translated.model_dump()) == translated
    with pytest.raises(ValueError, match="mix legacy"):
        Profile(**values, read="anywhere")
    with pytest.raises(ValueError, match="Unmapped legacy capability: mystery"):
        Profile(capabilities=["mystery"])


def test_translation_after_defaults_and_mixed_alias_warning(caplog):
    config = Config.model_validate(
        {
            "profile_defaults": {"read_scopes": ["path:/tmp"], "capabilities": []},
            "profiles": {
                "inbox": {
                    "capabilities": [
                        "mailbox:a:read",
                        "mailbox:a:reply",
                        "mailbox:b:read",
                    ]
                }
            },
        }
    )
    profile = config.profiles["inbox"]
    assert profile.workspace == ["path:/tmp"]
    assert profile.effects == ["send"]
    assert "aliases: a, b" in caplog.text


def test_family_expansion_jobs_residents_and_reserved_namespace(config):
    config.enabled_tools = ["files", "runtime"]
    config.profiles = {
        "worker": Profile(
            tools=["runtime.status", "files.*"],
            write="workspace",
            tool_disclosure="deferred",
            tool_resident=["files.*"],
        )
    }
    registry = Registry(config)
    names = registry.tool_names("worker")
    assert names[0] == "runtime.status"
    assert names[1:] == sorted(n for n in registry.tools if n.startswith("files."))
    assert (
        registry.expand_grants(["files.read", "files.*", "files.read"], "worker")[0]
        == "files.read"
    )
    assert registry.view("worker").resident == tuple(names[1:])
    assert registry.effective_tools("worker", ["runtime.*"]) == ["runtime.status"]
    assert not any(
        n.startswith("tools.") for n in registry.expand_grants(["*"], "worker")
    )
    for pattern in ("web.f*", "f*.*", "tools.*", "typo.*"):
        with pytest.raises(ValueError):
            registry.expand_grants([pattern], "worker")
    config.profiles["worker"].tools = ["runtime.status"]
    assert registry.effective_tools("worker", ["files.*"]) == []


def test_unknown_workspace_and_effect_registration(config):
    config.profiles = {"worker": Profile(workspace=["repo:missing"])}
    with pytest.raises(ValueError, match="worker.*workspace"):
        Registry(config)
    config.profiles = {"worker": Profile()}
    registry = Registry(config)

    def action(ctx):
        return "done"

    with pytest.raises(ValueError, match="Unknown tool effect"):
        registry.register(action, Manifest("a.b", ("*",), (), None, effect="mystery"))
    with pytest.raises(ValueError, match="writes accepts only filesystem"):
        registry.register(action, Manifest("a.b", ("*",), ("git.push",), None))


@pytest.mark.parametrize(
    "module,name,effect",
    [
        ("repo_git", "repo_git.commit_push", "push"),
        ("shell", "shell.exec", "shell"),
        ("shell", "shell.run", "shell"),
        ("jobs", "jobs.create", "operate"),
        ("jobs", "jobs.run", "operate"),
        ("sola_packs.email", "email.reply", "send"),
        ("sola_packs.printer", "printer.print_text", "print"),
        ("sola_packs.journal", "journal.publish", "push"),
        ("sola_packs.blog", "blog.publish", "push"),
    ],
)
def test_effects_fail_startup_and_recheck_dispatch(config, module, name, effect):
    config.enabled_tools = [module]
    profile = Profile(tools=[name], write="workspace")
    config.profiles = {"worker": profile}
    with pytest.raises(ValueError, match=f"worker.*{name}.*effect '{effect}'"):
        Registry(config)
    profile.effects = [effect]
    registry = Registry(config)
    registry.check_access(registry.tools[name], profile)
    profile.effects.clear()
    with pytest.raises(ToolError, match=f"effect '{effect}'"):
        registry.check_access(registry.tools[name], profile)


def test_catalog_profile_toml_and_access_snapshot_are_independent(config):
    import tomllib

    config.profiles = {"worker": Profile(effects=["send"], resources=["mailbox:one"])}
    registry = Registry(config)
    catalog = registry.catalog()
    pasted = tomllib.loads(catalog["profile_toml"]["worker"])
    assert Profile(**pasted["profiles"]["worker"]) == config.profiles["worker"]
    saved = registry.access_summary("worker")
    config.profiles["worker"].effects.clear()
    assert saved["effects"] == ["send"]


def test_job_patterns_save_as_expanded_narrowing(config):
    from types import SimpleNamespace

    from solakit.core.config import Job
    from solakit.jobs import Jobs

    config.enabled_tools = ["files", "runtime"]
    config.profiles = {"worker": Profile(tools=["files.read", "runtime.status"])}
    registry = Registry(config)
    jobs = object.__new__(Jobs)
    jobs.config = config
    jobs.execution = SimpleNamespace(registry=registry)
    from datetime import UTC, datetime

    jobs.clock = lambda: datetime.now(UTC)
    job = Job(
        id="one",
        cron="0 0 * * *",
        profile="worker",
        task="Read",
        bind_session="s",
        tools=["files.*"],
        tool_resident=["files.*"],
    )
    # Residents must be a subset after expansion, even when tools narrow a family.
    with pytest.raises(ValueError, match="resident"):
        jobs.validate_job(job)
    job.tool_resident = ["files.read"]
    jobs.validate_job(job)
    assert job.tools == ["files.read"]
    assert job.tool_resident == ["files.read"]
