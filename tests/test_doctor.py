import asyncio
import json

import httpx

from solakit.core import doctor


def server(config, *, content_calls=False, reject_sampling=False, slow=False):
    """A configurable mock OpenAI-compatible server."""

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": config.model.model_id, "context_length": 32768}]},
            )
        body = json.loads(request.content)
        if reject_sampling and "top_k" in body:
            return httpx.Response(400, json={"error": "unknown field top_k"})
        if slow:
            await asyncio.sleep(0.02)
        message = {"role": "assistant", "content": "ok"}
        tools = body.get("tools") or []
        if tools:
            name = tools[0]["function"]["name"]
            arguments = (
                {"city": "Lisbon"}
                if name == "get_weather"
                else {
                    "quantity": 3,
                    "gift": True,
                    "tags": ["birthday", "rush"],
                    "size": "large",
                }
            )
            if content_calls:
                message["content"] = (
                    "<tool_call>\n"
                    + json.dumps({"name": name, "arguments": arguments})
                    + "\n</tool_call>"
                )
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
        thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking")
        if thinking:
            message["reasoning_content"] = "17*23 = 391"
        if body.get("stream"):
            chunks = [
                {
                    "model": config.model.model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ready"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
            return httpx.Response(
                200,
                content=text + "data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "model": config.model.model_id,
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(handler)


def statuses(probes):
    return {p.name: p.status for p in probes}


def test_healthy_server_passes(config):
    probes = asyncio.run(doctor.run_doctor(config, transport=server(config)))
    result = statuses(probes)
    assert result["model reachable"] == "PASS"
    assert result["streaming"] == "PASS"
    assert result["tool-call round trip"] == "PASS"
    assert result["argument fidelity"] == "PASS"
    assert result["sampling params"] == "PASS"
    assert result["thinking control"] == "PASS"
    assert result["context window"] == "PASS"
    assert result["tools"] == "PASS"
    assert result["memory"] == "SKIP"
    window = next(p for p in probes if p.name == "context window")
    assert window.data["window"] == 32768 and window.data["bytes_per_token"]


def test_flags_tool_calls_in_content(config):
    probes = asyncio.run(
        doctor.run_doctor(config, transport=server(config, content_calls=True))
    )
    probe = next(p for p in probes if p.name == "tool-call round trip")
    assert probe.status == "WARN"
    assert "tool_call_fallback" in probe.remedy


def test_flags_rejected_sampling_parameters(config):
    probes = asyncio.run(
        doctor.run_doctor(
            config, transport=server(config, reject_sampling=True), quick=True
        )
    )
    assert statuses(probes)["sampling params"] == "FAIL"


def test_flags_concurrency_above_knee(config):
    config.model.concurrency = 4
    probes = asyncio.run(doctor.run_doctor(config, transport=server(config, slow=True)))
    probe = next(p for p in probes if p.name == "concurrency")
    # Mock throughput scales; the probe still reports levels and the knee.
    assert set(probe.data["levels"]) == {1, 2, 4}
    assert probe.status in {"PASS", "WARN"}
    table = {
        1: {"throughput_tok_s": 100, "p95_s": 1.0, "median_s": 1.0},
        2: {"throughput_tok_s": 104, "p95_s": 1.9, "median_s": 1.8},
        4: {"throughput_tok_s": 106, "p95_s": 3.9, "median_s": 3.7},
    }
    verdict = doctor.concurrency_verdict(table, 4)
    assert verdict.status == "WARN" and verdict.data["knee"] == 1
    assert "concurrency = 1" in verdict.remedy
    assert doctor.concurrency_verdict(table, 1).status == "PASS"


def test_unreachable_model_fails_fast(config):
    def handler(request):
        return httpx.Response(401)

    probes = asyncio.run(
        doctor.run_doctor(config, transport=httpx.MockTransport(handler))
    )
    assert [p.status for p in probes] == ["FAIL"]
    assert "api_key" in probes[0].remedy


def test_save_and_last(config):
    probe = doctor.Probe("x", "PASS", "fine")
    path = doctor.save(config, [probe])
    assert path and doctor.last(config)["probes"][0]["name"] == "x"
