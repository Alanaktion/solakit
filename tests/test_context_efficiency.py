"""Tool-result aging, local compaction and the concurrency guard
(CONTEXT-EFFICIENCY-PLAN)."""

import asyncio
import json

from test_execution import completion, model_list, setup

from solakit.core import doctor
from solakit.core.aging import age_tool_results
from solakit.core.compaction import Compactor, transcript_text
from solakit.core.config import CompactionConfig
from solakit.core.schemas import Message, Origin, Session
from solakit.tools import ToolContext, ToolResult, tool


def group(call_id, name, content):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def test_aging_is_deterministic_monotone_and_spares_errors():
    big = "x" * 2000
    error = json.dumps({"content": "y" * 2000, "error": True})
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        *group("a", "files.read", big),
        *group("b", "files.read", error),
        *group("c", "files.read", "short"),
        *group("d", "web.get_page", big),
        *group("e", "files.read", big),
    ]
    aged, stubbed = age_tool_results(messages, keep_groups=2, min_chars=1500)
    assert stubbed == ["a"]
    assert aged[3]["content"].startswith(
        "[Earlier result of files.read (2,000 chars) omitted"
    )
    assert aged[5]["content"] == error and aged[9]["content"] == big
    assert messages[3]["content"] == big  # evidence untouched
    assert age_tool_results(messages, 2, 1500) == (aged, stubbed)
    more = messages + group("f", "files.read", big)
    later, later_stubbed = age_tool_results(more, 2, 1500)
    assert later[: len(aged)][:4] == aged[:4]  # stubbed stays stubbed
    assert later_stubbed == ["a", "d"]
    assert age_tool_results(messages, 0, 1500)[1] == ["a", "d", "e"]


def test_aging_in_loop_records_evidence_and_keeps_transcript(config):
    cfg = config.profiles["interactive"]
    cfg.tool_result_aging, cfg.aging_keep_groups, cfg.aging_min_chars = (
        "auto",
        1,
        100,
    )
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        if len(bodies) <= 2:
            return completion(
                config,
                calls=[
                    {
                        "id": f"c{len(bodies)}",
                        "type": "function",
                        "function": {
                            "name": "big",
                            "arguments": json.dumps({"n": len(bodies)}),
                        },
                    }
                ],
            )
        return completion(config, "done")

    execution = setup(config, handler)

    @tool(name="big", profiles=["interactive"])
    async def big(ctx: ToolContext, n: int) -> ToolResult:
        return ToolResult(content=f"result {n} " + "z" * 500)

    execution.registry.register(big)
    cfg.tools = ["big"]
    execution.registry.validate()

    async def exercise():
        run = await execution.submit("s", "go")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    tools = [m for m in bodies[2]["messages"] if m["role"] == "tool"]
    assert tools[0]["content"].startswith("[Earlier result of big")
    assert "result 2" in tools[1]["content"]
    assert run.aging == [{"step": 3, "stubbed": ["c1"]}]
    stored = [m for m in execution.store.transcript("s") if m["role"] == "tool"]
    assert "zzzz" in stored[0]["content"]


def test_local_compaction_without_openviking(config):
    config.compaction = CompactionConfig(strategy="auto")
    requests = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        requests.append(json.loads(request.content))
        return completion(config, "Summary: user likes tea.")

    execution = setup(config, handler)
    store = execution.store
    for index in range(4):
        store.append_message(
            "s",
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=f"message {index} " + "w" * 50,
                origin=Origin(),
                run_id="old",
            ),
        )

    async def exercise():
        result = await execution.compact_session("s")
        await execution.close()
        return result

    result = asyncio.run(exercise())
    assert result["stage"] == "committed"
    boundary = store.transcript("s")[0]
    assert boundary["kind"] == "compaction"
    assert boundary["summary_source"] == "local_model"
    assert boundary["content"] == "Summary: user likes tea."
    assert store.archives("s") == ["s.jsonl.1"]
    assert "<transcript>" in requests[0]["messages"][1]["content"]
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_local_compaction_failure_is_context_limit(config):
    from solakit.core.compaction import ContextLimit

    config.compaction = CompactionConfig(strategy="local")

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        return completion(config, "   ")

    execution = setup(config, handler)
    execution.store.append_message(
        "s", Message(role="user", content="hi", origin=Origin(), run_id="r")
    )
    compactor = Compactor(execution.store, None, execution.model, config.compaction)

    async def exercise():
        try:
            await compactor.compact(Session(id="s"))
        finally:
            await execution.close()

    import pytest

    with pytest.raises(ContextLimit):
        asyncio.run(exercise())
    assert execution.store.archives("s") == []


def test_transcript_text_keeps_prior_summary_and_recent_turns():
    history = [{"role": "assistant", "kind": "compaction", "content": "OLD"}] + [
        {"role": "user", "content": f"turn {i} " + "q" * 100} for i in range(50)
    ]
    text = transcript_text(history, 1000)
    assert text.startswith("[earlier summary]\nOLD")
    assert "turn 49" in text and "earlier messages omitted" in text
    assert "turn 0 " not in text


def test_concurrency_guard_uses_last_doctor_run(config):
    assert doctor.concurrency_warning(config) is None
    probe = doctor.Probe("concurrency", "WARN", "x", data={"knee": 1})
    doctor.save(config, [probe])
    config.model.concurrency = 1
    assert doctor.concurrency_warning(config) is None
    config.model.concurrency = 4
    assert "exceeds the measured knee 1" in doctor.concurrency_warning(config)
