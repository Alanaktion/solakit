"""Scope-bound OpenViking REST access and conservative commit recovery.

Contract checked against OpenViking tag v0.4.17.1 session/search routers and
working-memory policy, plus docs/en/api/{05-sessions,06-retrieval,12-content}.md.
The server does not document idempotency for message appends. Persist intent
before each remote write and refuse ambiguous replays, rather than duplicating
memories after a lost response. Each run has a stable remote session identity.
"""

import asyncio
import json
import re
import time
from collections.abc import Mapping

import httpx

from solakit.core.config import MemoryConfig, ProfileMemory
from solakit.core.io import durable
from solakit.core.schemas import Session
from solakit.core.store import Store, safe_id


class MemoryUnavailable(Exception):
    """Memory operation failed or needs operator reconciliation."""


class MemoryClient:
    def __init__(
        self,
        config: MemoryConfig,
        key: str | None = None,
        transport=None,
        overrides: Mapping[str, ProfileMemory] | None = None,
    ):
        self.config = config
        # Profile name -> isolated memory user/roots; absent profiles use config.
        self.overrides = dict(overrides or {})
        headers = {"X-API-Key": key} if key else {}
        if config.user:
            headers["X-OpenViking-User"] = config.user
        self.client = httpx.AsyncClient(
            base_url=(config.base_url or "http://127.0.0.1:1933").rstrip("/") + "/",
            headers=headers,
            timeout=config.timeout_s,
            transport=transport,
            trust_env=config.trust_env,
        )

    async def request(
        self, method: str, path: str, *, user: str | None = None, **kwargs
    ):
        if user is not None:
            # Per-request header replaces the client default user hint.
            kwargs["headers"] = {**kwargs.get("headers", {}), "X-OpenViking-User": user}
        try:
            response = await self.client.request(method, f"api/v1/{path}", **kwargs)
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "ok" or "result" not in payload:
                raise MemoryUnavailable("OpenViking returned an invalid result")
            return payload["result"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise MemoryUnavailable("OpenViking request failed") from exc

    async def preflight(self) -> None:
        """Read-only authenticated checks of every configured memory root."""
        try:
            async with asyncio.timeout(min(5, self.config.timeout_s)):
                targets = [(None, self.config.roots)] + [
                    (o.user, o.roots) for o in self.overrides.values()
                ]
                for user, roots in targets:
                    for root in roots:
                        result = await self.request(
                            "GET", "fs/stat", params={"uri": root}, user=user
                        )
                        if not isinstance(result, dict):
                            raise MemoryUnavailable(
                                "OpenViking returned invalid root metadata"
                            )
        except TimeoutError as exc:
            raise MemoryUnavailable("OpenViking startup check timed out") from exc

    def scoped(self, session: Session, profile: str | None = None):
        """Facade bound to the session and the profile's memory user/roots."""
        return ScopedMemory(self, session, self.overrides.get(profile or ""))

    async def close(self):
        await self.client.aclose()


class ScopedMemory:
    """Tools receive only this facade, never the unrestricted HTTP client."""

    def __init__(
        self,
        client: MemoryClient,
        session: Session,
        override: ProfileMemory | None = None,
    ):
        self._client = client
        self.session = session
        self.user = override.user if override else client.config.user
        self.roots = override.roots if override else client.config.roots
        # Only an override needs a per-request header; the client default covers
        # the global user.
        self._user_header = override.user if override else None

    def _request(self, method: str, path: str, **kwargs):
        return self._client.request(method, path, user=self._user_header, **kwargs)

    def check(self):
        if self.session.memory_scope != "shared":
            raise MemoryUnavailable(
                "Memory access is only implemented for shared scope"
            )

    def check_uri(self, uri: str):
        self.check()
        if not isinstance(uri, str):
            raise MemoryUnavailable("Invalid memory URI")
        if (
            "%" in uri
            or "?" in uri
            or "#" in uri
            or any(p in {".", ".."} for p in uri.split("/"))
        ):
            raise MemoryUnavailable("Noncanonical memory URI")
        roots = self.roots
        if not any(
            uri == root.rstrip("/") or uri.startswith(root.rstrip("/") + "/")
            for root in roots
        ):
            raise MemoryUnavailable("Memory URI outside configured roots")

    async def search(self, query: str) -> str:
        self.check()
        result = await self._request(
            "POST",
            "search/find",
            json={
                "query": query,
                "target_uri": self.roots,
                "limit": self._client.config.recall_limit,
            },
        )
        try:
            hits = []
            for category in ("memories", "resources", "skills"):
                for item in result.get(category, []):
                    self.check_uri(item["uri"])
                    hits.append(f"{item['uri']}\n{item.get('abstract', '')}")
            return "\n\n".join(hits)
        except (TypeError, KeyError, AttributeError) as exc:
            raise MemoryUnavailable("Invalid recall result") from exc

    async def read(self, uri: str) -> str:
        self.check_uri(uri)
        result = await self._request("GET", "content/read", params={"uri": uri})
        if not isinstance(result, str):
            raise MemoryUnavailable("Invalid memory content")
        return result

    async def commit(
        self,
        store: Store,
        run_id: str,
        messages: list[dict],
        *,
        summary_only: bool = False,
    ) -> dict:
        self.check()
        remote = f"solakit-{safe_id(run_id)}"
        events = await durable(store.memory_events, run_id)
        if events:
            accepted = next(
                (event for event in reversed(events) if event["stage"] == "accepted"),
                None,
            )
            if accepted:
                return accepted["result"]
            # No append or commit replay without a verified remote idempotency API.
            raise MemoryUnavailable(
                "Ambiguous prior memory write; reconcile remote session"
            )
        await durable(
            store.log_memory, run_id, {"stage": "creating", "session": remote}
        )
        if summary_only:
            created = await self._request(
                "POST",
                "sessions",
                json={
                    "session_id": remote,
                    "memory_policy": {
                        "self": {"enabled": False},
                        "peer": {"enabled": False},
                        "working_memory": {"enabled": True},
                    },
                },
            )
        else:
            created = await self._request(
                "GET", f"sessions/{remote}", params={"auto_create": "true"}
            )
        if self.user:
            expected_uri = f"viking://user/{self.user}/sessions/{remote}"
            uri = created.get("uri") if isinstance(created, dict) else None
            if not isinstance(uri, str) or uri.rstrip("/") != expected_uri:
                raise MemoryUnavailable(
                    "Remote session user does not match configured memory user"
                )
        # Disable inherited automatic extraction on this run-owned remote session.
        await self._request(
            "PATCH", f"sessions/{remote}/config", json={"auto_commit_policy": None}
        )
        # Parts preserve assistant call/result groups without unsupported tool roles.
        converted = memory_messages(messages)
        for start in range(0, len(converted), 100):
            await durable(
                store.log_memory,
                run_id,
                {"stage": "sending", "offset": start, "session": remote},
            )
            await self._request(
                "POST",
                f"sessions/{remote}/messages/batch",
                json={"messages": converted[start : start + 100]},
            )
        await durable(
            store.log_memory, run_id, {"stage": "committing", "session": remote}
        )
        result = await self._request(
            "POST",
            f"sessions/{remote}/commit",
            json={"keep_recent_count": 0},
        )
        if not isinstance(result, dict) or result.get("status") not in {
            "accepted",
            "skipped",
        }:
            raise MemoryUnavailable("Invalid commit acknowledgment")
        await durable(
            store.log_memory,
            run_id,
            {"stage": "accepted", "session": remote, "result": result},
        )
        return result

    async def refresh(self, store: Store, run_id: str, *, wait_s: float = 0) -> dict:
        self.check()
        events = await durable(store.memory_events, run_id)
        accepted = next((e for e in reversed(events) if e["stage"] == "accepted"), None)
        if accepted is None:
            raise MemoryUnavailable("No acknowledged commit; reconcile before recovery")
        result = accepted["result"]
        if result["status"] == "skipped":
            return result
        task_id = result.get("task_id")
        if not isinstance(task_id, str):
            raise MemoryUnavailable("Accepted commit has no extraction task ID")
        try:
            safe_id(task_id)
        except ValueError:
            raise MemoryUnavailable("Invalid extraction task ID") from None
        deadline = time.monotonic() + wait_s
        while True:
            task = await self._request("GET", f"tasks/{task_id}")
            if not isinstance(task, dict) or task.get("status") not in {
                "pending",
                "running",
                "cancelling",
                "completed",
                "failed",
                "cancelled",
            }:
                raise MemoryUnavailable("Invalid extraction task response")
            outcome = {**result, "status": task["status"], "task": task}
            latest = next((e for e in reversed(events) if e["stage"] == "task"), None)
            if latest is None or latest["result"] != outcome:
                await durable(
                    store.log_memory, run_id, {"stage": "task", "result": outcome}
                )
                events.append({"stage": "task", "result": outcome})
            if (
                task["status"] in {"completed", "failed", "cancelled"}
                or time.monotonic() >= deadline
            ):
                return outcome
            await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))

    async def summary(
        self, store: Store, operation: str, messages: list[dict]
    ) -> tuple[str, str]:
        await self.commit(store, operation, messages, summary_only=True)
        budget = self._client.config.compaction_timeout_s
        try:
            async with asyncio.timeout(budget):
                outcome = await self.refresh(store, operation, wait_s=budget)
                if outcome["status"] in {"pending", "running", "cancelling"}:
                    raise TimeoutError
        except TimeoutError:
            events = await durable(store.memory_events, operation)
            latest = next(
                (
                    event["result"]
                    for event in reversed(events)
                    if event["stage"] == "task"
                ),
                None,
            )
            state = (
                f"extraction is still {latest['status']}"
                if latest and latest["status"] in {"pending", "running", "cancelling"}
                else "extraction has not completed"
            )
            raise MemoryUnavailable(
                f"Compaction timed out after {budget:g} seconds; {state}. "
                "This turn has stopped."
            ) from None
        if outcome["status"] != "completed":
            raise MemoryUnavailable(
                f"Compaction extraction is {outcome['status']}. This turn has stopped."
            )
        remote = f"solakit-{safe_id(operation)}"
        uri = outcome.get("archive_uri", "")
        if not isinstance(uri, str):
            raise MemoryUnavailable("Compaction acknowledgment has no archive URI")
        match = re.fullmatch(
            r"viking://[^?#]+/sessions/"
            + re.escape(remote)
            + r"/history/(archive_\d+)",
            uri,
        )
        if not match:
            raise MemoryUnavailable(
                "Compaction acknowledgment has invalid archive identity"
            )
        archive = await self._request("GET", f"sessions/{remote}/archives/{match[1]}")
        if (
            not isinstance(archive, dict)
            or archive.get("status", "completed") != "completed"
            or archive.get("archive_id") != match[1]
            or not isinstance(archive.get("overview"), str)
            or not archive["overview"].strip()
        ):
            raise MemoryUnavailable("Completed compaction archive has no summary")
        return archive["overview"], uri + "/.overview.md"


