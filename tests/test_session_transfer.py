import asyncio
import io
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from test_compaction import compact_profile, material, seed, summary_reply
from test_delegation import DELEGATE, get_link, setup, start_parent
from test_images import png
from test_jobs import Clock, job
from test_run_loop import harness, reply, wire
from test_storage import opened

from solakit.config import EffectiveProfile, ImageSettings
from solakit.engine.compaction import active_boundary, manifest_for
from solakit.engine.run_loop import records
from solakit.records import (
    Artifact,
    Delivery,
    ImageGeneration,
    MemoryCommit,
    MemoryState,
    Message,
    ModelRequest,
    ModelStep,
    RunState,
    SessionMode,
    ToolCall,
    new_id,
)
from solakit.storage.evidence import canonical, digest, read_artifact
from solakit.storage.search import search
from solakit.storage.session_transfer import (
    EVIDENCE,
    MANIFEST,
    PROVENANCE,
    TransferConflict,
    export_session,
    import_session,
)
from solakit.tools.images import ImageService


def test_delegated_session_archive_round_trip(profile: EffectiveProfile, tmp_path: Path) -> None:
    async def make_archive() -> tuple[bytes, str, str, Artifact, bytes]:
        context, _ = setup(profile, [DELEGATE, reply("Worker report."), reply("Parent report.")])
        await context.start()
        try:
            assert context.store and context.runs
            store = context.store
            parent = start_parent(context)
            assert (await context.runs.wait(parent.id, timeout=5)).state == RunState.COMPLETED
            link = get_link(store, parent.id)
            record = next(
                r for r in records(store, link.child_run_id) if isinstance(r, ModelRequest)
            )
            store.update_session(
                parent.session_id, title="A private survey", archived=True, pinned=True
            )
            archive = export_session(store, parent.session_id)
            copy = import_session(store, archive, new_id())
            assert copy != parent.session_id
            child_copy = import_session(
                store, export_session(store, link.child_session_id), new_id()
            )
            assert store.session(child_copy).parent_session_id not in (None, parent.session_id)
            return (
                archive,
                parent.id,
                link.child_session_id,
                record.body,
                read_artifact(store.root, record.body),
            )
        finally:
            await context.stop()

    archive, parent_id, child_id, artifact, prompt = asyncio.run(make_archive())
    key = new_id()
    with opened(tmp_path / "target") as store:
        root = import_session(store, archive, key)
        assert import_session(store, archive, key) == root
        assert import_session(store, archive, new_id()) != root
        with pytest.raises(TransferConflict):
            import_session(store, archive + b"other", key)
        session = store.session(root)
        assert session.title == "A private survey" and session.archived and session.pinned
        ((_, parent),) = store.runs(root)
        assert parent.id != parent_id and store.request(parent.request_id) is None
        link = get_link(store, parent.id)
        assert link.child_session_id != child_id
        child = store.session(link.child_session_id)
        assert child.mode == SessionMode.ISOLATED and child.parent_session_id == root
        assert store.run(link.child_run_id).parent_tool_call_id == link.id
        request = next(r for r in records(store, link.child_run_id) if isinstance(r, ModelRequest))
        assert request.body.path != artifact.path
        assert read_artifact(store.root, request.body) == prompt
        assert search(store.root, "Worker report", session_id=child.id)
    with opened(tmp_path / "target") as store:
        assert import_session(store, archive, key) == root
        reexported = export_session(store, root)
    with opened(tmp_path / "third") as store:
        third = import_session(store, reexported, new_id())
        ((_, parent),) = store.runs(third)
        assert store.session(get_link(store, parent.id).child_session_id).parent_session_id == third


