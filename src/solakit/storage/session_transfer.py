"""Portable session evidence and independent historical copies."""

import io
import re
import stat
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, Literal
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from pydantic import Field, model_validator

from solakit.engine.compaction import Manifest, source_hash
from solakit.records import (
    RUN_TRANSITIONS,
    Artifact,
    CompactionBoundary,
    Delegation,
    Delivery,
    DeliveryState,
    DurableEvent,
    MemoryCommit,
    Message,
    ModelRequest,
    ModelStep,
    Origin,
    Record,
    RecordID,
    RecoveryNote,
    Run,
    RunState,
    Session,
    ToolCall,
    Value,
    new_id,
)
from solakit.storage.evidence import (
    RECORDS,
    CorruptionError,
    Entry,
    Reference,
    canonical,
    checked_json,
    digest,
    ensure_directory,
    publish,
    read_artifact,
)
from solakit.storage.store import TRANSFER_RUN, TRANSFER_SOURCE, Store, valid_id

TRANSFER_BYTES = 128 * 1024 * 1024
TRANSFER_ITEMS = 50_000
MANIFEST = "manifest.json"
EVIDENCE = "evidence.jsonl"
PROVENANCE = "provenance.jsonl"
AUXILIARY = {"legacy", "occurrence", "letter_publication"}
SESSION_KEY = "session-transfer:session:"
REQUEST_KEY = "session-transfer:request:"
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ASSET = re.compile(r"artifacts/[0-9a-f]{64}")


class TransferLimit(ValueError):
    pass


class TransferConflict(ValueError):
    pass


class Member(Value):
    length: Annotated[int, Field(ge=0)]
    sha256: SHA256


class Bundle(Value):
    format: Literal["solakit-session"] = "solakit-session"
    version: Literal[1] = 1
    session_id: RecordID
    members: dict[str, Member]


class EvidenceRow(Value):
    # The bundle scope may identify a copy while the entry still has its source IDs.
    session_id: RecordID
    reference: Reference
    entry: Entry

    @model_validator(mode="after")
    def verify_reference(self) -> EvidenceRow:
        data = self.entry.encoded()
        if (
            self.reference.entry_id != self.entry.id
            or self.reference.length != len(data)
            or self.reference.sha256 != digest(data)
        ):
            raise ValueError("evidence reference mismatch")
        return self

    def record(self) -> Record:
        return RECORDS[self.entry.kind].model_validate_json(canonical(self.entry.record))


class ImportReceipt(Value):
    sha256: SHA256
    session_id: RecordID


def _jsonl(rows: Iterable[EvidenceRow]) -> bytes:
    result = io.BytesIO()
    for row in rows:
        result.write(canonical(row.model_dump(mode="json")) + b"\n")
        if result.tell() > TRANSFER_BYTES:
            raise TransferLimit("Topic archive exceeds 128 MiB")
    return result.getvalue()


def _rows(raw: bytes, limit: int) -> list[EvidenceRow]:
    result: list[EvidenceRow] = []
    for line in io.BytesIO(raw):
        if len(result) >= limit:
            raise TransferLimit("Topic archive has too many records or artifacts")
        checked_json(line)
        result.append(EvidenceRow.model_validate_json(line))
    return result


def _asset_id(sha256: str, media_type: str) -> str:
    # Imported artifacts can share immutable content across independent topic copies.
    return digest(canonical(["solakit-session-artifact", sha256, media_type]))[:32]


def _references(record: Record) -> Iterator[Artifact]:
    for field in type(record).model_fields:
        if isinstance(value := getattr(record, field), Artifact):
            yield value
    if isinstance(record, Message):
        yield from record.documents


