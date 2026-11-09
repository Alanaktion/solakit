"""Live diagnostics against the configured model: ``sola doctor``.

``sola check`` is offline by design; doctor is its live counterpart. Every
probe returns PASS/WARN/FAIL with a one-line remedy. Probes send small,
synthetic requests only; they never touch sessions, runs or memory writes
(an explicit ``--write-memory`` is required for the scratch round-trip).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from solakit.core.config import Config

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
TOOL_CALL_MARKERS = ("<tool_call>", "<function=", '"name":')
FILLER = "The quick brown fox jumps over the lazy dog near the riverbank. "


@dataclass
class Probe:
    name: str
    status: str
    detail: str
    remedy: str = ""
    data: dict = field(default_factory=dict)


def _weather_tool():
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City"}},
                "required": ["city"],
            },
        },
    }


def _typed_tool():
    return {
        "type": "function",
        "function": {
            "name": "record_order",
            "description": "Record a customer order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "quantity": {"type": "integer", "description": "Item count"},
                    "gift": {"type": "boolean", "description": "Gift wrap"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels",
                    },
                    "size": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "Size",
                    },
                },
                "required": ["quantity", "gift", "tags", "size"],
            },
        },
    }


class Doctor:
    def __init__(self, config: Config, *, transport=None, api_key: str | None = None):
        self.config = config
        self.transport = transport
        key = api_key if api_key is not None else config.model.resolve_api_key()
        self.client = httpx.AsyncClient(
            base_url=config.model.base_url.rstrip("/") + "/",
            timeout=config.model.timeout_s,
            transport=transport,
            trust_env=config.model.trust_env,
            headers={"Authorization": f"Bearer {key}"} if key else {},
        )
        self.models: list[dict] = []

    async def close(self):
        await self.client.aclose()

    def base_request(self, messages, **extra) -> dict:
        request = {
            "model": self.config.model.model_id,
            "messages": messages,
            "max_tokens": extra.pop("max_tokens", 512),
            "stream": False,
        }
        params = getattr(self.config.model, "request", None)
        if params is not None:
            request.update(params.wire(thinking=extra.pop("thinking", "off")))
        else:
            extra.pop("thinking", None)
        request.update(extra)
        return request

    async def post(self, request: dict) -> tuple[dict, float]:
        started = time.monotonic()
        response = await self.client.post("chat/completions", json=request)
        elapsed = time.monotonic() - started
        response.raise_for_status()
        return response.json(), elapsed

    # Probes -----------------------------------------------------------------

    async def reachable(self) -> Probe:
        try:
            response = await self.client.get("models", timeout=10)
            response.raise_for_status()
            self.models = response.json()["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            remedy = (
                "check [model].api_key / backend token"
                if status in (401, 403)
                else "start oMLX or fix [model].base_url"
            )
            return Probe("model reachable", FAIL, type(exc).__name__, remedy)
        served = [m.get("id") for m in self.models]
        if self.config.model.model_id not in served:
            return Probe(
                "model reachable",
                FAIL,
                f"pinned {self.config.model.model_id} not served",
                f"served: {', '.join(map(str, served[:5]))}; fix [model].model_id",
            )
        return Probe("model reachable", PASS, "pinned model served")

    async def streaming(self) -> Probe:
        from solakit.core.model import ModelClient, ModelUnavailable

        client = ModelClient(
            self.config.model,
            transport=self.transport,
            api_key=self._key(),
        )
        try:
            data = await client.complete(
                [{"role": "user", "content": "Reply with the single word: ready"}], []
            )
            text = (data["choices"][0]["message"].get("content") or "").strip()
            return Probe("streaming", PASS, f"stream terminated; reply {text[:40]!r}")
        except ModelUnavailable as exc:
            return Probe(
                "streaming",
                FAIL,
                str(exc),
                "check the server's SSE support and [model].timeout_s",
            )
        finally:
            await client.close()

    def _key(self) -> str | None:
        header = self.client.headers.get("Authorization", "")
        return header.removeprefix("Bearer ") or None

    async def tool_round_trip(self) -> Probe:
        request = self.base_request(
            [{"role": "user", "content": "What is the weather in Lisbon right now?"}],
            tools=[_weather_tool()],
        )
        try:
            data, _ = await self.post(request)
        except httpx.HTTPError as exc:
            return Probe("tool-call round trip", FAIL, type(exc).__name__)
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        if calls and calls[0]["function"]["name"] == "get_weather":
            try:
                args = json.loads(calls[0]["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            if str(args.get("city", "")).casefold().startswith("lisbon"):
                return Probe("tool-call round trip", PASS, "parsed tool_calls")
            return Probe(
                "tool-call round trip",
                WARN,
                f"tool_calls parsed but arguments were {args}",
            )
        if any(marker in content for marker in TOOL_CALL_MARKERS):
            return Probe(
                "tool-call round trip",
                WARN,
                "tool call arrived as text content, not tool_calls",
                "enable the server tool parser or [model].tool_call_fallback = true",
            )
        return Probe(
            "tool-call round trip",
            FAIL,
            "model answered without calling the offered tool",
            "check the chat template supports tools",
        )

    async def argument_fidelity(self) -> Probe:
        request = self.base_request(
            [
                {
                    "role": "user",
                    "content": "Record an order: 3 items, gift wrapped, size large, "
                    "tagged 'birthday' and 'rush'.",
                }
            ],
            tools=[_typed_tool()],
        )
        try:
            data, _ = await self.post(request)
        except httpx.HTTPError as exc:
            return Probe("argument fidelity", FAIL, type(exc).__name__)
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            return Probe("argument fidelity", WARN, "no tool call returned")
        try:
            args = json.loads(calls[0]["function"]["arguments"])
        except ValueError:
            return Probe("argument fidelity", FAIL, "arguments were not JSON")
        problems = []
        if type(args.get("quantity")) is not int:
            problems.append(f"quantity={args.get('quantity')!r}")
        if type(args.get("gift")) is not bool:
            problems.append(f"gift={args.get('gift')!r}")
        if not isinstance(args.get("tags"), list):
            problems.append(f"tags={args.get('tags')!r}")
        if args.get("size") not in ("small", "medium", "large"):
            problems.append(f"size={args.get('size')!r}")
        if problems:
            return Probe(
                "argument fidelity",
                WARN,
                "mistyped: " + ", ".join(problems),
                "lenient argument normalization absorbs scalar/array strings",
            )
        return Probe("argument fidelity", PASS, "int, bool, array, enum valid")

    async def sampling(self) -> Probe:
        request = self.base_request(
            [{"role": "user", "content": "Say hi."}],
            max_tokens=16,
            temperature=0.31,
            top_p=0.77,
            top_k=13,
            presence_penalty=0.2,
        )
        try:
            await self.post(request)
        except httpx.HTTPStatusError as exc:
            return Probe(
                "sampling params",
                FAIL,
                f"HTTP {exc.response.status_code} with temperature/top_p/top_k/"
                "presence_penalty",
                "remove unsupported keys from [model.request]",
            )
        except httpx.HTTPError as exc:
            return Probe("sampling params", FAIL, type(exc).__name__)
        return Probe(
            "sampling params",
            PASS,
            "accepted (servers rarely echo values; acceptance is not proof of use)",
        )

    async def thinking(self) -> Probe:
        results = {}
        for enabled in (True, False):
            request = self.base_request(
                [{"role": "user", "content": "What is 17 * 23?"}],
                max_tokens=1024,
            )
            request["chat_template_kwargs"] = {"enable_thinking": enabled}
            try:
                data, elapsed = await self.post(request)
            except httpx.HTTPStatusError as exc:
                return Probe(
                    "thinking control",
                    FAIL,
                    f"HTTP {exc.response.status_code} for chat_template_kwargs",
                    'set [model.request].thinking_control = "none"',
                )
            except httpx.HTTPError as exc:
                return Probe("thinking control", FAIL, type(exc).__name__)
            message = data["choices"][0]["message"]
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            content = message.get("content") or ""
            results[enabled] = {
                "reasoning": bool(reasoning) or "<think>" in content,
                "seconds": round(elapsed, 2),
                "completion_tokens": (data.get("usage") or {}).get("completion_tokens"),
            }
        on, off = results[True], results[False]
        if on["reasoning"] and not off["reasoning"]:
            return Probe(
                "thinking control",
                PASS,
                f"toggle works (on {on['seconds']}s / off {off['seconds']}s)",
                data=results,
            )
        if not on["reasoning"] and not off["reasoning"]:
            return Probe(
                "thinking control",
                WARN,
                "no reasoning observed either way",
                "the model or server may not split reasoning_content",
                data=results,
            )
        return Probe(
            "thinking control",
            WARN,
            "enable_thinking=false did not suppress reasoning",
            'use thinking = "on" or check the server\'s template kwargs support',
            data=results,
        )

    async def context_window(self, filler_tokens: int = 3000) -> Probe:
        entry = next(
            (m for m in self.models if m.get("id") == self.config.model.model_id), {}
        )
        window = next(
            (
                entry[key]
                for key in (
                    "context_length",
                    "max_model_len",
                    "max_context_length",
                    "context_window",
                )
                if isinstance(entry.get(key), int)
            ),
            None,
        )
        text = FILLER * max(1, filler_tokens // 13)
        messages = [{"role": "user", "content": text + "\nReply: ok"}]
        request = self.base_request(messages, max_tokens=4)
        try:
            data, _ = await self.post(request)
        except httpx.HTTPError as exc:
            return Probe("context window", FAIL, type(exc).__name__)
        prompt_tokens = (data.get("usage") or {}).get("prompt_tokens")
        size = len(json.dumps(messages, ensure_ascii=False).encode())
        ratio = round(size / prompt_tokens, 3) if prompt_tokens else None
        budgets = {
            name: profile.context_budget
            for name, profile in self.config.profiles.items()
        }
        detail = (
            f"advertised window {window or 'not exposed'}; "
            f"bytes/token {ratio or 'unknown'}"
        )
        data_out = {"window": window, "bytes_per_token": ratio, "budgets": budgets}
        if window and ratio:
            over = {
                n: b
                for n, b in budgets.items()
                if b / ratio + self.config.model.max_tokens > window
            }
            if over:
                return Probe(
                    "context window",
                    WARN,
                    detail + f"; budgets exceed window: {over}",
                    "lower context_budget or [model].max_tokens",
                    data_out,
                )
        return Probe("context window", PASS, detail, data=data_out)

    async def prefix_cache(self, filler_tokens: int = 6000) -> Probe:
        prefix = FILLER * max(1, filler_tokens // 13)
        timings = []
        for suffix in ("Question A: reply ok.", "Question B: reply ok."):
            request = self.base_request(
                [
                    {"role": "system", "content": prefix},
                    {"role": "user", "content": suffix},
                ],
                max_tokens=4,
            )
            try:
                _, elapsed = await self.post(request)
            except httpx.HTTPError as exc:
                return Probe("prefix cache", FAIL, type(exc).__name__)
            timings.append(elapsed)
        ratio = timings[1] / timings[0] if timings[0] else 1
        data = {"first_s": round(timings[0], 2), "second_s": round(timings[1], 2)}
        if ratio < 0.6:
            return Probe(
                "prefix cache",
                PASS,
                f"shared prefix reused ({data['first_s']}s → {data['second_s']}s)",
                data=data,
            )
        return Probe(
            "prefix cache",
            WARN,
            f"no speed-up on shared prefix ({data['first_s']}s → {data['second_s']}s)",
            "enable the server's prefix/KV cache; keep prompt_layout = stable",
            data=data,
        )

    async def concurrency(self, levels=(1, 2, 4)) -> Probe:
        async def one():
            started = time.monotonic()
            request = self.base_request(
                [{"role": "user", "content": "Count from 1 to 20, comma separated."}],
                max_tokens=96,
            )
            data, _ = await self.post(request)
            tokens = (data.get("usage") or {}).get("completion_tokens") or 0
            return time.monotonic() - started, tokens

        table = {}
        try:
            for level in levels:
                started = time.monotonic()
                results = await asyncio.gather(*(one() for _ in range(level)))
                wall = time.monotonic() - started
                latencies = sorted(r[0] for r in results)
                tokens = sum(r[1] for r in results)
                table[level] = {
                    "throughput_tok_s": round(tokens / wall, 1) if wall else 0,
                    "p95_s": round(latencies[-1], 2),
                    "median_s": round(statistics.median(latencies), 2),
                }
        except httpx.HTTPError as exc:
            return Probe("concurrency", FAIL, type(exc).__name__)
        return concurrency_verdict(table, self.config.model.concurrency)

    def tools(self, threshold_bytes: int = 12000) -> Probe:
        from solakit.tools import Registry

        try:
            registry = Registry(self.config)
        except (ValueError, ImportError) as exc:
            return Probe("tools", FAIL, str(exc)[:200], "run sola check")
        sizes = {}
        for name in self.config.profiles:
            view = registry.view(name)
            sizes[name] = {
                "tools": len(view.effective),
                "schema_bytes": len(json.dumps(view.schemas()).encode()),
                "mode": view.mode,
            }
        heavy = {
            n: s["schema_bytes"]
            for n, s in sizes.items()
            if s["mode"] == "eager" and s["schema_bytes"] > threshold_bytes
        }
        detail = "; ".join(
            f"{n}: {s['tools']} tools {s['schema_bytes']} B {s['mode']}"
            for n, s in sizes.items()
        )
        if heavy:
            return Probe(
                "tools",
                WARN,
                detail,
                "narrow profiles with toolsets or use tool_disclosure = auto",
                sizes,
            )
        return Probe("tools", PASS, detail, data=sizes)

    async def memory(self, write: bool = False) -> Probe:
        if not self.config.memory.enabled:
            return Probe("memory", SKIP, "memory disabled")
        from solakit.core.memory import MemoryClient, MemoryUnavailable

        key = (
            self.config.memory.api_key.resolve() if self.config.memory.api_key else None
        )
        client = MemoryClient(self.config.memory, key)
        try:
            await client.preflight()
        except MemoryUnavailable as exc:
            return Probe(
                "memory", FAIL, str(exc), "check memory endpoint, user and roots"
            )
        finally:
            await client.close()
        suffix = (
            "; scratch round-trip: use sola memory-test against the server"
            if write
            else ""
        )
        return Probe("memory", PASS, "read-only root checks passed" + suffix)


def concurrency_verdict(table: dict, configured: int) -> Probe:
    """Knee: the smallest level within 10% of the best measured throughput."""
    best = max(table, key=lambda n: table[n]["throughput_tok_s"])
    knee = min(
        level
        for level in table
        if table[level]["throughput_tok_s"] >= 0.9 * table[best]["throughput_tok_s"]
    )
    detail = "; ".join(
        f"{n}: {v['throughput_tok_s']} tok/s p95 {v['p95_s']}s"
        for n, v in table.items()
    )
    data = {"levels": table, "knee": knee, "configured": configured}
    if configured > knee:
        return Probe(
            "concurrency",
            WARN,
            detail + f"; knee {knee} < configured {configured}",
            f"set [model].concurrency = {knee}",
            data,
        )
    return Probe("concurrency", PASS, detail + f"; knee {knee}", data=data)


async def run_doctor(
    config: Config,
    *,
    transport=None,
    api_key: str | None = None,
    quick: bool = False,
    write_memory: bool = False,
) -> list[Probe]:
    doctor = Doctor(config, transport=transport, api_key=api_key)
    probes: list[Probe] = []
    try:
        first = await doctor.reachable()
        probes.append(first)
        if first.status == FAIL:
            return probes
        for name in ("streaming", "tool_round_trip", "argument_fidelity", "sampling"):
            probes.append(await getattr(doctor, name)())
        probes.append(await doctor.thinking())
        probes.append(await doctor.context_window())
        if not quick:
            probes.append(await doctor.prefix_cache())
            probes.append(await doctor.concurrency())
        probes.append(doctor.tools())
        probes.append(await doctor.memory(write_memory))
    finally:
        await doctor.close()
    return probes


def save(config: Config, probes: list[Probe]) -> Path | None:
    """Cache the latest result for the UI; never fatal."""
    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        "model_id": config.model.model_id,
        "probes": [asdict(p) for p in probes],
    }
    path = config.paths.data.expanduser() / "doctor.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2))
        temp.replace(path)
        return path
    except OSError:
        return None


def last(config: Config) -> dict | None:
    path = config.paths.data.expanduser() / "doctor.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def format_probe(probe: Probe) -> str:
    line = f"{probe.status:4} {probe.name}: {probe.detail}"
    if probe.remedy and probe.status in (WARN, FAIL):
        line += f"\n     → {probe.remedy}"
    return line


def concurrency_warning(config: Config) -> str | None:
    """Offline check against the last doctor measurement, when one exists."""
    record = last(config)
    if not record or record.get("model_id") != config.model.model_id:
        return None
    probe = next(
        (p for p in record.get("probes", []) if p.get("name") == "concurrency"), None
    )
    knee = (probe or {}).get("data", {}).get("knee")
    if isinstance(knee, int) and config.model.concurrency > knee:
        return (
            f"[model].concurrency = {config.model.concurrency} exceeds the measured "
            f"knee {knee} (sola doctor, {record.get('checked_at', 'unknown')})"
        )
    return None
