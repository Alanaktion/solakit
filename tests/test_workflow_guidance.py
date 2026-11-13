"""Playbooks, the plan tool, step notices, wrap-up and repeat detection
(WORKFLOW-GUIDANCE-PLAN)."""

import asyncio
import json
from pathlib import Path

import pytest
from test_execution import completion, model_list, setup
from turns import plain

from solakit.core import playbooks
from solakit.core.config import Job
from solakit.core.prompt import read_turn, validate_prompt_files
from solakit.tools import ToolContext, ToolResult, tool

SCAFFOLD = Path(__file__).parents[1] / "src/solakit/scaffold/fragments"
PLAYBOOK = """---
name: triage
summary: Check mail and report urgent items
tools: [email.search, email.read]
---
1. Search.
2. Read.
"""


def write_playbook(config, name="triage", text=PLAYBOOK):
    root = config.paths.fragments / "playbooks"
    root.mkdir(exist_ok=True)
    (root / f"{name}.md").write_text(text)


def test_parse_and_caps():
    item = playbooks.parse("triage", PLAYBOOK)
    assert item.tools == ("email.search", "email.read")
    assert item.text().startswith('<playbook name="triage">')
    with pytest.raises(playbooks.PlaybookError, match="front matter"):
        playbooks.parse("x", "no front matter")
    with pytest.raises(playbooks.PlaybookError, match="must match"):
        playbooks.parse("other", PLAYBOOK)
    with pytest.raises(playbooks.PlaybookError, match="summary"):
        playbooks.parse("x", "---\nname: x\n---\nbody")
    with pytest.raises(playbooks.PlaybookError, match="exceeds"):
        playbooks.parse("triage", PLAYBOOK + "x" * 9000)
    with pytest.raises(playbooks.PlaybookError, match="invalid"):
        playbooks.parse("../x", PLAYBOOK)
    scaffold = playbooks.parse(
        "triage-inbox", (SCAFFOLD / "playbooks/triage-inbox.md").read_text()
    )
    assert scaffold.tools[0] == "email.list_mailboxes"


def test_index_in_stable_prefix_and_job_playbook_in_turn(config):
    write_playbook(config)
    config.profiles["interactive"].playbooks = ["triage"]
    validate_prompt_files(config)
    kwargs = dict(profile="interactive", session="s", origin="api")
    from datetime import datetime

    at = datetime.fromisoformat("2026-11-04T19:00:00+00:00")
    prompt, turn = read_turn(config, at=at, **kwargs)
    assert "- triage — Check mail and report urgent items" in prompt
    assert "<playbook " not in prompt and "<playbook " not in turn
    job_prompt, job_turn = read_turn(config, at=at, playbook="triage", **kwargs)
    assert job_prompt == prompt
    assert '<playbook name="triage">' in job_turn
    config.profiles["interactive"].playbooks = ["missing"]
    with pytest.raises(ValueError, match="missing"):
        validate_prompt_files(config)


def test_playbook_tool_visibility(config):
    from solakit.core.schemas import Session
    from solakit.core.store import Store
    from solakit.tools import Registry

    write_playbook(config)
    write_playbook(config, "secret", PLAYBOOK.replace("triage", "secret"))
    config.enabled_tools = ["playbook"]
    config.profiles["interactive"].tools = ["playbook.load"]
    config.profiles["interactive"].playbooks = ["triage"]
    registry = Registry(config)
    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    ctx = ToolContext(store.session("s"), "interactive", config, store, "r")
    ok = asyncio.run(registry.dispatch("playbook.load", '{"name": "triage"}', ctx))
    assert not ok.error and "1. Search." in ok.content
    hidden = asyncio.run(registry.dispatch("playbook.load", '{"name": "secret"}', ctx))
    assert hidden.error and "Available: triage" in hidden.content


def test_job_playbook_validation():
    from types import SimpleNamespace

    from solakit.jobs import Jobs

    job = Job(
        id="j",
        cron="0 8 * * *",
        profile="interactive",
        task="x",
        bind_session="s",
        playbook="nope",
    )
    instance = Jobs.__new__(Jobs)
    instance.config = SimpleNamespace(
        channels=[],
        profiles={"interactive": object()},
        paths=SimpleNamespace(fragments=Path("/nonexistent")),
    )
    with pytest.raises(ValueError, match="Job playbook"):
        instance.validate_job(job, check_tools=False)