def _scoped_rows(store: Store, root: str) -> tuple[list[EvidenceRow], list[EvidenceRow]]:
    store.session(root)
    store._flush()
    sessions = {root}
    pending = [root]
    while pending:
        session_id = pending.pop()
        for row in store.db.execute(
            "SELECT id FROM entities WHERE kind='delegation' AND session_id=?", (session_id,)
        ):
            child = store._load("delegation", row[0], Delegation).child_session_id
            if child not in sessions:
                if len(sessions) >= TRANSFER_ITEMS:
                    raise TransferLimit("Topic archive has too many records or artifacts")
                sessions.add(child)
                pending.append(child)
    scope = canonical(sorted(sessions)).decode()
    if store.db.execute(
        "SELECT 1 FROM entities WHERE kind='run' "
        "AND session_id IN (SELECT value FROM json_each(?)) "
        "AND json_extract(body,'$.state') IN ('queued','running') LIMIT 1",
        (scope,),
    ).fetchone():
        raise TransferConflict("Wait for queued and running turns to finish before exporting")

    primary: list[EvidenceRow] = []
    provenance: list[EvidenceRow] = []
    evidence_bytes = 0
    # Occurrences and publication ledgers are historical attachments to the topic.
    # Their original records never become destination job or effect state.
    for ref_json, session_id in store.db.execute(
        """
        WITH scoped AS (SELECT value AS id FROM json_each(?)),
        linked AS (
            SELECT 'occurrence' AS kind, json_extract(body,'$.occurrence_id') AS id, session_id
            FROM entities WHERE kind='run' AND session_id IN scoped
            UNION ALL
            SELECT 'letter_publication', publication.id, tool.session_id
            FROM entities AS publication JOIN entities AS tool
            ON tool.id=json_extract(publication.body,'$.tool_call_id')
            WHERE publication.kind='letter_publication' AND tool.kind='tool'
            AND tool.session_id IN scoped
        ),
        selected AS (
            SELECT ordinal, reference, json_extract(body,'$.event.session_id') AS session_id
            FROM outbox WHERE delivered=1 AND json_extract(body,'$.event.session_id') IN scoped
            UNION ALL
            SELECT ordinal, reference, json_extract(body,'$.record.id') FROM outbox
            WHERE delivered=1 AND json_extract(body,'$.kind')='session'
            AND json_extract(body,'$.record.id') IN scoped
            UNION ALL
            SELECT ordinal, reference, json_extract(body,'$.details.session_id') FROM outbox
            WHERE delivered=1 AND json_extract(body,'$.kind')='legacy'
            AND json_extract(body,'$.details.session_id') IN scoped
            UNION ALL
            SELECT ordinal, reference, linked.session_id FROM outbox JOIN linked
            ON linked.kind=json_extract(outbox.body,'$.kind')
            AND linked.id=json_extract(outbox.body,'$.record.id') WHERE delivered=1
        )
        SELECT reference,session_id FROM selected ORDER BY ordinal LIMIT ?
        """,
        (scope, TRANSFER_ITEMS + 1),
    ):
        if len(primary) + len(provenance) >= TRANSFER_ITEMS:
            raise TransferLimit("Topic archive has too many records or artifacts")
        ref = Reference.model_validate_json(ref_json)
        evidence_bytes += ref.length
        if evidence_bytes > TRANSFER_BYTES:
            raise TransferLimit("Topic archive exceeds 128 MiB")
        entry = store.log.read(ref)
        item = EvidenceRow(session_id=session_id, reference=ref, entry=entry)
        (provenance if entry.kind in AUXILIARY else primary).append(item)

    for session_id in sorted(sessions):
        row = store.db.execute(
            "SELECT fingerprint FROM imports WHERE source=?", (SESSION_KEY + session_id,)
        ).fetchone()
        if row:
            fingerprint = row[0]
            with _provenance_path(store, fingerprint).open("rb") as stream:
                raw = stream.read(TRANSFER_BYTES - evidence_bytes + 1)
            evidence_bytes += len(raw)
            if evidence_bytes > TRANSFER_BYTES:
                raise TransferLimit("Topic archive exceeds 128 MiB")
            if digest(raw) != fingerprint:
                raise CorruptionError("imported provenance checksum mismatch")
            provenance.extend(_rows(raw, TRANSFER_ITEMS - len(primary) - len(provenance)))
    return primary, provenance


