"""Scope-bound OpenViking REST access and conservative commit recovery.

Contract: upstream docs/en/api/{05-sessions,06-retrieval,12-content}.md.
The server does not document idempotency for message appends. Persist intent
before each remote write and refuse ambiguous replays, rather than duplicating
memories after a lost response. Each run has a stable remote session identity.
"""

import json

import httpx

from solakit.core.config import MemoryConfig
from solakit.core.io import durable
from solakit.core.schemas import Session
from solakit.core.store import Store, safe_id


class MemoryUnavailable(Exception):
    """Memory operation failed or needs operator reconciliation."""


class MemoryClient:
    def __init__(self, config: MemoryConfig, key: str | None = None, transport=None):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=(config.base_url or "http://127.0.0.1:1933").rstrip("/") + "/",
            headers={"X-API-Key": key} if key else {},
            timeout=config.timeout_s,
            transport=transport,
            trust_env=config.trust_env,
        )

    async def request(self, method: str, path: str, **kwargs):
        try:
            response = await self.client.request(method, f"api/v1/{path}", **kwargs)
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "ok" or "result" not in payload:
                raise MemoryUnavailable("OpenViking returned an invalid result")
            return payload["result"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise MemoryUnavailable("OpenViking request failed") from exc

    def scoped(self, session: Session):
        return ScopedMemory(self, session)

    async def close(self):
        await self.client.aclose()


class ScopedMemory:
    """Tools receive only this facade, never the unrestricted HTTP client."""

    def __init__(self, client: MemoryClient, session: Session):
        self._client = client
        self.session = session

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
        roots = self._client.config.roots
        if not any(
            uri == root.rstrip("/") or uri.startswith(root.rstrip("/") + "/")
            for root in roots
        ):
            raise MemoryUnavailable("Memory URI outside configured roots")

    async def search(self, query: str) -> str:
        self.check()
        result = await self._client.request(
            "POST",
            "search/find",
            json={
                "query": query,
                "target_uri": self._client.config.roots,
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
        result = await self._client.request("GET", "content/read", params={"uri": uri})
        if not isinstance(result, str):
            raise MemoryUnavailable("Invalid memory content")
        return result

    async def commit(self, store: Store, run_id: str, messages: list[dict]) -> dict:
        self.check()
        remote = f"solakit-{safe_id(run_id)}"
        events = await durable(store.memory_events, run_id)
        if events:
            latest = events[-1]
            if latest["stage"] == "accepted":
                return latest["result"]
            # No append or commit replay without a verified remote idempotency API.
            raise MemoryUnavailable(
                "Ambiguous prior memory write; reconcile remote session"
            )
        await durable(
            store.log_memory, run_id, {"stage": "creating", "session": remote}
        )
        await self._client.request(
            "GET", f"sessions/{remote}", params={"auto_create": "true"}
        )
        # Disable inherited automatic extraction on this run-owned remote session.
        await self._client.request(
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
            await self._client.request(
                "POST",
                f"sessions/{remote}/messages/batch",
                json={"messages": converted[start : start + 100]},
            )
        await durable(
            store.log_memory, run_id, {"stage": "committing", "session": remote}
        )
        result = await self._client.request(
            "POST", f"sessions/{remote}/commit", json={"keep_recent_count": 0}
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
            calls = message.get("tool_calls") or []
            following = messages[index + 1 : index + 1 + len(calls)]
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
                        "tool_status": "completed"
                        if call["id"] in results
                        else "error",
                        "tool_output": results.get(call["id"], {}).get(
                            "content", "Interrupted"
                        ),
                    }
                )
            if parts:
                converted.append({"role": role, "parts": parts})
            index += len(calls)
        index += 1
    return converted
