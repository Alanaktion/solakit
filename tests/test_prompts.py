import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from solakit.config import EffectiveProfile, load_config, resolve_profile
from solakit.evidence import canonical, digest
from solakit.prompts import PreparedPrompt, PromptInput, assemble, inspect_prompt
from solakit.records import SessionMode

FIXTURES = Path(__file__).parent / "fixtures/prompts"


def golden() -> tuple[EffectiveProfile, PromptInput]:
    profile = resolve_profile(load_config(FIXTURES / "prompt.toml"), base=FIXTURES)
    return profile, PromptInput.model_validate_json((FIXTURES / "input.json").read_bytes())


def test_golden_bytes_sections_ids_and_schema_order() -> None:
    profile, frozen = golden()
    prepared = assemble(profile, frozen)
    assert prepared == assemble(profile, frozen)
    assert prepared.body == (FIXTURES / "body.json").read_bytes()
    manifest = json.loads(prepared.manifest)
    for fragment in manifest["fragments"]:
        fragment["path"] = Path(fragment["path"]).name
    assert canonical(manifest) == (FIXTURES / "manifest.json").read_bytes()
    request = json.loads(prepared.body)
    assert request["messages"][-1]["content"] == frozen.input.content
    assert "\r\n" in request["messages"][0]["content"]
    assert [tool["function"]["name"] for tool in request["tools"]] == ["a.read", "z.status"]
    reordered = frozen.model_copy(update={"tools": tuple(reversed(frozen.tools))})
    assert assemble(profile, reordered) == prepared


def test_fragment_and_schema_changes_affect_hashes_and_recorded_inspection(tmp_path: Path) -> None:
    profile, frozen = golden()
    soul = tmp_path / "SOUL.md"
    soul.write_bytes(profile.soul_file.read_bytes()) if profile.soul_file else None
    profile = profile.model_copy(update={"soul_file": soul})
    old = assemble(profile, frozen)
    before = inspect_prompt(old, hashes=True)
    soul.write_bytes(b"Changed identity\r\n")
    changed = assemble(profile, frozen)
    assert digest(changed.body) != digest(old.body)
    assert (
        json.loads(changed.manifest)["fragments"][0]["sha256"]
        != json.loads(old.manifest)["fragments"][0]["sha256"]
    )
    assert inspect_prompt(old, hashes=True) == before
    tools = json.loads(canonical(frozen.tools))
    tools[0]["function"]["description"] = "Changed schema"
    changed_schema = assemble(profile, frozen.model_copy(update={"tools": tuple(tools)}))
    assert (
        json.loads(changed_schema.manifest)["tools_sha256"]
        != json.loads(old.manifest)["tools_sha256"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature": -1.0},
        {"top_p": 0.0},
        {"context_boundary": None},
        {"tools": ({"type": "function"},)},
    ],
)
def test_invalid_selection_and_sampling(changes: dict[str, Any]) -> None:
    profile, frozen = golden()
    with pytest.raises(ValueError):
        assemble(profile, frozen.model_copy(update=changes))


def test_estimates_reserve_output_and_tool_headroom_without_trimming() -> None:
    profile, frozen = golden()
    accounting = json.loads(assemble(profile, frozen).manifest)["accounting"]
    assert "estimate" in accounting["kind"] and "no verified tokenizer" in accounting["kind"]
    assert accounting["tool_result_reserved"] == 4096
    assert accounting["output_reserved"] == 4096
    assert accounting["safety_reserved"] == 3277
    assert accounting["components"]["tool_schemas"] > 0
    with pytest.raises(ValueError, match="budget"):
        assemble(profile.model_copy(update={"model_context_tokens": 1024}), frozen)