def test_compacted_import_continues_and_can_compact_again(
    profile: EffectiveProfile, tmp_path: Path
) -> None:
    async def scenario() -> bytes:
        async def respond(body: dict[str, Any]) -> bytes:
            summary = material(body)
            return summary_reply(summary) if summary else wire(reply("A visible reply."))

        source, source_prompts = harness(compact_profile(profile), respond)
        await source.start()
        assert source.store and source.runs
        try:
            session = source.store.create_session()
            seed(source.store, session.id)
            run = source.runs.submit(
                session.id,
                profile=source.profile,
                content="Source continuation.",
                request_id=new_id(),
            )
            assert (await source.runs.wait(run.id, timeout=5)).state == RunState.COMPLETED
            anchor = active_boundary(source.store, session.id)
            assert anchor and anchor.manifest
            summary = source.store._load("message", anchor.summary_message_id, Message)
            source_hash = anchor.manifest.sha256
            archive = export_session(source.store, session.id)
            assert any(material(body) for body in source_prompts)
        finally:
            await source.stop()

        target, prompts = harness(
            compact_profile(profile).model_copy(
                update={"data_dir": tmp_path / "copy", "model_context_tokens": 21500}
            ),
            respond,
        )
        await target.start()
        assert target.store and target.runs
        runs = target.runs
        try:
            root = import_session(target.store, archive, new_id())
            imported = active_boundary(target.store, root)
            assert imported and imported.manifest and imported.manifest.sha256 == source_hash
            assert imported.id != anchor.id

            async def submit(content: str) -> None:
                run = runs.submit(
                    root, profile=target.profile, content=content, request_id=new_id()
                )
                outcome = await runs.wait(run.id, timeout=5)
                assert outcome.state == RunState.COMPLETED, outcome.reason

            await submit("Continue the copied topic.")
            assert any(m["content"] == summary.content for m in prompts[-1]["messages"])
            assert any(m["content"] == "Source continuation." for m in prompts[-1]["messages"])
            assert not any(m["content"].startswith("Fact 0:") for m in prompts[-1]["messages"])
            seed(target.store, root, count=5)
            await submit("Compact the copied topic again.")
            latest = active_boundary(target.store, root)
            assert latest and latest.id != imported.id
            assert manifest_for(target.store, latest).prior_boundary_id == imported.id
            return export_session(target.store, root)
        finally:
            await target.stop()

    reexported = asyncio.run(scenario())
    with opened(tmp_path / "second-copy") as store:
        imported_root = import_session(store, reexported, new_id())
        assert active_boundary(store, imported_root) is not None


@pytest.mark.parametrize("state", [MemoryState.PENDING, MemoryState.RUNNING])
def test_pending_historical_operations_stay_inert_across_restart(
    profile: EffectiveProfile, tmp_path: Path, state: MemoryState
) -> None:
    with opened(tmp_path / "source") as store:
        topic = store.create_session()
        pixels = png(80, 64)
        attachment = Artifact.model_validate(
            store.artifact(new_id(), pixels) | {"media_type": "image/png"}
        )
        run = store.admit(
            topic.id, request_id=new_id(), content="History", profile="default", image=attachment
        )
        store.transition_run(run.id, RunState.RUNNING)
        step = ModelStep(
            origin=run.origin, session_id=topic.id, run_id=run.id, index=1, model="test"
        )
        call = ToolCall(
            origin=run.origin,
            session_id=topic.id,
            run_id=run.id,
            model_step_id=step.id,
            name="image.generate",
            provider_tool_call_id="image",
            arguments={},
        )
        store.append(step)
        store.append(call)
        # Distinct evidence references may contain identical bytes with different media types.
        body = Artifact.model_validate(store.artifact(new_id(), pixels))
        image = ImageGeneration(
            origin=run.origin,
            session_id=topic.id,
            run_id=run.id,
            tool_call_id=call.id,
            endpoint="http://127.0.0.1:8188",
            workflow_sha256="a",
            parameters={},
            fingerprint="b",
            body=body,
            requested_id=new_id(),
        )
        memory = MemoryCommit(origin=run.origin, session_id=topic.id, run_id=run.id)
        store.append(memory)
        if state == MemoryState.RUNNING:
            store.transition_memory(memory.id, state)
        delivery = Delivery(
            origin=run.origin,
            session_id=topic.id,
            run_id=run.id,
            message_id=new_id(),
            content="An unpublished notification",
        )
        store._commit((image, delivery))
        store.transition_run(run.id, RunState.COMPLETED)
        archive = export_session(store, topic.id)
    with opened(tmp_path / "target") as store:
        root = import_session(store, archive, new_id())

    async def restart() -> None:
        context, prompts = harness(profile.model_copy(update={"data_dir": tmp_path / "target"}), [])
        assert context.store and context.memory and context.jobs
        await context.start()
        store = context.store
        try:
            await context.memory.tick()
            context.jobs.deliver()
            ((_, run),) = store.runs(root)
            assert store.imported_run(run.id) and not store.queued_runs() and not prompts
            history = records(store, run.id)
            generation = next(r for r in history if isinstance(r, ImageGeneration))
            image = next(r.image for r in history if isinstance(r, Message) and r.image)
            assert generation.state == "intent"
            assert image.sha256 == generation.body.sha256 and image.path != generation.body.path
            assert read_artifact(store.root, image) == pixels
            historical_memory = next(r for r in history if isinstance(r, MemoryCommit))
            assert historical_memory.state == state
            assert next(r for r in history if isinstance(r, Delivery)).state == "pending"
            assert ImageService(store, ImageSettings()).unfinished() == []
            with pytest.raises(ValueError, match="historical"):
                store.admitted_profile(run.id)
            with pytest.raises(ValueError, match="historical"):
                store.transition_memory(historical_memory.id, MemoryState.AMBIGUOUS)
            assert not store.db.execute("SELECT 1 FROM entities WHERE kind='receipt'").fetchone()
        finally:
            await context.stop()

    asyncio.run(restart())


