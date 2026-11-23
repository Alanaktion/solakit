import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ConfigDict

from solakit.config import EffectiveProfile, OperatorConfig, resolve_profile, restrict_to_admission
from solakit.dispatch import MAX_ARTIFACT_BYTES, Dispatcher, ToolSession
from solakit.evidence import StorageError, digest
from solakit.files import MAX_FILE_BYTES
from solakit.records import (
    ErrorCode,
    ModelStep,
    Origin,
    RunState,
    SessionMode,
    ToolCall,
    ToolReceipt,
    new_id,
)
from solakit.registry import Registry, RegistryError, load_registry
from solakit.sdk import Tool, ToolArguments, ToolContext, ToolError, ToolResult
from solakit.store import Store
from solakit.tools import TOOLS


@dataclass
class Harness:
    profile: EffectiveProfile
    store: Store
    dispatcher: Dispatcher
    session: ToolSession
    step: ModelStep
    root: Path

    def call(self, name: str, **arguments: Any) -> ToolCall:
        return ToolCall(
            origin=self.profile.origin,
            session_id=self.step.session_id,
            run_id=self.step.run_id,
            model_step_id=self.step.id,
            provider_tool_call_id=new_id(),
            name=name,
            arguments=arguments,
        )

    def dispatch(self, name: str, **arguments: Any) -> ToolReceipt:
        return asyncio.run(self.session.dispatch(self.call(name, **arguments)))


@contextmanager
def opened(
    root: Path,
    *,
    extra: tuple[Tool[Any], ...] = (),
    modules: tuple[str, ...] = (),
    grants: list[str] | None = None,
    mode: SessionMode = SessionMode.SHARED,
    budgets: dict[str, int] | None = None,
    identity: bool = False,
    blog_repository: Path | None = None,
    web_search_env: str | None = None,
    shell_enabled: bool = False,
    git_repositories: dict[str, dict[str, str]] | None = None,
    letters_repository: Path | None = None,
    letters_settings: dict[str, str] | None = None,
    timezone: str = "America/Denver",
) -> Iterator[Harness]:
    root = root.resolve()
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    registry = load_registry(modules)
    for tool in extra:
        registry.register(tool)
    soul = workspace / "SOUL.md" if identity else None
    if soul:
        soul.write_text("operator owned")
    profile = resolve_profile(
        OperatorConfig.model_validate(
            {
                "data_dir": str(root / "data"),
                "model": {"pin": "fixture"},
                "tool_modules": list(modules),
                "blog": {"repository": str(blog_repository)} if blog_repository else {},
                "web_search": {"api_key_env": web_search_env},
                "shell": {"enabled": shell_enabled},
                "git": {"repositories": git_repositories or {}},
                "letters": ({"repository": str(letters_repository)} if letters_repository else {})
                | (letters_settings or {}),
                "background": {"timezone": timezone},
                "soul_file": str(soul) if soul else None,
                "defaults": {
                    "workspace_roots": [str(workspace)],
                    "tools": grants if grants is not None else list(registry.tools),
                    "budgets": budgets or {},
                },
            }
        ),
        mode=mode,
    )
    profile = changed(
        profile, tools=profile.tools | {t.name for t in extra if t.name in profile.granted_tools}
    )
    store = Store(profile.data_dir)
    asyncio.run(store.start())
    try:
        session = store.create_session(mode=mode)
        run = store.admit(
            session.id,
            request_id=new_id(),
            content="fixture input",
            profile=profile.name,
            admission=profile.model_dump(mode="json"),
        )
        store.transition_run(run.id, RunState.RUNNING)
        step = ModelStep(
            origin=run.origin,
            session_id=session.id,
            run_id=run.id,
            index=1,
            model=profile.model_pin,
        )
        store.append(step)
        dispatcher = Dispatcher(store, registry, lambda admitted: profile)
        yield Harness(profile, store, dispatcher, dispatcher.bind(run.id), step, workspace)
    finally:
        asyncio.run(store.stop())


def changed(profile: EffectiveProfile, **changes: Any) -> EffectiveProfile:
    return EffectiveProfile.model_validate(profile.model_dump() | changes)


