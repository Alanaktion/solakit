import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from solakit.config import (
    ConfigError,
    EffectiveProfile,
    HTTPSettings,
    OperatorConfig,
    load_config,
    resolve_profile,
    restrict_to_admission,
)
from solakit.records import Origin, SessionMode


@pytest.mark.parametrize(
    "origin",
    [
        "http://10.0.0.1:8382",
        "http://172.16.0.1:8382",
        "http://172.31.255.254:8382",
        "http://192.168.1.20:8382",
        "http://[fd12::1]:8382",
        "http://127.0.0.1:8382",
        "http://localhost:8382",
    ],
)
def test_explicit_lan_http_origins(origin: str) -> None:
    settings = HTTPSettings(allowed_origins=(origin,))
    assert settings.allowed_origins == (origin,)
    assert not settings.secure_cookie


@pytest.mark.parametrize(
    "origin",
    [
        "http://172.15.255.255:8382",
        "http://172.32.0.1:8382",
        "http://8.8.8.8:8382",
        "http://0.0.0.0:8382",
        "http://169.254.1.2:8382",
        "http://[2001:db8::1]:8382",
        "http://sola.local:8382",
        "http://192.168.1.0/24:8382",
        "http://*.local:8382",
        "http://user:pass@192.168.1.20:8382",
        "http://192.168.1.20:8382/path",
        "http://192.168.1.20:8382?query",
        "http://192.168.1.20:8382#fragment",
        "null",
    ],
)
def test_lan_http_does_not_allow_other_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        HTTPSettings(allowed_origins=(origin,))


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'data_dir = "data"\nloaded_tools = ["read", "write"]\n'
        '[model]\npin = "fixture-qwen"\napi_key_env = "PRIVATE_KEY_REFERENCE"\n'
        '[defaults]\nworkspace_roots = ["."]\ntools = ["read", "write", "unloaded"]\n' + extra
    )
    return path


def test_inheritance_sources_and_empty_tools(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[profiles.readonly]\ntools = ["read"]\n'
        "[profiles.readonly.budgets]\nmodel_calls = 4\n"
        '[profiles.none]\nextends = "readonly"\ntools = []\n',
    )
    config = load_config(path)
    inherited = resolve_profile(config, "readonly", base=tmp_path)
    assert inherited.tools == {"read"}
    assert inherited.workspace_roots == (tmp_path,)
    assert inherited.budgets.model_calls == 4 and inherited.budgets.tool_calls == 32
    assert dict(inherited.sources)["budgets.model_calls"] == "profiles.readonly.budgets.model_calls"
    assert not resolve_profile(config, "none", base=tmp_path).tools
    default = resolve_profile(config, base=tmp_path)
    assert default.granted_tools == {"read", "write", "unloaded"}
    assert default.tools == {"read", "write"}
    with pytest.raises(ValidationError):
        inherited.memory_mode = "off"
    assert "PRIVATE_KEY_REFERENCE" not in json.dumps(inherited.explain())
    assert "PRIVATE_KEY_REFERENCE" not in repr(inherited)


@pytest.mark.parametrize(
    "extra",
    [
        '[profiles.child]\ntools = ["extra"]\n',
        "[profiles.child.budgets]\nmodel_calls = 17\n",
        '[profiles.child]\nworkspace_roots = [".."]\n',
        '[profiles.child]\nextends = "child"\n',
        '[profiles.child]\nextends = "missing"\n',
    ],
)
def test_reject_widening_and_bad_inheritance(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ConfigError):
        resolve_profile(load_config(write_config(tmp_path, extra)), "child", base=tmp_path)