def test_scheduled_history_carries_occurrence_and_visible_delivery(
    profile: EffectiveProfile, tmp_path: Path
) -> None:
    async def archive_job() -> bytes:
        context, _ = harness(profile, [reply("Published result.")])
        assert context.store and context.jobs and context.runs
        clock = Clock()
        context.jobs.clock = clock
        await context.start()
        try:
            definition = job(context, clock)
            occurrence = context.jobs.manual(definition.id, request_id=new_id())
            assert occurrence.run_id and definition.session_id
            outcome = await context.runs.wait(occurrence.run_id, timeout=5)
            assert outcome.state == RunState.COMPLETED
            return export_session(context.store, definition.session_id)
        finally:
            await context.stop()

    archive = asyncio.run(archive_job())
    with opened(tmp_path / "target") as store:
        root = import_session(store, archive, new_id())
        assert [message.content for _, message in store.messages(root)] == ["Published result."]
        assert not store.jobs()
        assert not store.db.execute("SELECT 1 FROM entities WHERE kind='occurrence'").fetchone()
        with ZipFile(io.BytesIO(export_session(store, root))) as bundle:
            rows = [json.loads(row) for row in bundle.read(PROVENANCE).splitlines()]
            assert any(row["entry"]["kind"] == "occurrence" for row in rows)


@pytest.mark.parametrize("interruption", ["state_commit", "outbox_complete"])
def test_invalid_link_and_outbox_failure_are_atomic(tmp_path: Path, interruption: str) -> None:
    with opened(tmp_path / "source") as store:
        topic = store.create_session()
        run = store.admit(topic.id, request_id=new_id(), content="History", profile="default")
        with pytest.raises(TransferConflict):
            export_session(store, topic.id)
        store.transition_run(run.id, RunState.CANCELLED)
        store.update_session(topic.id, archived=True)
        archive = export_session(store, topic.id)
    with ZipFile(io.BytesIO(archive)) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    rows = [json.loads(line) for line in parts[EVIDENCE].splitlines()]
    row = next(row for row in rows if row["entry"]["kind"] == "message")
    row["entry"]["record"]["run_id"] = row["entry"]["event"]["run_id"] = new_id()
    entry = canonical(row["entry"]) + b"\n"
    row["reference"].update(length=len(entry), sha256=digest(entry))
    parts[EVIDENCE] = b"".join(canonical(row) + b"\n" for row in rows)
    manifest = json.loads(parts[MANIFEST])
    manifest["members"][EVIDENCE] = {
        "length": len(parts[EVIDENCE]),
        "sha256": digest(parts[EVIDENCE]),
    }
    parts[MANIFEST] = canonical(manifest)
    broken = io.BytesIO()
    with ZipFile(broken, "w") as output:
        for name, part in parts.items():
            output.writestr(name, part)
    with opened(tmp_path / "target") as store:
        with pytest.raises(ValueError, match="lineage"):
            import_session(store, broken.getvalue(), new_id())
        assert store.list_sessions() == []

    hits = 0

    def fail(stage: str) -> None:
        nonlocal hits
        if stage == interruption:
            hits += 1
        if stage == interruption and (stage == "state_commit" or hits == 3):
            raise RuntimeError("simulated interruption")

    key = new_id()
    with opened(tmp_path / "target", fault=fail) as store:
        with pytest.raises(RuntimeError, match="interruption"):
            import_session(store, archive, key)
    with opened(tmp_path / "target") as store:
        copy = import_session(store, archive, key)
        assert [session.id for session in store.list_sessions(archived=True)] == [copy]
        assert [message.content for _, message in store.messages(copy)] == ["History"]
