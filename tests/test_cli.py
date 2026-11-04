import httpx
import pytest

from solakit.cli import main


def test_cli_prints_exact_prompt(config, monkeypatch, capsys):
    monkeypatch.setattr("solakit.cli.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "prompt", "--profile", "interactive"])
    captured = []

    def get(url, **kwargs):
        captured.append((url, kwargs))
        return httpx.Response(
            200, json={"prompt": "Verbatim\r\n  "}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("solakit.cli.httpx.get", get)
    main()
    assert capsys.readouterr().out == "Verbatim\r\n  "
    assert captured[0][1]["headers"]["Authorization"] == "Bearer operator-test-token"
    assert captured[0][1]["trust_env"] is False


def test_offline_check_has_actionable_error_without_traceback(
    config, monkeypatch, capsys
):
    import pytest

    monkeypatch.setattr("solakit.core.startup.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "check"])

    def forbidden(*args, **kwargs):
        raise AssertionError("offline check must not call external services")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    main()
    assert capsys.readouterr().out == "Configuration valid.\n"
    (config.paths.fragments / "profiles/interactive.md").unlink()
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    stderr = capsys.readouterr().err
    assert "Configuration error:" in stderr
    assert "profiles/interactive.md" in stderr
    assert "Traceback" not in stderr


def test_offline_check_reports_invalid_tool_grants(config, monkeypatch, capsys):
    import pytest

    monkeypatch.setattr("solakit.core.startup.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "check"])
    config.enabled_tools = ["files"]
    config.profiles["interactive"].tools = ["files.write"]
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    stderr = capsys.readouterr().err
    assert (
        "Configuration error: filesystem tool requires explicit write roots" in stderr
    )
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    "options, port, level",
    [([], 8382, "info"), (["--port", "9000", "--log-level", "debug"], 9000, "debug")],
)
def test_serve_defaults_and_overrides(config, monkeypatch, options, port, level):
    monkeypatch.setattr("solakit.core.startup.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["sola", "serve", *options])
    observed = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: observed.append(kwargs))
    main()
    assert observed == [
        {"host": "127.0.0.1", "port": port, "log_level": level, "workers": 1}
    ]


def test_default_cli_uses_solakit_port(config, monkeypatch, capsys):
    monkeypatch.setattr("solakit.cli.load_config", lambda: config)
    monkeypatch.delenv("SOLA_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["sola", "prompt"])
    urls = []

    def get(url, **kwargs):
        urls.append(url)
        return httpx.Response(
            200, json={"prompt": "exact"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr("httpx.get", get)
    main()
    assert urls == ["http://127.0.0.1:8382/api/prompt"]
    assert capsys.readouterr().out == "exact"