def _provenance_path(store: Store, sha256: str) -> Path:
    if not ASSET.fullmatch("artifacts/" + sha256):
        raise CorruptionError("invalid imported provenance reference")
    path = store.root / "imports" / "session-transfer" / sha256
    if path.is_symlink() or path.parent.is_symlink():
        raise CorruptionError("imported provenance cannot be a symlink")
    return path


def _artifact_references(rows: Iterable[EvidenceRow]) -> Iterator[Artifact]:
    for row in rows:
        if row.entry.kind in RECORDS:
            yield from _references(row.record())


def _compaction_manifests(rows: Iterable[EvidenceRow]) -> Iterator[Artifact]:
    for row in rows:
        if row.entry.kind == "compaction":
            boundary = row.record()
            assert isinstance(boundary, CompactionBoundary)
            if boundary.manifest is not None:
                yield boundary.manifest


def export_session(store: Store, session_id: str) -> bytes:
    primary, provenance = _scoped_rows(store, session_id)
    members: dict[str, Member] = {}
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:

        def write(name: str, raw: bytes) -> None:
            if output.tell() + len(raw) > TRANSFER_BYTES:
                raise TransferLimit("Topic archive exceeds 128 MiB")
            members[name] = Member(length=len(raw), sha256=digest(raw))
            archive.writestr(ZipInfo(name), raw)

        write(EVIDENCE, _jsonl(primary))
        write(PROVENANCE, _jsonl(provenance))
        verified: set[tuple[str, str]] = set()

        def save(reference: Artifact, *, historical: bool = False) -> str:
            name = f"artifacts/{reference.sha256}"
            if name in members and members[name].length != reference.length:
                raise CorruptionError("inconsistent artifact length")
            if historical and name in members:
                return name
            if (reference.path, reference.sha256) not in verified or name not in members:
                if reference.length > TRANSFER_BYTES:
                    raise TransferLimit("Topic archive exceeds 128 MiB")
                if historical:
                    reference = reference.model_copy(
                        update={
                            "path": f"artifacts/{_asset_id(reference.sha256, reference.media_type)}"
                        }
                    )
                data = read_artifact(store.root, reference)
                verified.add((reference.path, reference.sha256))
                if name not in members:
                    if len(primary) + len(provenance) + len(members) - 2 >= TRANSFER_ITEMS:
                        raise TransferLimit("Topic archive has too many records or artifacts")
                    write(name, data)
            return name

        for reference in _artifact_references(primary):
            save(reference)
        for reference in _artifact_references(provenance):
            # Native auxiliary records have no artifacts. Copied source records can
            # refer to their original path, but the destination owns the verified bytes.
            save(reference, historical=True)
        manifests = set()
        for rows, historical in ((primary, False), (provenance, True)):
            for row in rows:
                for reference in _compaction_manifests((row,)):
                    copied = historical or TRANSFER_SOURCE in row.entry.details
                    if (reference.sha256, copied) not in manifests:
                        manifest = Manifest.model_validate_json(
                            archive.read(f"artifacts/{reference.sha256}")
                        )
                        save(manifest.summary, historical=copied)
                        manifests.add((reference.sha256, copied))
        manifest_bytes = canonical(
            Bundle(session_id=session_id, members=members).model_dump(mode="json")
        )
        archive.writestr(ZipInfo(MANIFEST), manifest_bytes)
    if output.tell() > TRANSFER_BYTES:
        raise TransferLimit("Topic archive exceeds 128 MiB")
    return output.getvalue()