def test_durable_intent_receipt_atomic_write_and_duplicate(tmp_path: Path) -> None:
    with opened(tmp_path) as h:
        receipt = h.dispatch("files.write", path="hello.txt", content="hello λ")
        assert receipt.status == "succeeded" and receipt.effect == "confirmed"
        assert (h.root / "hello.txt").read_text() == "hello λ"
        assert h.dispatch("files.read", path="hello.txt").result == "hello λ"
        rejected = h.dispatch("files.write", path="hello.txt", content="lost")
        assert rejected.status == "failed" and rejected.effect == "none"
        assert (h.root / "hello.txt").read_text() == "hello λ"
        assert (
            h.dispatch("files.write", path="hello.txt", content="replaced", overwrite=True).status
            == "succeeded"
        )
        assert (h.root / "hello.txt").read_text() == "replaced"
        assert not list(h.root.glob(".sola-*.tmp"))
        call = h.call("runtime.status")
        asyncio.run(h.session.dispatch(call))
        with pytest.raises(ValueError, match="already"):
            asyncio.run(h.session.dispatch(call))
        clone = ToolCall.model_validate(call.model_dump() | {"id": new_id()})
        with pytest.raises(ValueError, match="already"):
            asyncio.run(h.session.dispatch(clone))
        events = h.store.events(h.step.session_id)
        kinds = [event.type for event in events if event.type.startswith("tool.")]
        assert kinds == ["tool.intent", "tool.receipt"] * 5
        entries = [entry for _, entry in h.store.log.scan() if entry.kind in ("tool", "receipt")]
        assert len(entries) == 10


@pytest.mark.parametrize("grants", [[], ["files.read"]])
def test_guessed_excluded_tools_empty_set_and_deferred_visibility(
    tmp_path: Path, grants: list[str]
) -> None:
    with opened(tmp_path, grants=grants) as h:
        assert h.session.schemas(deferred=frozenset({"files.read"})) == ()
        assert h.dispatcher.registry.disclose(frozenset({"files.write"}), h.profile.tools) == ()
        for name in ("files.write", "not.registered"):
            receipt = h.dispatch(name, path="denied", content="never")
            assert receipt.status == "denied" and receipt.effect == "none"
        assert not (h.root / "denied").exists()
        if grants:
            (h.root / "read").write_text("deferred is executable")
            assert h.dispatch("files.read", path="read").status == "succeeded"