@pytest.mark.parametrize("field", ["resources", "effects"])
@pytest.mark.parametrize("table", ["defaults", "profiles.child"])
def test_removed_permission_settings_are_rejected(tmp_path: Path, table: str, field: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[{table}]\n{field} = []\n")
    with pytest.raises(ConfigError, match=field):
        load_config(path)


def test_root_narrowing_and_current_revocations(tmp_path: Path) -> None:
    subdir = tmp_path / "sub"
    subdir.mkdir()
    path = write_config(tmp_path, '[profiles.child]\nworkspace_roots = ["sub"]\ntools = ["read"]\n')
    config = load_config(path)
    admitted = resolve_profile(config, base=tmp_path)
    current = resolve_profile(config, "child", base=tmp_path)
    assert restrict_to_admission(current, admitted).workspace_roots == (subdir,)
    assert restrict_to_admission(current, admitted).tools == {"read"}
    assert restrict_to_admission(admitted, current).tools == {"read"}
    assert restrict_to_admission(admitted, current).workspace_roots == (subdir,)
    changed = current.model_dump() | {"model_pin": "another-model"}
    with pytest.raises(ConfigError):
        restrict_to_admission(EffectiveProfile.model_validate(changed), admitted)


def test_modes_and_proactive_budgets(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    for mode in (SessionMode.ISOLATED, SessionMode.EPHEMERAL):
        assert resolve_profile(config, base=tmp_path, mode=mode).memory_mode == "off"
    proactive = resolve_profile(config, base=tmp_path, origin=Origin.PROACTIVE)
    assert (
        proactive.budgets.model_calls,
        proactive.budgets.tool_calls,
        proactive.budgets.active_seconds,
    ) == (8, 12, 600)
    disabled = OperatorConfig.model_validate(
        config.model_dump()
        | {
            "defaults": {"workspace_roots": ["."], "memory_mode": "off"},
            "profiles": {"child": {"memory_mode": "optional"}},
        }
    )
    with pytest.raises(ConfigError):
        resolve_profile(disabled, "child", base=tmp_path)


@pytest.mark.parametrize(
    "contents",
    [
        'unknown = "VERY_PRIVATE_VALUE"',
        '[model]\nbase_url = "http://user:VERY_PRIVATE_VALUE@localhost/v1"',
        "[defaults.budgets]\nmodel_calls = true",
        '[defaults.budgets]\nmodel_calls = "VERY_PRIVATE_VALUE"',
        "[defaults.budgets]\nmodel_calls = 257",
        '[defaults.budgets]\nmisspelled = "VERY_PRIVATE_VALUE"',
        'loaded_tools = "VERY_PRIVATE_VALUE"',
        "schema_version = true",
        'broken = ["VERY_PRIVATE_VALUE"',
    ],
)
def test_invalid_config_is_secret_safe(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(contents)
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "VERY_PRIVATE_VALUE" not in str(error.value)


def test_empty_config_and_missing_roots(tmp_path: Path) -> None:
    path = tmp_path / "empty.toml"
    path.write_text("")
    config = load_config(path)
    with pytest.raises(ConfigError, match="workspace root"):
        resolve_profile(config)
    path.write_text('[model]\npin = "fixture"\n[defaults]\nworkspace_roots = ["missing"]')
    with pytest.raises(ConfigError, match="must exist"):
        resolve_profile(load_config(path), base=tmp_path)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "missing.toml")
    path.write_text(
        '[model]\npin = "fixture"\n[defaults]\nworkspace_roots = ["~sola_fixture_no_such_user"]'
    )
    with pytest.raises(ConfigError, match="must exist"):
        resolve_profile(load_config(path), base=tmp_path)


def test_example_matches_effective_defaults(tmp_path: Path) -> None:
    example = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
    assert example.model.pin == ""
    assert example.defaults.workspace_roots == []
    with pytest.raises(ConfigError, match="workspace root"):
        resolve_profile(example, base=tmp_path)

    # Supply only the required setup so all remaining effective defaults can be compared.
    def configured(config: OperatorConfig) -> OperatorConfig:
        values = config.model_dump()
        values["model"]["pin"] = "fixture-qwen"
        values["defaults"]["workspace_roots"] = [str(tmp_path)]
        return OperatorConfig.model_validate(values)

    for origin in Origin:
        expected = resolve_profile(configured(OperatorConfig()), base=tmp_path, origin=origin)
        actual = resolve_profile(configured(example), base=tmp_path, origin=origin)
        assert actual.model_dump(exclude={"sources"}) == expected.model_dump(exclude={"sources"})


def test_cli_explain_is_offline_and_read_only(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    script = (
        "import sys; from solakit.cli import main; "
        "sys.argv = ['sola', 'check', '--config', sys.argv[1], '--explain']; main(); "
        "assert 'fastapi' not in sys.modules; assert 'openai' not in sys.modules; "
        "assert 'solakit.runtime' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], check=True, capture_output=True, text=True
    )
    report = json.loads(result.stdout)
    assert report["runtime_configured"]
    assert report["effective_config"]["values"]["model_api_key_env"] == "<redacted>"
    for section in ("values", "sources"):
        assert not {"resources", "effects"} & report["effective_config"][section].keys()
    assert "PRIVATE_KEY_REFERENCE" not in result.stdout
    assert not (tmp_path / "data").exists()


def test_canonical_roots_and_fragment_inheritance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "alias").symlink_to(outside, target_is_directory=True)
    (tmp_path / "soul.md").write_text("Operator-owned identity")
    config = OperatorConfig.model_validate(
        {
            "model": {"pin": "fixture"},
            "defaults": {"workspace_roots": ["workspace"], "fragments": ["soul.md"]},
            "profiles": {"child": {"workspace_roots": ["workspace/alias"]}},
        }
    )
    assert resolve_profile(config, base=tmp_path).fragments == (tmp_path / "soul.md",)
    with pytest.raises(ConfigError, match="exceed"):
        resolve_profile(config, "child", base=tmp_path)


def test_proactive_override_and_admitted_budget_ceiling(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    config = load_config(path)
    admitted = resolve_profile(config, base=tmp_path, origin=Origin.PROACTIVE)
    raised = OperatorConfig.model_validate(
        config.model_dump() | {"proactive_budgets": {"model_calls": 12}}
    )
    current = resolve_profile(raised, base=tmp_path, origin=Origin.PROACTIVE)
    assert current.budgets.model_calls == 12 and current.budgets.tool_calls == 12
    assert restrict_to_admission(current, admitted).budgets.model_calls == 8
    lowered = OperatorConfig.model_validate(
        config.model_dump() | {"proactive_budgets": {"model_calls": 2}}
    )
    current = resolve_profile(lowered, base=tmp_path, origin=Origin.PROACTIVE)
    assert restrict_to_admission(current, admitted).budgets.model_calls == 2


def test_wildcards_expand_loaded_names_and_children_only_narrow(tmp_path: Path) -> None:
    config = OperatorConfig.model_validate(
        {
            "model": {"pin": "fixture"},
            "loaded_tools": ["files.read", "files.write", "runtime.status"],
            "defaults": {"workspace_roots": [str(tmp_path)], "tools": ["files.*", "not.loaded"]},
            "profiles": {
                "job": {"tools": ["files.r?ad"], "budgets": {"tool_calls": 1}},
                "widened": {"tools": ["*"]},
            },
        }
    )
    admitted = resolve_profile(config)
    assert admitted.tools == {"files.read", "files.write"}
    assert admitted.granted_tools == {"files.read", "files.write", "not.loaded"}
    job = resolve_profile(config, "job", origin=Origin.SCHEDULED)
    assert job.tools == {"files.read"} and job.budgets.tool_calls == 1
    assert restrict_to_admission(admitted, job).tools == {"files.read"}
    assert restrict_to_admission(admitted, job).origin == Origin.SCHEDULED
    with pytest.raises(ConfigError, match="exceed"):
        resolve_profile(config, "widened")
    added = OperatorConfig.model_validate(
        config.model_dump()
        | {
            "loaded_tools": [*config.loaded_tools, "files.new"],
        }
    )
    assert "files.new" in resolve_profile(added).tools
    assert "files.new" not in restrict_to_admission(resolve_profile(added), admitted).tools


def test_memory_credential_reference_is_redacted(profile: EffectiveProfile) -> None:
    from solakit.config import MemorySettings

    selected = profile.model_copy(
        update={"memory": MemorySettings(api_key_env="PRIVATE_MEMORY_KEY")}
    )
    explanation = str(selected.explain())
    assert "PRIVATE_MEMORY_KEY" not in explanation
    assert "<redacted>" in explanation