def _unpack(raw: bytes) -> tuple[Bundle, list[EvidenceRow], list[EvidenceRow], dict[str, bytes]]:
    if len(raw) > TRANSFER_BYTES:
        raise TransferLimit("Topic archive exceeds 128 MiB")
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if (
                len(members) > TRANSFER_ITEMS + 3
                or sum(m.file_size for m in members) > TRANSFER_BYTES
            ):
                raise TransferLimit("Topic archive exceeds the transfer limits")
            names = [m.filename for m in members]
            if (
                len(set(names)) != len(names)
                or not {MANIFEST, EVIDENCE, PROVENANCE}.issubset(names)
                or any(
                    m.filename != m.orig_filename
                    or m.is_dir()
                    or m.compress_type != ZIP_STORED
                    or m.compress_size != m.file_size
                    or m.flag_bits & 1
                    or stat.S_IFMT(m.external_attr >> 16) not in (0, stat.S_IFREG)
                    or (
                        m.filename not in (MANIFEST, EVIDENCE, PROVENANCE)
                        and not ASSET.fullmatch(m.filename)
                    )
                    for m in members
                )
            ):
                raise ValueError("unexpected archive member")
            raw_manifest = archive.read(MANIFEST)
            manifest_json = checked_json(raw_manifest)
            if (
                not isinstance(manifest_json, dict)
                or manifest_json.get("format") != "solakit-session"
                or type(manifest_json.get("version")) is not int
                or manifest_json["version"] != 1
            ):
                raise ValueError("unsupported archive format or version")
            manifest = Bundle.model_validate_json(raw_manifest)
            if set(manifest.members) != set(names) - {MANIFEST}:
                raise ValueError("archive manifest member mismatch")
            files = {}
            for name, metadata in manifest.members.items():
                data = archive.read(name)
                if (
                    len(data) != metadata.length
                    or digest(data) != metadata.sha256
                    or (name.startswith("artifacts/") and name.split("/")[1] != metadata.sha256)
                ):
                    raise ValueError("archive member checksum mismatch")
                files[name] = data
    except (BadZipFile, EOFError, NotImplementedError, RuntimeError) as error:
        raise ValueError("invalid ZIP archive") from error
    limit = TRANSFER_ITEMS - (len(files) - 2)
    primary = _rows(files.pop(EVIDENCE), limit)
    provenance = _rows(files.pop(PROVENANCE), limit - len(primary))
    return manifest, primary, provenance, {name.split("/")[1]: raw for name, raw in files.items()}