def test_revocation_new_grants_roots_and_identity(tmp_path: Path) -> None:
    with opened(tmp_path, grants=["files.write", "runtime.status"]) as h:
        h.dispatcher.current = lambda admitted: changed(
            h.profile, tools=frozenset({"runtime.status"})
        )
        assert h.dispatch("files.write", path="denied", content="never").status == "denied"
        h.dispatcher.current = lambda admitted: changed(
            h.profile, tools=frozenset(h.dispatcher.registry.tools)
        )
        assert h.dispatch("files.read", path="denied").status == "denied"
        sub = h.root / "sub"
        sub.mkdir()
        h.dispatcher.current = lambda admitted: changed(h.profile, workspace_roots=(sub,))
        assert (
            h.dispatch("files.write", path=str(h.root / "denied"), content="never").status
            == "denied"
        )
        assert h.dispatch("files.write", path="allowed", content="yes").status == "succeeded"
        h.dispatcher.current = lambda admitted: changed(h.profile, model_pin="changed")
        assert h.dispatch("runtime.status").failure.code == ErrorCode.CONFIGURATION  # type: ignore[union-attr]
        assert not (h.root / "denied").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": 12},
        {"path": "bad", "content": "text", "overwrite": "true"},
        {"path": "bad", "content": "text", "extra": 1},
        {"path": "bad", "content": "x" * 65536},
    ],
)
def test_schema_and_argument_bounds_precede_effect(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    with opened(tmp_path) as h:
        receipt = asyncio.run(h.session.dispatch(h.call("files.write", **arguments)))
        assert receipt.status == "failed" and receipt.effect == "none"
        assert receipt.failure and receipt.failure.code == ErrorCode.INVALID_INPUT
        assert not (h.root / "bad").exists()


@pytest.mark.parametrize(
    "path", ["../escape", "missing/child", ".git/config", ".GiT/config", "a/../../escape"]
)
def test_bad_paths_do_not_write(tmp_path: Path, path: str) -> None:
    with opened(tmp_path) as h:
        receipt = h.dispatch("files.write", path=path, content="never")
        assert receipt.status == "denied" and receipt.effect == "none"
        assert list(h.root.iterdir()) == []
        assert not (tmp_path / "escape").exists()


def test_symlinks_git_indirection_hardlinks_and_managed_roots(tmp_path: Path) -> None:
    with opened(tmp_path) as h:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file").write_text("preserved")
        (h.root / "escape").symlink_to(outside, target_is_directory=True)
        (h.root / "leaf").symlink_to(outside / "file")
        (h.root / "inside").mkdir()
        (h.root / "alias").symlink_to(h.root / "inside", target_is_directory=True)
        (h.root / ".git").mkdir()
        (h.root / "git-alias").symlink_to(h.root / ".git", target_is_directory=True)
        os.link(outside / "file", h.root / "hardlink")
        h.dispatcher.protected += (h.root / "inside",)
        for path in (
            "escape/file",
            "leaf",
            "alias/file",
            "git-alias/config",
            "hardlink",
            str(h.profile.data_dir / "state.db"),
            str(Path(__file__).resolve().parents[1] / "pyproject.toml"),
        ):
            receipt = h.dispatch("files.write", path=path, content="never", overwrite=True)
            assert receipt.status == "denied" and receipt.effect == "none", (path, receipt)
        for path in ("escape/file", "leaf", "hardlink"):
            assert h.dispatch("files.read", path=path).status == "denied"
        assert (outside / "file").read_text() == "preserved"
        (h.root / ".git").rmdir()
        metadata = h.root / "metadata"
        metadata.mkdir()
        common = h.root / "common"
        common.mkdir()
        (metadata / "commondir").write_text("../common\n")
        (h.root / ".git").write_text("gitdir: metadata\n")
        for path in ("metadata/config", "common/config"):
            assert h.dispatch("files.write", path=path, content="never").status == "denied"
        assert not (metadata / "config").exists() and not (common / "config").exists()


def test_nested_worktree_git_metadata_outside_tree_is_protected(tmp_path: Path) -> None:
    with opened(tmp_path) as h:
        tree = h.root / "tree"
        tree.mkdir()
        metadata = h.root / "metadata"
        metadata.mkdir()
        (tree / ".git").write_text("gitdir: ../metadata\n")
        assert h.dispatch("files.write", path="metadata/config", content="never").status == "denied"
        assert not (metadata / "config").exists()


def test_parent_symlink_swap_and_atomic_leaf_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with opened(tmp_path) as h:
        directory = h.root / "parent"
        directory.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        real_open = os.open
        swapped = False

        def swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal swapped
            if path == "parent" and not swapped:
                swapped = True
                directory.rename(h.root / "moved")
                directory.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap)
        assert h.dispatch("files.write", path="parent/file", content="never").status == "denied"
        assert not (outside / "file").exists()
        monkeypatch.setattr(os, "open", real_open)
        target = h.root / "target"
        target.write_text("original")
        (outside / "file").write_text("preserved")
        replace = os.replace

        def swap_leaf(src: Any, dst: Any, **kwargs: Any) -> None:
            target.unlink()
            target.symlink_to(outside / "file")
            replace(src, dst, **kwargs)

        monkeypatch.setattr(os, "replace", swap_leaf)
        assert (
            h.dispatch("files.write", path="target", content="new", overwrite=True).status
            == "succeeded"
        )
        assert target.read_text() == "new" and (outside / "file").read_text() == "preserved"


