import asyncio
import json
from pathlib import Path

import httpx
import pytest

from solakit.core import eval as evaluation

SCENARIOS = Path(__file__).parents[1] / "evals" / "scenarios"


def scenario(**values):
    return evaluation.Scenario.model_validate(
        {"id": "s", "prompt": "Check mail", **values}
    )


def test_repository_scenarios_parse_and_name_known_tools(config):
    from solakit.core.config import Config
    from solakit.tools import Registry

    scenarios = evaluation.load_scenarios(SCENARIOS)
    assert len(scenarios) >= 20
    registry = Registry(Config(enabled_tools=evaluation.available_modules()))
    for item in scenarios:
        for name in item.tools + item.real_tools + item.forbid_tools:
            assert name in registry.tools, (item.id, name)
        assert set(item.real_tools) <= set(item.tools)
    quick = evaluation.load_scenarios(SCENARIOS, quick=True)
    assert quick and all(s.quick for s in quick)


def test_scenario_file_name_must_match_id(tmp_path):
    (tmp_path / "other.toml").write_text('id = "s"\nprompt = "x"\n')
    with pytest.raises(ValueError, match="must be named"):
        evaluation.load_scenarios(tmp_path)


def test_matrix_cross_product_and_aliases():
    cells = evaluation.parse_matrix(["disclosure=eager,deferred", "model.x=1,true"])
    assert cells == [
        {"profile.tool_disclosure": "eager", "model.x": 1},
        {"profile.tool_disclosure": "eager", "model.x": True},
        {"profile.tool_disclosure": "deferred", "model.x": 1},
        {"profile.tool_disclosure": "deferred", "model.x": True},
    ]
    with pytest.raises(ValueError):
        evaluation.parse_matrix(["broken"])


def test_filler_is_deterministic_and_sized():
    a = evaluation.filler_history(5000)
    assert a == evaluation.filler_history(5000)
    assert sum(len(m["content"]) for m in a) >= 5000


def test_fakes_keep_schema_bytes_and_match_responses(config):
    from solakit.core.config import Config
    from solakit.tools import Registry

    item = scenario(
        tools=["email.search", "math.eval"],
        responses={
            "email.search": [
                {"match": {"mailbox": "work"}, "content": "work mail"},
                {"content": "any mail"},
            ]
        },
    )
    real = Registry(Config(enabled_tools=["math", "sola_packs.email"]))
    faked = Registry(Config(enabled_tools=["math", "sola_packs.email"]))
    calls = []
    evaluation.install_fakes(faked, item, calls)
    for name in item.tools:
        assert faked.tools[name].schema() == real.tools[name].schema()
    function = faked.tools["email.search"].function
    result = function(None, mailbox="Work-mail")
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    assert result.content == "work mail"
    result = faked.tools["math.eval"].function(None, expression="1+1")
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    assert result.content == evaluation.DEFAULT_RESPONSE
    assert [c["name"] for c in calls] == ["email.search", "math.eval"]


class Scripted(httpx.AsyncBaseTransport):
    """A mock model that calls the first offered tool once, then answers."""

    def __init__(self, model_id, calls, answer):
        self.model_id, self.calls, self.answer = model_id, list(calls), answer
        self.samples = []
        self.requests = []

    async def handle_async_request(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": self.model_id}]})
        body = json.loads(request.content)
        self.requests.append(body)
        self.samples.append({"ttfb": 0.01, "seconds": 0.02})
        message = {"role": "assistant", "content": self.answer}
        if self.calls:
            name, arguments = self.calls.pop(0)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{len(self.calls)}",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "model": self.model_id,
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            },
        )


def test_mock_model_end_to_end_scoring_and_report(config, tmp_path):
    item = scenario(
        id="mail",
        family="selection",
        tools=["email.search", "files.read"],
        expect_first_tool="email.search",
        expect_calls=[{"name": "email.search", "args_subset": {"mailbox": "home"}}],
        forbid_tools=["files.read"],
        expect_final_contains=["invoice"],
        responses={"email.search": [{"content": "invoice overdue"}]},
    )
    transports = []

    def factory():
        transports.append(
            Scripted(
                config.model.model_id,
                [("email.search", '{"mailbox": "home"}')],
                "One urgent invoice.",
            )
        )
        return transports[-1]

    result = asyncio.run(
        evaluation.run_suite(config, [item], trials=2, transport_factory=factory)
    )
    stats = result["summary"]["{}"]["scenarios"]["mail"]
    assert stats["pass_rate"] == 1.0
    assert stats["first_tool_accuracy"] == 1.0
    assert stats["arg_valid_rate"] == 1.0
    assert stats["forbidden_call_rate"] == 0.0
    assert stats["prompt_tokens"] == 200
    assert stats["bytes_per_token"] > 0
    # Evaluation never touches the configured data directory.
    assert not config.paths.data.exists()
    assert "| mail |" in evaluation.markdown(result)
    path = evaluation.result_path(tmp_path, result)
    assert path.name.endswith(".json")


def test_mock_model_failures_are_scored(config):
    item = scenario(
        id="bad",
        tools=["email.search", "files.read"],
        expect_first_tool="email.search",
        forbid_tools=["files.read"],
    )

    def factory():
        return Scripted(
            config.model.model_id,
            [("files.read", '{"path": "x"}'), ("email.search", '{"bogus": 1}')],
            "done",
        )

    result = asyncio.run(
        evaluation.run_suite(config, [item], trials=1, transport_factory=factory)
    )
    trial = result["trials"][0]
    assert not trial["passed"]
    assert trial["forbidden"] == 1
    assert trial["repairs"] == 1
    assert "first tool files.read" in trial["failures"]


def test_compare_reports_deltas():
    def result(passes):
        return {
            "trials": [{"scenario": "a", "overrides": {}, "passed": p} for p in passes]
        }

    text = evaluation.compare(result([0, 0, 0, 0]), result([1, 1, 1, 1]))
    assert "+1.00 *" in text
    assert "**overall**" in text
