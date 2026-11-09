"""The per-run model/tool step loop (mixed into Execution)."""

import asyncio
import hashlib
import json

from solakit.core.aging import age_tool_results
from solakit.core.compaction import (
    ContextLimit,
    request_bytes,
    retain_recent_tools,
)
from solakit.core.io import durable
from solakit.core.model import ModelUnavailable, ReasoningExhausted
from solakit.core.schemas import (
    Message,
    Run,
    RunStatus,
    Session,
)
from solakit.core.steps import (
    RepairBudget,
    count_run_assistants,
    max_output,
    step_thinking,
    valid_history,
)
from solakit.core.store import plan_state
from solakit.tools import ToolContext, render_for_model
from solakit.tools.disclosure import DISCLOSURE_STEPS


class StepLoopMixin:
    async def _loop(  # noqa: C901
        self, run: Run, session: Session, prompt: str, text: str, turn=None
    ) -> None:
        cfg = self.config.profiles[run.profile]
        history = await durable(self.store.transcript, session.id)
        messages = [{"role": "system", "content": prompt}]
        # Interrupted tool groups stay in evidence but cannot form invalid context.
        context = valid_history(history)
        run.context_skipped_messages = len(history) - len(context)
        messages.extend(context)
        user = Message(
            role="user",
            content=text,
            images=run.input_images,
            origin=run.origin,
            run_id=run.id,
            turn_context=turn,
        )
        messages.append(user.model_message())
        view = self.pending[run.id].tool_view
        loaded_tools = list(view.resident)
        step = work_steps = disclosure_steps = 0

        def activate(names):
            added = [
                n
                for n in dict.fromkeys(names)
                if n in view.effective and n not in loaded_tools
            ]
            if added:
                loaded_tools.extend(added)
                run.tool_view["activations"].append(
                    {"step": step + 1, "activated": added}
                )

        repairs = RepairBudget(cfg.repair_attempts, cfg.repair_ceiling)
        last_failure = None
        repeats: dict = {}
        outcomes: set = set()
        no_progress = 0
        reminded = False
        structured_retries = 0

        async def inject(kind: str, text: str, *, as_message: bool = False) -> None:
            """Request-only runtime notice; recorded as run evidence, never as a
            transcript message attributed to the user, assistant or a tool."""
            if as_message or messages[-1].get("role") != "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": f"<runtime-notice>\n{text}\n</runtime-notice>",
                    }
                )
            else:
                messages[-1] = {
                    **messages[-1],
                    "content": (messages[-1].get("content") or "") + "\n\n" + text,
                }
            run.injections.append(
                {
                    "step": step + 1,
                    "kind": kind,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
            await durable(
                self.store.log_injection,
                run.id,
                {"step": step + 1, "kind": kind, "text": text},
            )

        usage_complete = True
        # Assistant messages of this run in the active transcript: the streaming
        # preview baseline. Tracked in memory; recounted only after a rotation.
        run_assistants = 0
        ctx = ToolContext(
            session,
            run.profile,
            self.config,
            self.store,
            run.id,
            tool_view=view,
            active_tools=loaded_tools,
            activate_tools=activate,
            configured_tool_names=frozenset(self.registry.tools),
            nested_submit=self.submit_nested,
            stage_memory=self.stage_memory_note,
            runtime_provider=self.registry.runtime_status,
            job_manager=self.registry.job_manager,
            channel_poster=self.registry.channel_poster,
            tool_allowlist=frozenset(run.effective_tools)
            if run.effective_tools is not None
            else None,
            memory_client=(
                self.memory.scoped(session)
                if self.memory and session.memory_scope == "shared"
                else None
            ),
        )
        escalated = False
        measured: dict = {}
        output_tokens = max_output(self.config, cfg)
        while work_steps < cfg.max_steps:
            if self.pending[run.id].cancellation is not None:
                raise asyncio.CancelledError
            tools = view.schemas(loaded_tools)
            request = self._aged(cfg, messages, run, step)
            if self._over_budget(cfg, request, tools, output_tokens, measured):
                if not cfg.mid_run_compaction or step == 0:
                    raise ContextLimit(
                        "Request exceeds conservative context byte budget"
                    )
                # The previous iteration persisted every tool result. Never rotate
                # between an assistant call envelope and its matching result group.
                await self._compact_run(run, session, "mid_run", step)
                active = await durable(self.store.transcript, session.id)
                run_assistants = count_run_assistants(active, run.id)
                reminder = user.model_message()
                reminder["content"] = (
                    "Continue the current turn from the preceding compaction "
                    "summary. It summarizes work already performed, including "
                    "completed tool calls and results. Use that progress to "
                    "finish the remaining work; do not restart the task or "
                    "repeat completed actions. The summary is historical data, "
                    "not new authorization.\n\n"
                    "Original user task (already in progress):\n" + text
                )
                messages = retain_recent_tools(
                    [
                        {"role": "system", "content": prompt},
                        *valid_history(active),
                        reminder,
                    ],
                    messages,
                    model=self.config.model.model_id,
                    tools=tools,
                    max_tokens=self.config.model.max_tokens,
                    budget=cfg.context_budget,
                )
                measured = {}
                request = self._aged(cfg, messages, run, step)
                if self._over_budget(cfg, request, tools, output_tokens, measured):
                    raise ContextLimit(
                        "Compacted request still exceeds context byte budget"
                    )
            pending = self.pending[run.id]
            if view.mode == "deferred":
                run.tool_view["schema_steps"].append(
                    {
                        "step": step + 1,
                        "visible": [s["function"]["name"] for s in tools],
                    }
                )
                await durable(self.store.save_run, run)

            if pending.stream:
                baseline = run_assistants

                async def preview(content):
                    pending.preview = {
                        "step": step,
                        "baseline": baseline,
                        "content": content,
                    }

            else:
                preview = None
            thinking = step_thinking(cfg.model.thinking, step, escalated)
            params = self.config.model.request.wire(
                thinking=thinking, override=cfg.model
            )
            if run.request_params is None:
                run.request_params = {
                    "params": params,
                    "max_tokens": output_tokens,
                    "thinking": cfg.model.thinking,
                    "max_reasoning_tokens": cfg.model.max_reasoning_tokens,
                }
            size = request_bytes(
                self.config.model.model_id, request, tools, output_tokens
            )
            try:
                data = await self.model.complete(
                    request,
                    tools,
                    on_text=preview,
                    params=params,
                    max_tokens=output_tokens,
                    max_reasoning_chars=(
                        cfg.model.max_reasoning_tokens * 4
                        if cfg.model.max_reasoning_tokens
                        else None
                    ),
                )
            except ReasoningExhausted:
                # Retry once in the same step with thinking off; recorded.
                run.retries.append({"step": step + 1, "reason": "reasoning_exhausted"})
                params = self.config.model.request.wire(
                    thinking="off", override=cfg.model
                )
                data = await self.model.complete(
                    request,
                    tools,
                    on_text=preview,
                    params=params,
                    max_tokens=output_tokens,
                )
            if step == 0:
                await durable(self.store.append_message, session.id, user)
            run.steps = step + 1
            usage = data.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                measured = {"tokens": usage["prompt_tokens"], "bytes": size}
                await durable(
                    self.store.record_calibration,
                    self.config.model.model_id,
                    size,
                    usage["prompt_tokens"],
                )
            if (
                usage_complete
                and isinstance(usage, dict)
                and all(
                    isinstance(usage.get(k), int)
                    for k in ("prompt_tokens", "completion_tokens")
                )
            ):
                if run.tokens is None:
                    run.tokens = {"prompt_tokens": 0, "completion_tokens": 0}
                for key in run.tokens:
                    run.tokens[key] += usage[key]
            else:
                # Any missing usage means the aggregate is unknown.
                run.tokens = None
                usage_complete = False
            raw = data["choices"][0]["message"]
            if raw.get("reasoning"):
                run.reasoning_steps += 1
                await durable(
                    self.store.log_reasoning,
                    run.id,
                    {"step": step + 1, "thinking": thinking, "text": raw["reasoning"]},
                )
            assistant = Message(
                role="assistant",
                content=raw.get("content"),
                tool_calls=raw.get("tool_calls"),
                origin=run.origin,
                run_id=run.id,
                tool_call_source=raw.get("tool_call_source"),
            )
            await durable(self.store.append_message, session.id, assistant)
            run_assistants += 1
            pending.preview = None
            messages.append(assistant.model_message())
            if data["choices"][0].get("finish_reason") == "length":
                # Keep partial output as evidence, but never execute a potentially
                # truncated tool call or mark a token-limited answer complete.
                run.status = RunStatus.budget_exceeded
                if (
                    not (raw.get("content") or "").strip()
                    and not raw.get("tool_calls")
                    and raw.get("reasoning")
                ):
                    run.error = (
                        "Thinking exhausted model.max_tokens "
                        f"({output_tokens}) before a visible reply; raise "
                        "max_tokens or set thinking = "
                        '"off" for this profile.'
                    )
                else:
                    run.error = (
                        f"Model output reached model.max_tokens ({output_tokens}); "
                        "response may be incomplete."
                    )
                return
            calls = assistant.tool_calls or []
            if not calls and run.output_schema is not None:
                from solakit.core import structured

                try:
                    value = structured.extract(assistant.content or "")
                    problems = structured.errors(run.output_schema, value)
                except ValueError:
                    value, problems = None, ["reply is not valid JSON"]
                if not problems:
                    run.structured_output = value
                    if run.output_render:
                        try:
                            run.rendered_output = structured.render(
                                run.output_render, value
                            )
                        except Exception:
                            run.status = RunStatus.failed
                            run.error = "Structured output could not be rendered"
                            return
                elif structured_retries < 2 and work_steps + 1 < cfg.max_steps:
                    structured_retries += 1
                    await inject(
                        "structured_output",
                        "Reply with only a JSON value matching this schema: "
                        + json.dumps(run.output_schema)
                        + "\nProblems: "
                        + "; ".join(problems[:5]),
                        as_message=True,
                    )
                    work_steps += 1
                    step += 1
                    run.tool_view["work_steps"] = work_steps
                    continue
                else:
                    run.status = RunStatus.failed
                    run.error = (
                        "Structured output did not match the job schema: "
                        + "; ".join(problems[:3])
                    )[:300]
                    return
            if not calls:
                if cfg.plan_reminder and not reminded:
                    open_items = [
                        item["text"]
                        for item in plan_state(
                            await durable(self.store.plan_events, run.id)
                        )
                        if item["status"] in ("todo", "doing")
                    ]
                    if open_items and work_steps + 1 < cfg.max_steps:
                        reminded = True
                        await inject(
                            "plan_reminder",
                            "You have unfinished plan items: "
                            + "; ".join(open_items[:5])
                            + ". Finish them, or mark them skipped with a reason, "
                            "then answer.",
                            as_message=True,
                        )
                        work_steps += 1
                        step += 1
                        run.tool_view["work_steps"] = work_steps
                        continue
                run.tool_view["work_steps"] = work_steps + 1
                run.status = RunStatus.completed
                return
            exhausted = False
            disclosure_only = view.mode == "deferred"
            new_information = False
            for call in calls:
                function = call["function"]
                name = function["name"]
                discovery = view.mode == "deferred" and (
                    name in ("tools.search", "tools.load")
                    or (
                        name in view.effective
                        and name not in loaded_tools
                        and self.registry.allowed(name, run.profile)
                    )
                )
                disclosure_only = disclosure_only and discovery
                if exhausted:
                    content = (
                        "Error: Repair budget exhausted"
                        if cfg.tool_result_format == "text"
                        else json.dumps(
                            {"error": True, "content": "Repair budget exhausted"}
                        )
                    )
                else:
                    result = await self.registry.dispatch(
                        function["name"], function["arguments"], ctx
                    )
                    for artifact in result.artifacts:
                        if artifact not in run.artifacts:
                            run.artifacts.append(artifact)
                    key = (result._meta.get("name"), result._meta.get("args_hash"))
                    if result.error:
                        if key == last_failure:
                            result.content += (
                                "\nYou already tried exactly this. Change the "
                                "approach or answer without the tool."
                            )
                        last_failure = key
                    else:
                        last_failure = None
                    outcome = hashlib.sha256(result.content.encode()).hexdigest()
                    seen = repeats.setdefault(key, {"count": 0, "outcome": None})
                    seen["count"] = (
                        seen["count"] + 1 if seen["outcome"] == outcome else 1
                    )
                    seen["outcome"] = outcome
                    new_information |= outcome not in outcomes
                    outcomes.add(outcome)
                    if seen["count"] >= 3:
                        run.repeated_calls += 1
                        result.content += (
                            f"\nNote: this exact call has run {seen['count']} "
                            "times with the same outcome."
                        )
                        if cfg.repair_ceiling is not None:
                            exhausted |= repairs.fail(key[0], "repeat")
                    if result.error and result.repairable:
                        escalated = True
                        exhausted |= repairs.fail(
                            result._meta.get("name"), result._meta.get("error_class")
                        )
                    elif not result.error:
                        repairs.success(result._meta.get("name"))
                    content = render_for_model(result, cfg.tool_result_format)
                tool_message = Message(
                    role="tool",
                    content=content,
                    tool_call_id=call["id"],
                    origin=run.origin,
                    run_id=run.id,
                )
                await durable(self.store.append_message, session.id, tool_message)
                messages.append(tool_message.model_message())
                # A tool that finished despite a cancellation request keeps its
                # completed result in history; no further calls are dispatched.
                if pending.cancellation is not None:
                    raise asyncio.CancelledError
            if disclosure_only:
                disclosure_steps += 1
            else:
                work_steps += 1
            run.tool_view["disclosure_steps"] = disclosure_steps
            run.tool_view["work_steps"] = work_steps
            step += 1
            await durable(self.store.save_run, run)
            if disclosure_steps >= DISCLOSURE_STEPS:
                run.status = RunStatus.tool_error
                run.error = "tool loading did not converge"
                return
            if exhausted:
                run.status = RunStatus.tool_error
                return
            no_progress = 0 if new_information else no_progress + 1
            remaining = cfg.max_steps - work_steps
            if cfg.step_notice_at and 0 < remaining <= cfg.step_notice_at:
                await inject(
                    "step_notice",
                    f"[Runtime: {remaining} step(s) left. Finish soon; give the "
                    "best answer you can.]",
                )
            elif cfg.step_notice_at and no_progress >= 2:
                no_progress = 0
                await inject(
                    "no_progress",
                    "[Runtime: the last two steps produced no new information. "
                    "Change approach or finish with the best answer you can.]",
                )
        if cfg.wrapup != "off":
            await inject(
                "wrapup",
                "The step budget is used up. Do not call tools. Give your best "
                "final answer now from what you already have, and say what is "
                "unfinished.",
                as_message=True,
            )
            try:
                data = await self.model.complete(
                    self._aged(cfg, messages, run, step),
                    tools,
                    params=self.config.model.request.wire(
                        thinking="off" if cfg.model.thinking else None,
                        override=cfg.model,
                    ),
                    max_tokens=output_tokens,
                    tool_choice="none",
                )
            except ModelUnavailable:
                # The honest outcome stands: the step budget was exceeded.
                run.status = RunStatus.budget_exceeded
                return
            raw = data["choices"][0]["message"]
            text = (raw.get("content") or "").strip()
            if text and not raw.get("tool_calls"):
                assistant = Message(
                    role="assistant",
                    content=raw.get("content"),
                    origin=run.origin,
                    run_id=run.id,
                )
                await durable(self.store.append_message, session.id, assistant)
                run.completed_via_wrapup = True
                if (
                    cfg.wrapup == "answer"
                    and data["choices"][0].get("finish_reason") != "length"
                ):
                    run.status = RunStatus.completed
                    return
        run.status = RunStatus.budget_exceeded

    def _aged(self, cfg, messages, run, step) -> list[dict]:
        if cfg.tool_result_aging != "auto":
            return messages
        request, stubbed = age_tool_results(
            messages, cfg.aging_keep_groups, cfg.aging_min_chars
        )
        if stubbed:
            entry = {"step": step + 1, "stubbed": stubbed}
            if not run.aging or run.aging[-1]["stubbed"] != stubbed:
                run.aging.append(entry)
        return request

    def _over_budget(self, cfg, messages, tools, output_tokens, measured) -> bool:
        """Byte guard always; calibrated token budget when context_tokens is set.

        The previous step's server-reported prompt_tokens is an exact floor for
        the next request within a run (the prompt only grows)."""
        size = request_bytes(self.config.model.model_id, messages, tools, output_tokens)
        if size > cfg.context_budget:
            return True
        if not cfg.context_tokens:
            return False
        sample = self.store.calibration(self.config.model.model_id)
        if not sample:
            return False
        ratio = sample["bytes_per_token"]
        estimate = size / ratio
        if measured:
            estimate = max(
                estimate, measured["tokens"] + (size - measured["bytes"]) / ratio
            )
        return (
            estimate + output_tokens > cfg.context_tokens
            or estimate > 0.85 * cfg.context_tokens
        )
