"""Per-run and per-session sidecar evidence files (Store mixin)."""

import hashlib
import json
import os
import tempfile
from typing import Any

from solakit.core.contracts import StoreServices
from solakit.core.schemas import (
    MemoryNote,
)
from solakit.core.store_base import (  # noqa: F401,E501
    INDEX_VERSION,
    StorageCorruption,
    atomic_write,
    safe_id,
    sync_directory,
)


def _calibration_sample(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    ratio, samples = value.get("bytes_per_token"), value.get("samples")
    if (
        not isinstance(ratio, (int, float))
        or not 1.5 <= ratio <= 8.0
        or type(samples) is not int
        or not 1 <= samples <= 1000
    ):
        return None
    return value


class SidecarMixin(StoreServices):
    def write_prompt(self, run_id: str, prompt: str) -> str:
        payload = prompt.encode("utf-8")
        # Never redact prompt bytes: this is the exact model input for audit.
        fragments = getattr(prompt, "fragments", None)
        if fragments is not None:
            self._write_run_input(
                run_id, ".prompt-fragments.json", json.dumps(fragments).encode()
            )
        return self._write_run_input(run_id, ".prompt.txt", payload)

    def write_turn_context(self, run_id: str, turn: str) -> str:
        # Exact request-time preamble sent before the user's verbatim text.
        return self._write_run_input(run_id, ".turn.txt", turn.encode("utf-8"))

    def write_tool_catalog(self, run_id: str, catalog_json: str) -> str:
        return self._write_run_input(
            run_id, ".tool-catalog.json", catalog_json.encode()
        )

    def write_tool_schemas(self, run_id: str, schemas: list[dict]) -> str:
        # Input evidence, not diagnostics: preserve schema property names/types.
        payload = json.dumps(schemas, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        return self._write_run_input(run_id, ".tool-schemas.json", payload)

    def _write_run_input(self, run_id: str, suffix: str, payload: bytes) -> str:
        # Readers (run inspection while the run is live) must never observe a
        # created-but-unwritten file, so publish complete bytes with a hard
        # link. Linking still refuses to replace existing immutable evidence.
        path = self._path("runs", run_id, suffix)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(name, path)
        finally:
            os.unlink(name)
        sync_directory(self.root / "runs")
        return hashlib.sha256(payload).hexdigest()

    def stage_memory_note(self, note: MemoryNote) -> None:
        # Notes, like transcript and prompt evidence, are intentionally verbatim.
        self._append(
            self._path("runs", note.run_id, ".notes.jsonl"),
            note.model_dump(mode="json"),
        )

    def memory_notes(self, run_id: str) -> list[dict]:
        path = self._path("runs", run_id, ".notes.jsonl")
        records = self._read(path)
        try:
            for record in records:
                note = MemoryNote.model_validate(record)
                if note.run_id != run_id:
                    raise ValueError("Memory note run identity mismatch")
        except ValueError as exc:
            raise self._corruption(path, "invalid_memory_note") from exc
        return records

    def memory_events(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".memory.jsonl"))

    def log_memory(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".memory.jsonl"), self.redactor.redact(record)
        )

    def tool_log(self, run_id: str) -> list[dict]:
        """Read redacted tool execution evidence for a run."""
        return self._read(self._path("runs", run_id, ".tools.jsonl"))

    def log_tool(self, run_id: str, record: dict[str, Any]) -> None:
        self._append(
            self._path("runs", run_id, ".tools.jsonl"), self.redactor.redact(record)
        )

    def job_state(self, job_id: str) -> dict | None:
        records = self._read(self._path("jobs", job_id, ".jsonl"))
        return records[-1] if records else None

    def save_job(self, job_id: str, record: dict) -> None:
        self._append(self._path("jobs", job_id, ".jsonl"), record)
        self._index("jobs", job_id, record)

    def delivery_events(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".deliveries.jsonl"))

    def log_delivery(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".deliveries.jsonl"),
            self.redactor.redact(record),
        )

    def channel_post_events(self, key: str) -> list[dict]:
        return self._read(self._path("channel-posts", key, ".jsonl"))

    def log_channel_post(self, key: str, record: dict) -> None:
        """Append-only tool-initiated post receipts, separate from run delivery."""
        directory = self.root / "channel-posts"
        directory.mkdir(exist_ok=True, mode=0o700)
        self._append(
            self._path("channel-posts", key, ".jsonl"), self.redactor.redact(record)
        )

    def log_session_note(self, session_id: str, record: dict) -> None:
        # Append-only small facts for one session; survives compaction/archival.
        self.session(session_id)
        self._append(self._path("sessions", session_id, ".notes.jsonl"), record)

    def session_notes(self, session_id: str) -> dict[str, dict]:
        """Latest value per key (deleted keys omitted), in first-set order."""
        notes: dict[str, dict] = {}
        for event in self._read(self._path("sessions", session_id, ".notes.jsonl")):
            key = event["key"]
            if event.get("value") is None:
                notes.pop(key, None)
            else:
                notes[key] = event
        return notes

    def log_plan(self, run_id: str, record: dict) -> None:
        # Append-only, model-authored checklist events for one run.
        self._append(self._path("runs", run_id, ".plan.jsonl"), record)

    def plan_events(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".plan.jsonl"))

    def log_injection(self, run_id: str, record: dict) -> None:
        # Request-time runtime notices: exact text, never transcript messages.
        self._append(self._path("runs", run_id, ".injections.jsonl"), record)

    def injections(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".injections.jsonl"))

    def log_reasoning(self, run_id: str, record: dict) -> None:
        # Model reasoning is run evidence only: never transcript, never resent.
        self._append(
            self._path("runs", run_id, ".reasoning.jsonl"),
            self.redactor.redact(record),
        )

    def reasoning(self, run_id: str) -> list[dict]:
        return self._read(self._path("runs", run_id, ".reasoning.jsonl"))

    def calibration(self, model_id: str) -> dict | None:
        """Measured bytes-per-token for a model (rebuildable, advisory)."""
        path = self.root / "calibration.json"
        try:
            return _calibration_sample(json.loads(path.read_bytes()).get(model_id))
        except (OSError, ValueError, AttributeError):
            return None

    def record_calibration(
        self, model_id: str, request_bytes: int, prompt_tokens: int
    ) -> dict | None:
        if prompt_tokens <= 0 or request_bytes <= 0:
            return None
        ratio = min(8.0, max(1.5, request_bytes / prompt_tokens))
        path = self.root / "calibration.json"
        with self.lock:
            try:
                data = json.loads(path.read_bytes())
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            entry = _calibration_sample(data.get(model_id)) or {
                "bytes_per_token": ratio,
                "samples": 0,
            }
            samples = min(entry["samples"] + 1, 1000)
            weight = max(0.05, 1 / samples)
            entry = {
                "bytes_per_token": round(
                    entry["bytes_per_token"] * (1 - weight) + ratio * weight, 4
                ),
                "samples": samples,
            }
            data[model_id] = entry
            temp = path.with_suffix(".json.tmp")
            try:
                temp.write_text(json.dumps(data))
                temp.replace(path)
            except OSError:
                return None
        return entry

    def log_diagnostic(self, run_id: str, record: dict) -> None:
        self._append(
            self._path("runs", run_id, ".diagnostics.jsonl"),
            self.redactor.redact(record),
        )