def test_modes_and_lineage() -> None:
    profile, frozen = golden()
    for mode in (SessionMode.ISOLATED, SessionMode.EPHEMERAL):
        with pytest.raises(ValueError, match="recall"):
            assemble(profile.model_copy(update={"mode": mode}), frozen)
    with pytest.raises(ValueError, match="continuing context"):
        assemble(
            profile.model_copy(update={"mode": SessionMode.EPHEMERAL}),
            frozen.model_copy(update={"recall": None}),
        )
    with pytest.raises(ValueError, match="distinct"):
        assemble(profile, frozen.model_copy(update={"history": (frozen.input,)}))
    with pytest.raises(ValueError, match="session"):
        assemble(
            profile,
            frozen.model_copy(
                update={"input": frozen.input.model_copy(update={"session_id": "f" * 32})}
            ),
        )
    with pytest.raises(ValueError, match="duplicate tool"):
        assemble(profile, frozen.model_copy(update={"tools": (frozen.tools[0], frozen.tools[0])}))


def test_recall_omitted_is_labelled_and_manifest_tampering_fails() -> None:
    profile, frozen = golden()
    prompt = assemble(profile, frozen.model_copy(update={"recall": None}))
    assert json.loads(prompt.manifest)["recall"] == "omitted"
    with pytest.raises(ValueError, match="hash"):
        inspect_prompt(PreparedPrompt(prompt.body + b" ", prompt.manifest))


def test_offline_cli_frozen_and_recorded_without_importing_clients(tmp_path: Path) -> None:
    script = (
        "import sys; from solakit.cli import main; sys.argv = ['sola', 'prompt', *sys.argv[1:]]; "
        "main(); assert 'openai' not in sys.modules; assert 'fastapi' not in sys.modules; "
        "assert 'solakit.runtime' not in sys.modules"
    )
    frozen = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "--config",
            str(FIXTURES / "prompt.toml"),
            "--frozen",
            str(FIXTURES / "input.json"),
            "--hashes",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    profile, value = golden()
    prompt = assemble(profile, value)
    assert json.loads(frozen.stdout) == inspect_prompt(prompt, hashes=True)
    body, manifest = tmp_path / "body", tmp_path / "manifest"
    body.write_bytes(prompt.body)
    manifest.write_bytes(prompt.manifest)
    recorded = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "--request-body",
            str(body),
            "--manifest",
            str(manifest),
            "--config",
            "missing.toml",
            "--hashes",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(recorded.stdout) == json.loads(frozen.stdout)
    assert not (FIXTURES / "data").exists()


def test_complete_tool_projection_and_ephemeral_lineage(profile: EffectiveProfile) -> None:
    from solakit.prompts import PromptTurn
    from solakit.records import Message, Origin, ToolProposal, new_id, utc_now

    user = Message(
        origin=Origin.INTERACTIVE,
        session_id=new_id(),
        run_id=new_id(),
        role="user",
        content="request",
    )
    assistant = PromptTurn(
        source_id=new_id(),
        session_id=user.session_id,
        role="assistant",
        content="",
        tool_calls=(ToolProposal(provider_tool_call_id="provider", name="echo", arguments="{}"),),
    )
    result = PromptTurn(
        source_id=new_id(),
        session_id=user.session_id,
        role="tool",
        content="ok",
        tool_call_id="provider",
    )
    value = PromptInput(at=utc_now(), input=user, continuation=(assistant, result))
    ephemeral = profile.model_copy(update={"mode": SessionMode.EPHEMERAL})
    assert json.loads(assemble(ephemeral, value).body)["messages"][-1]["tool_call_id"] == "provider"
    for turns in (
        (assistant,),
        (result,),
        (assistant, result.model_copy(update={"tool_call_id": "wrong"})),
        (assistant, result.model_copy(update={"session_id": new_id()})),
    ):
        with pytest.raises(ValueError):
            assemble(profile, value.model_copy(update={"continuation": turns}))
    with pytest.raises(ValueError, match="continuing context"):
        assemble(ephemeral, value.model_copy(update={"prior_turns": (assistant, result)}))
