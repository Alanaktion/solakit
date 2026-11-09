import asyncio
import json

from test_execution import completion, model_list, setup

from solakit.core.archival import archive_session
from solakit.core.prompt import notes_text


def call(name, arguments, call_id="n1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_notes_persist_appear_in_turn_and_survive_archival(config):
    config.enabled_tools = ["notes"]
    config.profiles["interactive"].tools = ["notes.*"]
    bodies = []
    script = [
        [call("notes.set", {"key": "repo_path", "value": "/src/app"})],
        "Saved.",
        "It is /src/app.",
    ]

    def handler(request):
        if request.url.path.endswith("/models"):
            return model_list(config)
        bodies.append(json.loads(request.content))
        step = script[len(bodies) - 1]
        if isinstance(step, list):
            return completion(config, calls=step)
        return completion(config, step)

    execution = setup(config, handler)

    async def exercise():
        await execution.submit("s", "remember the repo path /src/app")
        archive_session(execution.store, "s")
        run = await execution.submit("s", "what is the repo path?")
        await execution.close()
        return run

    run = asyncio.run(exercise())
    assert run.status == "completed"
    assert "- repo_path: /src/app" in bodies[-1]["messages"][-1]["content"]
    assert len(bodies[-1]["messages"]) == 2  # archived history, notes kept
    assert execution.store.session_notes("s")["repo_path"]["value"] == "/src/app"
    record = execution.store.run(run.id)
    assert "repo_path" in record["turn_context"]


def test_notes_text_cap_and_delete(config):
    from solakit.core.schemas import Session
    from solakit.core.store import Store

    store = Store(config.paths.data)
    store.create_session(Session(id="s"))
    store.log_session_note("s", {"key": "a", "value": "1"})
    store.log_session_note("s", {"key": "b", "value": "2"})
    store.log_session_note("s", {"key": "a", "value": None})
    assert list(store.session_notes("s")) == ["b"]
    many = {f"k{i}": {"value": "v" * 100} for i in range(40)}
    text = notes_text(many)
    assert len(text) < 2300 and "older notes" in text and "k39" in text
    assert notes_text({}) == ""