def memory_messages(messages: list[dict]) -> list[dict]:
    converted = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message["role"]
        if role in {"user", "assistant"}:
            parts = []
            if message.get("content"):
                parts.append({"type": "text", "text": message["content"]})
            for image in message.get("images", []):
                parts.append(
                    {
                        "type": "text",
                        "text": "[Image attachment metadata; pixels retained locally: "
                        + json.dumps(image, ensure_ascii=False)
                        + "]",
                    }
                )
            calls = message.get("tool_calls") or []
            following = []
            for candidate in messages[index + 1 : index + 1 + len(calls)]:
                if candidate["role"] != "tool":
                    break
                following.append(candidate)
            results = {
                m.get("tool_call_id"): m for m in following if m["role"] == "tool"
            }
            for call in calls:
                function = call["function"]
                try:
                    arguments = json.loads(function["arguments"])
                except ValueError:
                    arguments = {"invalid_arguments": function["arguments"]}
                parts.append(
                    {
                        "type": "tool",
                        "tool_id": call["id"],
                        "tool_name": function["name"],
                        "tool_input": arguments,
                        "tool_status": tool_status(results.get(call["id"])),
                        "tool_output": results.get(call["id"], {}).get(
                            "content", "Interrupted"
                        ),
                    }
                )
            if parts:
                converted.append({"role": role, "parts": parts})
            index += len(following)
        index += 1
    return converted


def tool_status(result: dict | None) -> str:
    if result is None:
        return "error"
    try:
        payload = json.loads(result.get("content", ""))
        return (
            "error"
            if isinstance(payload, dict) and payload.get("error")
            else "completed"
        )
    except (ValueError, TypeError):
        return "completed"
