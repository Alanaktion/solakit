import asyncio
import json

import pytest
from pydantic import ValidationError
from test_execution import completion, model_list, setup

from solakit.core import structured
from solakit.core.config import Job

SCHEMA = {
    "type": "object",
    "properties": {
        "urgent": {"type": "integer"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["urgent", "items"],
    "additionalProperties": False,
}


def test_validator_and_extract():
    assert structured.errors(SCHEMA, {"urgent": 1, "items": ["a"]}) == []
    assert structured.errors(SCHEMA, {"urgent": "1", "items": [2], "x": 0}) == [
        "$.x: not allowed",
        "$.urgent: expected integer",
        "$.items[0]: expected string",
    ]
    assert structured.errors({"type": "integer"}, True) == ["$: expected integer"]
    assert structured.extract('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        structured.extract("not json")
    assert (
        structured.render(
            "{{ urgent }} urgent: {{ items|join(', ') }}",
            {
                "urgent": 2,
                "items": ["a", "b"],
            },
        )
        == "2 urgent: a, b"
    )


def test_job_validation():
    base = dict(
        id="j", cron="0 8 * * *", profile="interactive", task="x", bind_session="s"
    )
    Job(**base, output_schema=SCHEMA, render="{{ urgent }}", then="other")
    with pytest.raises(ValidationError, match="render requires output_schema"):
        Job(**base, render="x")
    with pytest.raises(ValidationError, match="another job"):
        Job(**base, then="j")
    with pytest.raises(ValidationError, match="invalid"):
        Job(**base, output_schema=SCHEMA, render="{{ unclosed")
    with pytest.raises(ValidationError, match="top-level type"):
        Job(**base, output_schema={"properties": {}})


def run_structured(config, replies, render=None):
    bodies = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        return completion(config, replies[len(bodies) - 1])

    execution = setup(config, handler)

    async def exercise():
        pending = await execution.enqueue(
            "s", "summarize", output_schema=SCHEMA, output_render=render
        )
        run = await pending.future
        await execution.close()
        return run

    return asyncio.run(exercise()), bodies


def test_retry_then_render(config):
    run, bodies = run_structured(
        config,
        ["Here you go: two items", '{"urgent": 1, "items": ["rent"]}'],
        render="{{ urgent }} urgent: {{ items|join(', ') }}",
    )
    assert run.status == "completed"
    assert run.structured_output == {"urgent": 1, "items": ["rent"]}
    assert run.rendered_output == "1 urgent: rent"
    notice = bodies[1]["messages"][-1]
    assert (
        "<runtime-notice>" in notice["content"]
        and "not valid JSON" in (notice["content"])
    )
    assert [i["kind"] for i in run.injections] == ["structured_output"]


def test_invalid_after_retries_is_a_failed_status(config):
    run, bodies = run_structured(config, ["nope", "still nope", '{"urgent": 1}'])
    assert run.status == "failed"
    assert run.error.startswith("Structured output did not match the job schema")
    assert len(bodies) == 3 and run.structured_output is None


def test_then_chain_cycles_rejected_and_trigger_called():
    from types import SimpleNamespace

    from solakit.jobs import Jobs

    base = dict(cron="0 8 * * *", profile="interactive", task="x", bind_session="s")
    instance = Jobs.__new__(Jobs)
    instance.config = SimpleNamespace(channels=[], profiles={"interactive": object()})
    from solakit.core.schemas import now

    instance.clock = now
    instance.jobs = {
        "b": Job(id="b", then="c", **base),
        "c": Job(id="c", then="a", **base),
    }
    with pytest.raises(ValueError, match="cycle"):
        instance.validate_job(Job(id="a", then="b", **base), check_tools=False)
    instance.jobs["c"] = Job(id="c", **base)
    instance.validate_job(Job(id="a", then="b", **base), check_tools=False)
    triggered = []

    async def trigger(job_id, *, authorize=None):
        triggered.append(job_id)
        raise ValueError("disabled")

    instance.trigger = trigger
    asyncio.run(instance._chain(Job(id="a", then="b", **base)))
    assert triggered == ["b"]