def _validate_graph(
    root: str, primary: list[EvidenceRow], provenance: list[EvidenceRow]
) -> dict[tuple[str, str], Record]:
    sequences: dict[str, int] = defaultdict(int)
    records: dict[tuple[str, str], Record] = {}
    children: dict[str, set[str]] = defaultdict(set)
    entry_ids: set[str] = set()
    for row in primary:
        entry = row.entry
        if entry.id in entry_ids or entry.kind in AUXILIARY | {"job", "scheduler"}:
            raise ValueError("unexpected or duplicate projected record")
        entry_ids.add(entry.id)
        record = row.record()
        key = (entry.kind, record.id)
        previous = records.get(key)
        if isinstance(record, Session):
            if row.session_id != record.id or (
                isinstance(previous, Session)
                and previous.model_copy(
                    update={
                        "title": record.title,
                        "archived": record.archived,
                        "pinned": record.pinned,
                    }
                )
                != record
            ):
                raise ValueError("session history disagrees")
        else:
            event = entry.event
            if (
                event is None
                or event.session_id != row.session_id
                or ("session", row.session_id) not in records
            ):
                raise ValueError("record has no owning session")
            sequences[row.session_id] += 1
            if event.sequence != sequences[row.session_id]:
                raise ValueError("session event sequence has a gap")
            if isinstance(record, Run):
                if isinstance(previous, Run):
                    transition_at = (
                        record.started_at if record.state in RUN_TRANSITIONS else record.ended_at
                    )
                    if (
                        transition_at is None
                        or previous.transition(record.state, at=transition_at, reason=record.reason)
                        != record
                    ):
                        raise ValueError("run transition differs from its admission")
                elif record.state != RunState.QUEUED and (
                    record.origin != Origin.SYSTEM or record.state in RUN_TRANSITIONS
                ):
                    raise ValueError("run admission is missing")
            else:
                owner = records.get(("run", event.run_id))
                # The delegation admission link precedes the *child's* run, not its owner's.
                if (
                    not isinstance(owner, Run)
                    or owner.session_id != event.session_id
                    or owner.origin != record.origin
                    or (
                        previous is not None
                        and (
                            entry.kind not in {"delivery", "memory", "image", "compaction"}
                            or previous.created_at != record.created_at
                            or previous.origin != record.origin
                            or getattr(previous, "run_id", None) != event.run_id
                            or getattr(previous, "session_id", None) != event.session_id
                        )
                    )
                ):
                    raise ValueError("record crosses run lineage")
        records[key] = record

    for record in records.values():
        if isinstance(record, Run) and record.state in RUN_TRANSITIONS:
            raise ValueError("archive includes an active run")
        if isinstance(record, Delegation):
            child = records.get(("session", record.child_session_id))
            run = records.get(("run", record.child_run_id))
            tool = records.get(("tool", record.id))
            if (
                not isinstance(child, Session)
                or child.parent_session_id != record.session_id
                or not isinstance(run, Run)
                or run.session_id != child.id
                or run.parent_tool_call_id != record.id
                or not isinstance(tool, ToolCall)
                or tool.run_id != record.run_id
                or tool.name != "delegate.run"
            ):
                raise ValueError("delegation lineage mismatch")
            children[record.session_id].add(child.id)
        _validate_links(record, records)

    scope: set[str] = set()
    pending = [root]
    while pending:
        identity = pending.pop()
        if identity not in scope:
            scope.add(identity)
            pending.extend(children[identity])
    if scope != {record.id for record in records.values() if isinstance(record, Session)} or any(
        row.session_id not in scope or row.entry.kind in {"job", "scheduler"} for row in provenance
    ):
        raise ValueError("archive contains evidence outside the selected topic")
    return records


def _validate_links(record: Record, records: dict[tuple[str, str], Record]) -> None:
    run_id = getattr(record, "run_id", None)

    def same_run(kind: str, identity: str) -> None:
        parent = records.get((kind, identity))
        if parent is None or getattr(parent, "run_id", None) != run_id:
            raise ValueError("record link crosses its run")

    if isinstance(record, Run) and record.retry_of:
        previous = records.get(("run", record.retry_of))
        if not isinstance(previous, Run) or previous.session_id != record.session_id:
            raise ValueError("retry link crosses its topic")
    if isinstance(record, ModelStep) and record.request_id:
        same_run("request", record.request_id)
        request = records["request", record.request_id]
        assert isinstance(request, ModelRequest)
        if (request.index, request.model, request.purpose) != (
            record.index,
            record.model,
            record.purpose,
        ):
            raise ValueError("model response differs from request")
    if isinstance(record, (ToolCall, RecoveryNote)):
        same_run("model", record.model_step_id)
    if (call_id := getattr(record, "tool_call_id", None)) is not None:
        same_run("tool", call_id)
    if isinstance(record, Delivery) and (
        record.state == DeliveryState.DELIVERED or record.content is None
    ):
        same_run("message", record.message_id)
    if isinstance(record, MemoryCommit):
        attempts: set[object] = set()
        attempt: str | None = record.run_id
        while attempt is not None and attempt not in attempts:
            attempts.add(attempt)
            owner = records.get(("run", attempt))
            attempt = owner.retry_of if isinstance(owner, Run) else None
        for kind, identities in (("message", record.source_ids), ("memory_note", record.note_ids)):
            for identity in identities:
                parent = records.get((kind, identity))
                if parent is None or getattr(parent, "run_id", None) not in attempts:
                    raise ValueError("record link crosses its run")
    if isinstance(record, CompactionBoundary):
        if record.through_message_id != record.source_message_ids[-1]:
            raise ValueError("compaction boundary differs from source range")
        for identity in record.source_message_ids:
            message = records.get(("message", identity))
            if not isinstance(message, Message) or message.session_id != record.session_id:
                raise ValueError("compaction source crosses its topic")
        if record.state == "committed":
            same_run("message", record.summary_message_id)


