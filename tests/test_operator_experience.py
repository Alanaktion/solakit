"""Model health stats, failure hints, config explain/diff, did-you-mean and
sola init (OPERATOR-EXPERIENCE-PLAN)."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from test_execution import completion, model_list, setup

from solakit.core.config import Profile
from solakit.core.explain import diff, explain
from solakit.core.hints import failure_hint
from solakit.core.schemas import Origin, Run
from solakit.core.startup import ConfigurationError, prepare
from solakit.main import create_app
from solakit.tools import Registry


def test_stats_api_and_page(config, auth, mock_model):
    with TestClient(create_app(config=config, model=mock_model)) as client:
        store = client.app.state.store
        failed = Run(
            session="x",
            profile="interactive",
            origin=Origin(),
            status="tool_error",
            error="boom",
            steps=2,
        )
        store.save_run(failed)
        store.log_tool(
            failed.id,
            {
                "name": "files.read",
                "result": {
                    "error": True,
                    "repairable": True,
                    "content": "Invalid tool arguments: path: missing — provide",
                },
                "normalizations": [{"path": "a", "kind": "x"}],
            },
        )
        store.log_tool(
            failed.id,
            {
                "name": "nope",
                "result": {
                    "error": True,
                    "repairable": True,
                    "content": "Unknown tool 'nope'.",
                },
            },
        )
        data = client.get("/api/stats/model", headers=auth).json()
        assert data["status"] == {"tool_error": 1}
        assert data["rates"]["repair_rate"] == 1.0
        assert data["rates"]["unknown_tool_rate"] == 0.5
        assert data["rates"]["normalizations"] == 1
        assert data["failing_arguments"] == [["files.read path (missing)", 1]]
        assert data["offending_runs"][0]["id"] == failed.id
        assert client.get("/api/stats/model?window=0", headers=auth).status_code == 422
        client.post("/login", data={"token": "operator-test-token"})
        page = client.get("/settings/system")
        assert page.status_code == 200 and failed.id[:8] in page.text


def test_failure_hints(config):
    def run(status, error=""):
        return Run(
            session="s",
            profile="interactive",
            origin=Origin(),
            status=status,
            error=error,
        )

    assert "thinking" in failure_hint(
        run("budget_exceeded", "Thinking exhausted model.max_tokens (8)"), config
    )
    assert "max_tokens" in failure_hint(
        run("budget_exceeded", "Model output reached model.max_tokens (8)"), config
    )
    assert 'strategy = "auto"' in failure_hint(run("context_limit"), config)
    assert "files.read" in failure_hint(
        run("tool_error"),
        config,
        [{"name": "files.read", "result": {"repairable": True}}],
    )
    assert failure_hint(run("completed"), config) is None


def test_run_records_hint(config):
    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return completion(config, "partial")

    config.profiles["interactive"].context_budget = 10
    execution = setup(config, handler)

    async def exercise():
        result = await execution.submit("s", "hello " * 50)
        await execution.close()
        return result

    result = asyncio.run(exercise())
    assert result.status == "context_limit"
    assert result.hint and "compaction" in result.hint


def test_explain_matches_admission(config):
    config.enabled_tools = ["math", "datetime"]
    config.toolsets = {"calc": ["math.*"]}
    config.profiles["interactive"] = Profile(tools=["@calc", "datetime.weekday"])
    registry = Registry(config)
    data = explain(config, registry, "interactive")
    assert data["effective_tools"] == list(registry.view("interactive").effective)
    assert data["effective_tools"] == ["math.eval", "datetime.weekday"]
    assert data["schema_bytes"] > 0
    assert data["model_request"]["first_step_params"] == {}
    with pytest.raises(ValueError):
        explain(config, registry, "missing")


def test_config_diff(tmp_path):
    old = tmp_path / "old.toml"
    new = tmp_path / "new.toml"
    base = 'enabled_tools = ["math", "datetime"]\n[paths]\ndata = "{data}"\n'
    old.write_text(
        base.format(data=tmp_path / "d")
        + '[profiles.interactive]\ntools = ["math.eval"]\n'
    )
    new.write_text(
        base.format(data=tmp_path / "d")
        + '[profiles.interactive]\ntools = ["datetime.*"]\neffects = ["operate"]\n'
    )
    lines = diff(old, new)
    assert "- interactive tools: math.eval" in lines
    assert "+ interactive tools: datetime.weekday" in lines
    assert "+ interactive effects: operate" in lines
    assert diff(old, old) == []


def test_did_you_mean(config, tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[model]\nmax_token = 5\n")
    monkeypatch.setenv("SOLA_CONFIG", str(path))
    with pytest.raises(ConfigurationError, match="did you mean max_tokens"):
        prepare()
    config.enabled_tools = ["math"]
    config.profiles["interactive"].tools = ["math.evl"]
    with pytest.raises(ValueError, match="did you mean math.eval"):
        Registry(config)


@pytest.mark.parametrize("preset", ["qwen3-35b-a3b-omlx", "generic-openai"])
def test_init_scaffold_is_valid_and_never_overwrites(tmp_path, monkeypatch, preset):
    from solakit.core import scaffold

    root = tmp_path / "site"
    files = scaffold.plan_files(preset, data=str(tmp_path / "data"))
    report = scaffold.write(root, files)
    assert any(line.startswith("created") for line in report)
    monkeypatch.setenv("SOLA_CONFIG", str(root / "config.toml"))
    monkeypatch.setenv("SOLA_OPERATOR_TOKEN", "init-test-token")
    prepared = prepare()
    assert set(prepared.config.profiles) <= {
        p.stem for p in (root / "fragments/profiles").glob("*.md")
    }
    assert "SOLA_OPERATOR_TOKEN=\n" in (root / ".env.example").read_text()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        scaffold.write(root, files)
    (root / "SOUL.md").write_text("Mine.\n")
    forced = scaffold.write(root, files, force=True)
    assert (root / "SOUL.md").read_text() == "Mine.\n"
    assert (root / "SOUL.md.new").exists()
    assert any(line.startswith("-Mine.") for line in forced)


def test_init_cli_yes(tmp_path, monkeypatch, capsys):
    from solakit import cli

    monkeypatch.setattr(
        "sys.argv",
        ["sola", "init", str(tmp_path / "x"), "--yes", "--data", str(tmp_path / "d")],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "created" in out and "sola doctor" in out
    assert (tmp_path / "x/config.toml").exists()