@pytest.mark.parametrize("cap", [1, 10, 32])
def test_output_bounds_and_artifact_source_hash(tmp_path: Path, cap: int) -> None:
    output = "λ" * (MAX_ARTIFACT_BYTES + 1)

    async def large(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        return ToolResult(data=output)

    with opened(
        tmp_path,
        extra=(Tool("large", "Large result", ToolArguments, large),),
        budgets={"tool_result_bytes": cap},
    ) as h:
        receipt = h.dispatch("large")
        assert receipt.status == "succeeded" and receipt.truncated
        assert isinstance(receipt.result, str) and len(receipt.result.encode()) <= cap
        assert receipt.result.endswith("[truncated]" if cap >= 11 else "!")
        assert receipt.result_bytes == len(output.encode()) and receipt.result_sha256 == digest(
            output.encode()
        )
        assert receipt.artifact and receipt.artifact.length <= MAX_ARTIFACT_BYTES
        retained = (h.profile.data_dir / receipt.artifact.path).read_bytes()
        assert (
            retained.endswith(b"[artifact truncated]")
            and digest(retained) == receipt.artifact.sha256
        )


def test_read_size_and_non_text(tmp_path: Path) -> None:
    with opened(tmp_path) as h:
        (h.root / "large").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        assert h.dispatch("files.read", path="large").failure.code == ErrorCode.BUDGET_EXCEEDED  # type: ignore[union-attr]
        (h.root / "binary").write_bytes(b"\xff")
        assert h.dispatch("files.read", path="binary").failure.code == ErrorCode.INVALID_INPUT  # type: ignore[union-attr]


def test_call_budget_survives_rebind_and_current_budget_narrows(tmp_path: Path) -> None:
    with opened(tmp_path, budgets={"tool_calls": 2}) as h:
        assert h.dispatch("runtime.status").status == "succeeded"
        h.session = h.dispatcher.bind(h.step.run_id)
        assert h.dispatch("runtime.status").status == "succeeded"
        assert h.dispatch("files.write", path="never", content="never").status == "denied"
        assert not (h.root / "never").exists()
        assert h.store.tool_attempts(h.step.run_id) == 3


def test_intent_failure_never_calls_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with opened(tmp_path) as h:
        append = h.store.append

        def fail(record: Any) -> None:
            if isinstance(record, ToolCall):
                raise StorageError("fixture intent failure")
            append(record)

        monkeypatch.setattr(h.store, "append", fail)
        with pytest.raises(StorageError):
            h.dispatch("files.write", path="never", content="never")
        assert not (h.root / "never").exists()


def test_context_expires_between_calls_and_read_tool_cannot_write(tmp_path: Path) -> None:
    contexts: list[ToolContext] = []

    async def retain(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        contexts.append(ctx)
        if len(contexts) > 1:
            with pytest.raises(ToolError):
                contexts[0].services.status()
            with pytest.raises(ToolError):
                contexts[0].confirm_effect()
        return ToolResult(data="saved")

    async def forbidden(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        ctx.files.write_text("never", "never")
        return ToolResult()

    with opened(
        tmp_path,
        extra=(
            Tool("retain", "Save context", ToolArguments, retain),
            Tool("forbidden", "No write declaration", ToolArguments, forbidden),
        ),
    ) as h:
        assert h.dispatch("retain").status == "succeeded"
        assert h.dispatch("retain").status == "succeeded"
        assert h.dispatch("forbidden").status == "denied"
        assert not (h.root / "never").exists()
        with pytest.raises(ToolError):
            contexts[0].files.read_text("anything")


def test_timeout_uncertainty_cancellation_redaction_and_inflight_revocation(tmp_path: Path) -> None:
    async def hang(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult()

    async def sent(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        ctx.begin_effect(("PRIVATE_KEY",))
        assert "PRIVATE_KEY" not in h.dispatcher.redactor.redact("PRIVATE_KEY")
        await asyncio.sleep(10)
        return ToolResult()

    async def echoed(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        ctx.begin_effect(("PRIVATE_KEY",))
        ctx.confirm_effect()
        return ToolResult(data="PRIVATE_KEY")

    async def revoke(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        h.dispatcher.current = lambda admitted: changed(h.profile, tools=frozenset())
        ctx.files.write_text("never", "never")
        return ToolResult()

    extra = tuple(
        Tool(name, name, ToolArguments, fn, consequential=True)
        for name, fn in (("hang", hang), ("sent", sent), ("echoed", echoed), ("revoke", revoke))
    )
    with opened(tmp_path, extra=extra, budgets={"tool_timeout_seconds": 1}) as h:
        timeout = h.dispatch("hang")
        assert (
            timeout.status == "failed"
            and timeout.failure
            and timeout.failure.code == ErrorCode.TIMEOUT
        )
        uncertain = h.dispatch("sent")
        assert uncertain.status == "uncertain" and uncertain.effect == "uncertain"
        assert h.dispatch("echoed").result == "<redacted>"

        async def cancel(name: str) -> None:
            call = h.call(name)
            task = asyncio.create_task(h.session.dispatch(call))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            receipts = [
                r
                for _, r in h.store.run_records(h.step.run_id, limit=100)
                if isinstance(r, ToolReceipt) and r.tool_call_id == call.id
            ]
            assert len(receipts) == 1
            assert receipts[0].status == ("uncertain" if name == "sent" else "cancelled")

        asyncio.run(cancel("hang"))
        asyncio.run(cancel("sent"))
        assert h.dispatch("revoke").status == "denied"
        assert not (h.root / "never").exists()


@pytest.mark.parametrize("mode", list(SessionMode))
def test_transcript_scope_and_memory_port(tmp_path: Path, mode: SessionMode) -> None:
    with opened(tmp_path, mode=mode) as h:
        for other_mode in SessionMode:
            session = h.store.create_session(mode=other_mode)
            h.store.admit(
                session.id, request_id=new_id(), content=f"fixture {other_mode}", profile="default"
            )
        receipt = h.dispatch("transcripts.search", query="fixture")
        assert receipt.status == "succeeded" and isinstance(receipt.result, str)
        results = json.loads(receipt.result)
        assert results and all(item["kind"] == "message" for item in results)
        if mode == SessionMode.SHARED:
            assert len(results) == 2
        else:
            assert all(item["session_id"] == h.step.session_id for item in results)
        if mode == SessionMode.EPHEMERAL:
            assert all(item["run_id"] == h.step.run_id for item in results)
        recall = h.dispatch("memory.recall", query="anything")
        assert recall.failure and recall.failure.code == (
            ErrorCode.MEMORY_UNAVAILABLE if mode == SessionMode.SHARED else ErrorCode.TOOL_DENIED
        )


def test_origin_restriction_and_missing_admission_ceiling(tmp_path: Path) -> None:
    async def scheduled(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        raise AssertionError("must not execute")

    with opened(
        tmp_path,
        extra=(
            Tool(
                "scheduled",
                "Scheduled only",
                ToolArguments,
                scheduled,
                origins=frozenset({Origin.SCHEDULED}),
            ),
        ),
    ) as h:
        assert h.dispatch("scheduled").status == "denied"
        session = h.store.create_session()
        run = h.store.admit(session.id, request_id=new_id(), content="legacy", profile="default")
        h.store.transition_run(run.id, RunState.RUNNING)
        with pytest.raises(ValueError, match="durable effective profile"):
            h.dispatcher.bind(run.id)


def test_registry_errors_and_explicit_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        return ToolResult()

    tool = Tool("valid", "Valid", ToolArguments, handler)
    registry = Registry((tool,))
    with pytest.raises(RegistryError, match="duplicate"):
        registry.register(tool)
    with pytest.raises(RegistryError, match="valid explicit name"):
        registry.register(Tool("../bad", "Invalid", ToolArguments, handler))

    class Invalid(ToolArguments):
        model_config = ConfigDict(extra="allow")

    with pytest.raises(RegistryError, match="schema"):
        Registry((Tool("bad", "Invalid schema", Invalid, handler),))
    for version in (None, True, 2):
        module = ModuleType("fixture_pack")
        module.SDK_VERSION = version  # type: ignore[attr-defined]
        module.TOOLS = ()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fixture_pack", module)
        with pytest.raises(RegistryError, match="SDK"):
            Registry().load(("fixture_pack",))
    (tmp_path / "missing_dependency_pack.py").write_text("import sola_missing_fixture_dependency")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RegistryError, match="dependency unavailable"):
        Registry().load(("missing_dependency_pack",))
    assert not Registry().tools
    assert set(load_registry().tools) == {tool.name for tool in TOOLS}


@pytest.mark.parametrize("grants", [[], ["fixture.read"], ["fixture.*"]])
def test_external_module_loading_requires_only_profile_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grants: list[str]
) -> None:
    async def handler(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        return ToolResult(data="external tool")

    module = ModuleType("fixture_pack")
    module.SDK_VERSION = 1  # type: ignore[attr-defined]
    module.TOOLS = (Tool("fixture.read", "External tool", ToolArguments, handler),)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fixture_pack", module)
    with opened(tmp_path, modules=("fixture_pack",), grants=grants) as h:
        assert "fixture.read" in h.dispatcher.registry.tools
        assert "files.read" in h.dispatcher.registry.tools
        assert h.dispatch("files.read", path="denied").status == "denied"
        receipt = h.dispatch("fixture.read")
        if grants:
            assert h.profile.tools == {"fixture.read"}
            assert receipt.status == "succeeded" and receipt.result == "external tool"
            added = Tool("fixture.new", "New external tool", ToolArguments, handler)
            module.TOOLS += (added,)  # type: ignore[attr-defined]
            config = OperatorConfig.model_validate(
                {
                    "data_dir": str(h.profile.data_dir),
                    "model": {"pin": h.profile.model_pin},
                    "tool_modules": ["fixture_pack"],
                    "defaults": {"workspace_roots": [str(h.root)], "tools": grants},
                }
            )
            current = resolve_profile(config)
            assert ("fixture.new" in current.tools) == (grants == ["fixture.*"])
            assert "fixture.new" not in restrict_to_admission(current, h.profile).tools
            removed = config.model_copy(update={"tool_modules": []})
            h.dispatcher.current = lambda admitted: resolve_profile(removed)
            assert h.dispatch("fixture.read").status == "denied"
        else:
            assert not h.profile.tools
            assert receipt.status == "denied"


def test_current_budget_reduction_and_active_elapsed_ceiling(tmp_path: Path) -> None:
    with opened(tmp_path) as h:
        assert h.dispatch("runtime.status").status == "succeeded"
        budget = h.profile.budgets.model_copy(update={"tool_calls": 1})
        h.dispatcher.current = lambda admitted: changed(h.profile, budgets=budget)
        receipt = h.dispatch("files.write", path="never", content="never")
        assert receipt.failure and receipt.failure.code == ErrorCode.BUDGET_EXCEEDED
        h.dispatcher.current = lambda admitted: h.profile
        h.session.started -= h.profile.budgets.active_seconds
        receipt = h.dispatch("files.write", path="never", content="never")
        assert receipt.failure and receipt.failure.code == ErrorCode.BUDGET_EXCEEDED
        assert not (h.root / "never").exists()


def test_receipt_failure_preserves_effect_and_recovers_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with opened(tmp_path) as h:
        append = h.store.append

        def fail(record: Any) -> None:
            if isinstance(record, ToolReceipt):
                raise StorageError("fixture receipt failure")
            append(record)

        monkeypatch.setattr(h.store, "append", fail)
        with pytest.raises(StorageError):
            h.dispatch("files.write", path="effect", content="done once")
        assert (h.root / "effect").read_text() == "done once"
        monkeypatch.setattr(h.store, "append", append)
        asyncio.run(h.store.stop())
        asyncio.run(h.store.start())
        assert h.store.run(h.step.run_id).state == RunState.INTERRUPTED
        receipts = [
            record
            for _, record in h.store.run_records(h.step.run_id)
            if isinstance(record, ToolReceipt)
        ]
        assert len(receipts) == 1 and receipts[0].status == "uncertain"
        assert (h.root / "effect").read_text() == "done once"


def test_confirmed_effect_survives_later_timeout_and_errors_are_redacted(tmp_path: Path) -> None:
    async def partial(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        ctx.files.write_text("effect", "confirmed before timeout")
        await asyncio.sleep(10)
        return ToolResult()

    async def secret(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        ctx.begin_effect(('PRIVATE"KEY',))
        ctx.confirm_effect()
        return ToolResult(data={"nested": ['PRIVATE"KEY']})

    async def error(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        raise ToolError('PRIVATE"KEY')

    with opened(
        tmp_path,
        extra=(
            Tool("partial", "Confirmed partial effect", ToolArguments, partial, True),
            Tool("secret", "Credential registration", ToolArguments, secret, True),
            Tool("error", "Recoverable error", ToolArguments, error),
        ),
        budgets={"tool_timeout_seconds": 1},
    ) as h:
        receipt = h.dispatch("partial")
        assert receipt.status == "failed" and receipt.effect == "confirmed"
        assert receipt.failure and receipt.failure.code == ErrorCode.TIMEOUT
        assert (h.root / "effect").read_text() == "confirmed before timeout"
        assert receipt.artifact
        assert (
            h.profile.data_dir / receipt.artifact.path
        ).read_text() == "confirmed before timeout"
        assert h.dispatch("secret").result == '{"nested":["<redacted>"]}'
        receipt = h.dispatch("error")
        assert receipt.failure and receipt.failure.message == "<redacted>"


@pytest.mark.parametrize("field", ["granted_tools", "tools"])
def test_revocation_of_each_authority_set_is_enforced(tmp_path: Path, field: str) -> None:
    with opened(tmp_path) as h:
        h.dispatcher.current = lambda admitted: changed(h.profile, **{field: frozenset()})
        assert h.dispatch("files.write", path="never", content="never").status == "denied"
        assert not (h.root / "never").exists()


def test_bare_git_repository_and_configured_prompt_files_are_protected(tmp_path: Path) -> None:
    with opened(tmp_path, identity=True) as h:
        bare = h.root / "bare"
        bare.mkdir()
        (bare / "HEAD").write_text("ref: refs/heads/main\n")
        (bare / "objects").mkdir()
        (bare / "refs").mkdir()
        assert h.dispatch("files.write", path="bare/config", content="never").status == "denied"
        soul = h.root / "SOUL.md"
        assert (
            h.dispatch("files.write", path="SOUL.md", content="never", overwrite=True).status
            == "denied"
        )
        assert soul.read_text() == "operator owned"


def test_non_yielding_tool_is_not_reported_successful_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = time.monotonic
    offset = 0.0

    async def late(ctx: ToolContext, args: ToolArguments) -> ToolResult:
        nonlocal offset
        offset += 2
        return ToolResult(data="late")

    with opened(
        tmp_path,
        extra=(Tool("late", "Late return", ToolArguments, late),),
        budgets={"tool_timeout_seconds": 1},
    ) as h:
        monkeypatch.setattr(time, "monotonic", lambda: clock() + offset)
        receipt = h.dispatch("late")
        assert receipt.status == "failed" and receipt.effect == "none"
        assert receipt.failure and receipt.failure.code == ErrorCode.TIMEOUT


def test_search_projection_cannot_forge_shared_session_authority(tmp_path: Path) -> None:
    import sqlite3

    with opened(tmp_path) as h:
        private = h.store.create_session(mode=SessionMode.ISOLATED)
        h.store.admit(private.id, request_id=new_id(), content="private needle", profile="default")
        db = sqlite3.connect(h.profile.data_dir / "search.db")
        try:
            db.execute(
                "UPDATE documents SET text=? WHERE kind='session' AND record_id=?",
                ('{"mode":"shared"}', private.id),
            )
            db.commit()
        finally:
            db.close()
        receipt = h.dispatch("transcripts.search", query="needle")
        assert receipt.status == "failed" and receipt.result is None
        assert receipt.failure and receipt.failure.code == ErrorCode.CORRUPTION


def test_file_workflow_list_search_unicode_and_artifact(tmp_path: Path) -> None:
    with opened(tmp_path) as h:
        (h.root / "nested").mkdir()
        (h.root / "brief.txt").write_text("Use violet and cream.\n")
        (h.root / "nested" / "café.txt").write_text("猫 likes violet.\n")
        listing = h.dispatch("files.list")
        assert listing.status == "succeeded"
        assert {entry["name"] for entry in json.loads(str(listing.result))["entries"]} == {
            "brief.txt",
            "nested",
        }
        found = h.dispatch("files.search", query="violet")
        assert found.status == "succeeded"
        matches = json.loads(str(found.result))["matches"]
        assert len(matches) == 2 and {m["line"] for m in matches} == {1}
        for match in matches:
            assert h.dispatch("files.read", path=match["path"]).status == "succeeded"
        output = h.dispatch("files.write", path="result.txt", content="猫: violet and cream")
        assert output.status == "succeeded" and output.artifact
        retained = (h.profile.data_dir / output.artifact.path).read_bytes()
        assert retained == (h.root / "result.txt").read_bytes()
        assert output.artifact.sha256 == json.loads(str(output.result))["sha256"]
        assert h.dispatch("files.write", path="result.txt", content="bad").status == "failed"
        assert (
            h.dispatch("files.write", path="missing/result.txt", content="bad").status == "denied"
        )


def test_file_scans_are_bounded_and_skip_protected_aliases(tmp_path: Path) -> None:
    with opened(tmp_path, identity=True) as h:
        (h.root / "secret-link").symlink_to(h.root / "SOUL.md")
        (h.root / "huge.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        (h.root / "binary.bin").write_bytes(b"\xff")
        (h.root / "matches.txt").write_text("needle\n" * 25)
        listing = json.loads(str(h.dispatch("files.list", limit=1).result))
        assert listing["truncated"] and len(listing["entries"]) == 1
        names = {
            entry["name"] for entry in json.loads(str(h.dispatch("files.list").result))["entries"]
        }
        assert "SOUL.md" not in names and "secret-link" not in names
        found = json.loads(str(h.dispatch("files.search", query="needle", limit=2).result))
        assert found["truncated"] and len(found["matches"]) == 2
        assert h.dispatch("files.list", path="../").status == "denied"
        assert h.dispatch("files.search", path="secret-link", query="needle").status == "denied"