def _validate_artifacts(
    primary: list[EvidenceRow],
    provenance: list[EvidenceRow],
    assets: dict[str, bytes],
    records: dict[tuple[str, str], Record],
) -> tuple[set[str], dict[str, set[str]]]:
    def artifact(reference: Artifact) -> bytes:
        data = assets.get(reference.sha256)
        if data is None or len(data) != reference.length:
            raise ValueError("linked artifact is missing or has the wrong size")
        media_types[reference.sha256].add(reference.media_type)
        return data

    media_types: dict[str, set[str]] = defaultdict(set)
    all_rows = (*primary, *provenance)
    by_hash = {row.reference.sha256: row for row in all_rows}
    by_record: dict[tuple[str, str], EvidenceRow] = {}
    current_messages = {
        row.entry.record["id"]: row for row in primary if row.entry.kind == "message"
    }
    for row in all_rows:
        if row.entry.kind in RECORDS:
            by_record[row.entry.kind, str(row.entry.record["id"])] = row
    for reference in _artifact_references(all_rows):
        artifact(reference)
    manifests: dict[str, Manifest] = {}
    for reference in _compaction_manifests(all_rows):
        if reference.sha256 not in manifests:
            manifest = Manifest.model_validate_json(artifact(reference))
            artifact(manifest.summary)
            manifests[reference.sha256] = manifest
    for row in primary:
        if row.entry.kind != "compaction":
            continue
        boundary = row.record()
        assert isinstance(boundary, CompactionBoundary)
        if boundary.state != "committed" or boundary.manifest is None:
            continue
        manifest = manifests[boundary.manifest.sha256]
        original = by_hash.get(str(row.entry.details.get(TRANSFER_SOURCE, row.reference.sha256)))
        if original is None:
            raise ValueError("compaction source boundary is missing")
        original_boundary = original.record()
        if not isinstance(original_boundary, CompactionBoundary) or (
            original_boundary.id != manifest.boundary_id
            or original_boundary.state != "committed"
            or original_boundary.manifest is None
            or original_boundary.manifest.sha256 != boundary.manifest.sha256
        ):
            raise ValueError("compaction source boundary differs from its manifest")
        _validate_summary(
            boundary,
            original_boundary,
            manifest,
            artifact(manifest.summary),
            by_hash,
            current_messages,
        )
        step_row = by_record.get(("model", manifest.model_step_id))
        step = step_row.record() if step_row else None
        request_row = (
            by_record.get(("request", step.request_id))
            if (isinstance(step, ModelStep) and step.request_id is not None)
            else None
        )
        request = request_row.record() if request_row else None
        if (
            not isinstance(step, ModelStep)
            or step.run_id != original_boundary.run_id
            or step.purpose != "compaction"
            or step.tool_calls
            or step.failure
            or step.model != manifest.model
            or not isinstance(request, ModelRequest)
            or request.run_id != step.run_id
            or request.body.sha256 != manifest.prompt_sha256
        ):
            raise ValueError("compaction request or response evidence is missing")
        generated = checked_json(step.content.encode())
        prompt = checked_json(artifact(request.manifest))
        summary = records["message", boundary.summary_message_id]
        assert isinstance(summary, Message)
        if (
            generated
            != {
                "through_run_id": manifest.source_run_ids[-1],
                "source_sha256": manifest.source_sha256,
                "summary": summary.content,
            }
            or not isinstance(prompt, dict)
            or prompt.get("purpose") != "compaction"
            or prompt.get("source_sha256") != manifest.source_sha256
            or prompt.get("source_run_ids") != list(manifest.source_run_ids)
        ):
            raise ValueError("compaction response differs from its frozen request")
    if media_types.keys() != assets.keys():
        raise ValueError("archive contains unlinked artifacts")
    source_hashes = {
        reference.sha256 for manifest in manifests.values() for reference in manifest.sources
    }
    return source_hashes, media_types