def plan_call(name, arguments, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_plan_tool_persists_and_reminds_once(config):
    config.enabled_tools = ["plan"]
    config.profiles["interactive"].tools = ["plan.*"]
    config.profiles["interactive"].plan_reminder = True
    bodies = []
    script = [
        [plan_call("plan.set", {"items": ["one", "two"]}, "a")],
        [plan_call("plan.update", {"id": 1, "status": "done"}, "b")],
        None,  # premature final answer → reminder
        [plan_call("plan.update", {"id": 2, "status": "skipped"}, "c")],
        [
            plan_call(
                "plan.update", {"id": 2, "status": "skipped", "reason": "n/a"}, "d"
            )
        ],
        None,
    ]

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        calls = script[len(bodies) - 1]
        if calls is None:
            return completion(config, f"answer {len(bodies)}")
        return completion(config, calls=calls)

    execution = setup(config, handler)

    async def exercise():
        run = await execution.submit("s", "do two things")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    record = execution.store.run(run.id)
    assert [(i["id"], i["status"]) for i in record["plan"]] == [
        (1, "done"),
        (2, "skipped"),
    ]
    assert [i["kind"] for i in run.injections] == ["plan_reminder"]
    notice = bodies[3]["messages"][-1]
    assert notice["role"] == "user" and "<runtime-notice>" in notice["content"]
    assert "two" in notice["content"]
    # Injections never appear in the transcript as user/assistant/tool text.
    transcript = json.dumps(execution.store.transcript("s"))
    assert "runtime-notice" not in transcript
    assert record["injection_texts"][0]["kind"] == "plan_reminder"
    skipped = [
        m
        for m in bodies[4]["messages"]
        if m["role"] == "tool" and "reason" in m["content"]
    ]
    assert skipped


def looping_execution(config, *, answer="Summary of progress."):
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tool_choice") == "none":
            return completion(config, answer)
        return completion(
            config, calls=[plan_call("echo", {"text": "same"}, f"c{len(bodies)}")]
        )

    execution = setup(config, handler)

    @tool(name="echo", profiles=["interactive"])
    async def echo(ctx: ToolContext, text: str) -> ToolResult:
        return ToolResult(content=text)

    execution.registry.register(echo)
    config.profiles["interactive"].tools = ["echo"]
    execution.registry.validate()
    return execution, bodies


def run_once(execution):
    async def exercise():
        run = await execution.submit("s", "loop")
        await execution.close()
        return run

    return asyncio.run(exercise())


def test_step_notice_repeat_note_and_answer_wrapup(config):
    cfg = config.profiles["interactive"]
    cfg.max_steps, cfg.step_notice_at, cfg.wrapup = 4, 2, "answer"
    execution, bodies = looping_execution(config)
    run = run_once(execution)
    assert run.status == "completed" and run.completed_via_wrapup
    kinds = [i["kind"] for i in run.injections]
    assert kinds.count("step_notice") == 2 and kinds[-1] == "wrapup"
    tools = [m for m in bodies[-1]["messages"] if m["role"] == "tool"]
    assert "2 step(s) left" in tools[1]["content"]
    assert "this exact call has run 3 times" in tools[2]["content"]
    assert run.repeated_calls == 2
    assert bodies[-1]["tool_choice"] == "none" and bodies[-1]["tools"]
    transcript = execution.store.transcript("s")
    assert transcript[-1]["content"] == "Summary of progress."
    # Request-only notices never reach stored tool messages.
    assert all("step(s) left" not in (m["content"] or "") for m in transcript)
    assert plain(bodies[0]["messages"][-1]["content"]) == "loop"


def test_status_wrapup_keeps_budget_exceeded(config):
    cfg = config.profiles["interactive"]
    cfg.max_steps, cfg.wrapup = 2, "status"
    execution, _ = looping_execution(config)
    run = run_once(execution)
    assert run.status == "budget_exceeded" and run.completed_via_wrapup


def test_wrapup_off_is_unchanged(config):
    config.profiles["interactive"].max_steps = 2
    execution, bodies = looping_execution(config)
    run = run_once(execution)
    assert run.status == "budget_exceeded" and not run.completed_via_wrapup
    assert run.injections == [] and len(bodies) == 2


def test_scaffold_profile_fragment_mentions_core_rules():
    text = (SCAFFOLD / "profiles/interactive.md").read_text()
    for phrase in ("domain tool", "plan.set", "playbook.load", "ask instead"):
        assert phrase in text


def test_playbooks_api_and_operations_page(config, auth, mock_model):
    from fastapi.testclient import TestClient

    from solakit.main import create_app

    write_playbook(config)
    write_playbook(config, "broken", "no front matter")
    config.profiles["interactive"].playbooks = ["triage"]
    with TestClient(create_app(config=config, model=mock_model)) as client:
        data = client.get("/api/playbooks", headers=auth).json()
        assert [b["name"] for b in data] == ["broken", "triage"]
        assert data[1]["profiles"] == ["interactive"]
        assert data[0]["error"]
        client.post("/login", data={"token": "operator-test-token"})
        page = client.get("/settings/jobs")
        assert page.status_code == 200 and "triage" in page.text


@pytest.mark.parametrize("valid", [False, True])
def test_answer_wrapup_validates_structured_output(config, valid):
    cfg = config.profiles["interactive"]
    cfg.max_steps, cfg.wrapup = 1, "answer"
    answer = '{"ok": true}' if valid else "Summary without required JSON."
    execution, bodies = looping_execution(config, answer=answer)

    async def exercise():
        pending = await execution.enqueue(
            "s",
            "loop",
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            output_render="Result: {{ ok }}",
        )
        run = await pending.future
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == ("completed" if valid else "failed")
    assert len(bodies) == 2  # Validation cannot reopen the exhausted step budget.
    assert run.structured_output == ({"ok": True} if valid else None)
    if valid:
        assert run.rendered_output == "Result: True"


def test_answer_wrapup_rejects_action_only_reply(config):
    cfg = config.profiles["interactive"]
    cfg.max_steps, cfg.wrapup, cfg.completion_check = 1, "answer", True
    execution, bodies = looping_execution(config, answer="I'll check the status.")
    run = run_once(execution)
    assert run.status == "failed"
    assert len(bodies) == 2
    assert execution.store.transcript("s")[-1]["content"] == "I'll check the status."
