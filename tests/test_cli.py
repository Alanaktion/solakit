import httpx

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