def _validate_summary(
    boundary: CompactionBoundary,
    original: CompactionBoundary,
    manifest: Manifest,
    summary_bytes: bytes,
    by_hash: dict[str, EvidenceRow],
    current_messages: dict[object, EvidenceRow],
) -> None:
    def source_hash_for(row: EvidenceRow) -> object:
        return row.entry.details.get(TRANSFER_SOURCE, row.reference.sha256)

    source_run_ids = set(manifest.source_run_ids)
    source_runs = []
    source_messages: list[EvidenceRow] = []
    for reference in manifest.sources:
        source = by_hash.get(reference.sha256)
        if (
            source is None
            or source.reference != reference
            or source.entry.event is None
            or (
                source.entry.event.session_id != original.session_id
                or source.entry.event.run_id not in source_run_ids
            )
        ):
            raise ValueError("compaction evidence reference disagrees")
        if source.entry.kind == "run":
            source_runs.append(source.entry.event.run_id)
        if source.entry.kind == "message":
            source_messages.append(source)
    summary = Message.model_validate_json(summary_bytes)
    projected = current_messages[boundary.summary_message_id].record()
    projected_sources = [current_messages[identity] for identity in boundary.source_message_ids]
    sequences = [row.entry.event.sequence for row in projected_sources if row.entry.event]
    if (
        manifest.version != 1
        or not source_runs
        or tuple(source_runs) != manifest.source_run_ids
        or source_hash(manifest.sources) != manifest.source_sha256
        or tuple(row.entry.record["id"] for row in source_messages) != original.source_message_ids
        or [source_hash_for(row) for row in projected_sources]
        != [source_hash_for(row) for row in source_messages]
        or sequences != sorted(set(sequences))
        or original.through_message_id != original.source_message_ids[-1]
        or summary.id != original.summary_message_id
        or summary.session_id != original.session_id
        or summary.run_id != original.run_id
        or summary.origin != original.origin
        or summary.role != "assistant"
        or not summary.content.strip()
        or not 0 < len(summary.content.encode()) <= manifest.summary_byte_limit <= 4096
        or not isinstance(projected, Message)
        or projected.content != summary.content
        or projected.role != "assistant"
    ):
        raise ValueError("compaction summary or source range differs")


LINK_FIELDS = {
    "session": ("parent_session_id",),
    "run": ("request_id", "job_id", "occurrence_id", "retry_of", "parent_tool_call_id"),
    "message": ("tool_call_id",),
    "model": ("request_id",),
    "tool": ("model_step_id",),
    "receipt": ("tool_call_id",),
    "delivery": ("message_id",),
    "delegation": ("child_session_id", "child_run_id"),
    "recovery": ("model_step_id",),
    "memory_note": ("tool_call_id",),
    "image": ("tool_call_id",),
    "compaction": ("through_message_id", "summary_message_id", "source_message_ids"),
    "memory": ("source_ids", "note_ids"),
}


def _project(row: EvidenceRow, remap: dict[str, str]) -> Entry:
    def mapped(value: object) -> object:
        if isinstance(value, list):
            return [mapped(item) for item in value]
        if isinstance(value, str):
            if value not in remap:
                remap[value] = new_id()
            return remap[value]
        return value

    entry = row.entry
    record = row.record()
    projected = record.model_dump(mode="json")
    for field in ("id", "session_id", "run_id", *LINK_FIELDS.get(entry.kind, ())):
        if field in projected:
            projected[field] = mapped(projected[field])
    for field in type(record).model_fields:
        if isinstance(reference := getattr(record, field), Artifact):
            identity = _asset_id(reference.sha256, reference.media_type)
            projected[field]["path"] = f"artifacts/{identity}"
    if isinstance(record, Message):
        for document, reference in zip(
            projected.get("documents", []), record.documents, strict=True
        ):
            document["path"] = f"artifacts/{_asset_id(reference.sha256, reference.media_type)}"
    identity = new_id()
    event = None
    if entry.event is not None:
        event = DurableEvent.model_validate(
            entry.event.model_dump()
            | {
                "event_id": identity,
                "session_id": mapped(entry.event.session_id),
                "run_id": mapped(entry.event.run_id),
                "payload": {"kind": entry.kind, "record_id": projected["id"]},
            }
        )
    source = entry.details.get(TRANSFER_SOURCE, row.reference.sha256)
    return Entry(
        id=identity,
        kind=entry.kind,
        record=projected,
        event=event,
        details={TRANSFER_SOURCE: source},
    )


def import_session(store: Store, raw: bytes, request_id: str) -> str:
    key = REQUEST_KEY + valid_id(request_id)
    sha256 = digest(raw)
    existing = store.db.execute("SELECT fingerprint FROM imports WHERE source=?", (key,)).fetchone()
    if existing:
        receipt = ImportReceipt.model_validate_json(existing[0])
        if receipt.sha256 != sha256:
            raise TransferConflict("Import key was already used with another archive")
        return store.session(receipt.session_id).id

    manifest, primary, provenance, assets = _unpack(raw)
    records = _validate_graph(manifest.session_id, primary, provenance)
    sources = {row.reference.sha256: row for row in provenance}
    for row in primary:
        if TRANSFER_SOURCE in row.entry.details:
            source = sources.get(str(row.entry.details[TRANSFER_SOURCE]))
            if (
                source is None
                or source.session_id != row.session_id
                or source.entry.kind != row.entry.kind
                or TRANSFER_SOURCE in source.entry.details
            ):
                raise ValueError("copied record lacks its original evidence")
    compaction_sources, media_types = _validate_artifacts(primary, provenance, assets, records)
    remap: dict[str, str] = {}
    projected = [_project(row, remap) for row in primary]
    provenance_by_session: dict[str, dict[str, EvidenceRow]] = defaultdict(dict)
    for row in (*provenance, *primary):
        scope = remap[row.session_id]
        provenance_by_session[scope][row.reference.sha256] = row.model_copy(
            update={
                "session_id": scope,
            }
        )
    for row in primary:
        if TRANSFER_SOURCE in row.entry.details and row.reference.sha256 not in compaction_sources:
            provenance_by_session[remap[row.session_id]].pop(row.reference.sha256, None)
    metadata: dict[str, str] = {}
    session_id = remap[manifest.session_id]
    receipt_json = ImportReceipt(sha256=sha256, session_id=session_id).model_dump_json()
    for scope, rows in provenance_by_session.items():
        data = _jsonl(rows.values())
        fingerprint = digest(data)
        metadata[SESSION_KEY + scope] = fingerprint
        path = _provenance_path(store, fingerprint)
        ensure_directory(path.parent)
        if not path.exists():
            publish(path, data)
        elif path.read_bytes() != data:
            raise CorruptionError("imported provenance checksum mismatch")
    for checksum, data in assets.items():
        for media_type in media_types[checksum]:
            store.artifact(_asset_id(checksum, media_type), data)

    def prepare() -> None:
        store.db.executemany(
            "INSERT INTO sessions VALUES (?,0)", ((scope,) for scope in provenance_by_session)
        )
        store.db.executemany(
            "INSERT INTO imports VALUES (?,?)",
            [
                (key, receipt_json),
                *metadata.items(),
                *(
                    (TRANSFER_RUN + remap[record.id], sha256)
                    for record in records.values()
                    if isinstance(record, Run)
                ),
            ],
        )

    store._commit_entries(projected, prepare)
    return session_id
