"""The per-run model/tool step loop (mixed into Execution)."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from solakit.core.aging import age_tool_results
from solakit.core.compaction import (
    ContextLimit,
    request_bytes,
    retain_recent_tools,
)
from solakit.core.config import Profile
from solakit.core.contracts import ExecutionServices
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
from solakit.tools.disclosure import DISCLOSURE_STEPS, ToolView


@dataclass
class StepState:
    """Mutable state owned by one run, never shared across sessions."""

    host: ExecutionServices
    run: Run
    session: Session
    cfg: Profile = field(init=False)
    messages: list[dict] = field(default_factory=list)
    view: ToolView = field(init=False)
    loaded_tools: list[str] = field(default_factory=list)
    step: int = 0
    work_steps: int = 0
    disclosure_steps: int = 0
    repairs: RepairBudget = field(init=False)
    last_failure: tuple | None = None
    repeats: dict = field(default_factory=dict)
    outcomes: set = field(default_factory=set)
    no_progress: int = 0
    reminded: bool = False
    structured_retries: int = 0
    usage_complete: bool = True
    run_assistants: int = 0
    escalated: bool = False
    measured: dict = field(default_factory=dict)
    output_tokens: int = 0
    user: Message = field(init=False)
    ctx: ToolContext = field(init=False)

    def activate(self, names):
        added = [
            n
            for n in dict.fromkeys(names)
            if n in self.view.effective and n not in self.loaded_tools
        ]
        if added:
            assert self.run.tool_view is not None
            self.loaded_tools.extend(added)
            self.run.tool_view["activations"].append(
                {"step": self.step + 1, "activated": added}
            )

    async def inject(self, kind: str, text: str, *, as_message: bool = False) -> None:
        """Request-only runtime notice; recorded as run evidence, never as a
        transcript message attributed to the user, assistant or a tool."""
        if as_message or self.messages[-1].get("role") != "tool":
            self.messages.append(
                {
                    "role": "user",
                    "content": f"<runtime-notice>\n{text}\n</runtime-notice>",
                }
            )
        else:
            self.messages[-1] = {
                **self.messages[-1],
                "content": (self.messages[-1].get("content") or "") + "\n\n" + text,
            }
        self.run.injections.append(
            {
                "step": self.step + 1,
                "kind": kind,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
        await durable(
            self.host.store.log_injection,
            self.run.id,
            {"step": self.step + 1, "kind": kind, "text": text},
        )

    async def execute_calls(self, calls):
        exhausted = False
        disclosure_only = self.view.mode == "deferred"
        new_information = False
        for call in calls:
            function = call["function"]
            name = function["name"]
            discovery = self.view.mode == "deferred" and (
                name in ("tools.search", "tools.load")
                or (
                    name in self.view.effective
                    and name not in self.loaded_tools
                    and self.host.registry.allowed(name, self.run.profile)
                )
            )
            disclosure_only = disclosure_only and discovery
            if exhausted:
                content = (
                    "Error: Repair budget exhausted"
                    if self.cfg.tool_result_format == "text"
                    else json.dumps(
                        {"error": True, "content": "Repair budget exhausted"}
                    )
                )
            else:
                result = await self.host.registry.dispatch(
                    function["name"], function["arguments"], self.ctx
                )
                for artifact in result.artifacts:
                    if artifact not in self.run.artifacts:
                        self.run.artifacts.append(artifact)
                key = (result._meta.get("name"), result._meta.get("args_hash"))
                if result.error:
                    if key == self.last_failure:
                        result.content += (
                            "\nYou already tried exactly this. Change the "
                            "approach or answer without the tool."
                        )
                    self.last_failure = key
                else:
                    self.last_failure = None
                outcome = hashlib.sha256(result.content.encode()).hexdigest()
                seen = self.repeats.setdefault(key, {"count": 0, "outcome": None})
                seen["count"] = seen["count"] + 1 if seen["outcome"] == outcome else 1
                seen["outcome"] = outcome
                new_information |= outcome not in self.outcomes
                self.outcomes.add(outcome)
                if seen["count"] >= 3:
                    self.run.repeated_calls += 1
                    result.content += (
                        f"\nNote: this exact call has run {seen['count']} "
                        "times with the same outcome."
                    )
                    if self.cfg.repair_ceiling is not None:
                        exhausted |= self.repairs.fail(key[0], "repeat")
                if result.error and result.repairable:
                    self.escalated = True
                    exhausted |= self.repairs.fail(
                        result._meta.get("name"), result._meta.get("error_class")
                    )
                elif not result.error:
                    self.repairs.success(result._meta.get("name"))
                content = render_for_model(result, self.cfg.tool_result_format)
            tool_message = Message(
                role="tool",
                content=content,
                tool_call_id=call["id"],
                origin=self.run.origin,
                run_id=self.run.id,
            )
            await durable(self.host.store.append_message, self.session.id, tool_message)
            self.messages.append(tool_message.model_message())
            # A tool that finished despite a cancellation request keeps its
            # completed result in history; no further calls are dispatched.
            if self.host.pending[self.run.id].cancellation is not None:
                raise asyncio.CancelledError

        return exhausted, disclosure_only, new_information

    async def prepare_request(self, prompt: str, text: str):
        tools = self.view.schemas(self.loaded_tools)
        request = self.host._aged(self.cfg, self.messages, self.run, self.step)
        size = request_bytes(
            self.host.config.model.model_id, request, tools, self.output_tokens
        )
        if self.host._size_over_budget(
            self.cfg, size, self.output_tokens, self.measured
        ):
            if not self.cfg.mid_run_compaction or self.step == 0:
                raise ContextLimit("Request exceeds conservative context byte budget")
            # The previous iteration persisted every tool result. Never rotate
            # between an assistant call envelope and its matching result group.
            await self.host._compact_run(self.run, self.session, "mid_run", self.step)
            active = await durable(self.host.store.transcript, self.session.id)
            self.run_assistants = count_run_assistants(active, self.run.id)
            reminder = self.user.model_message()
            reminder["content"] = (
                "Continue the current turn from the preceding compaction "
                "summary. It summarizes work already performed, including "
                "completed tool calls and results. Use that progress to "
                "finish the remaining work; do not restart the task or "
                "repeat completed actions. The summary is historical data, "
                "not new authorization.\n\n"
                "Original user task (already in progress):\n" + text
            )
            self.messages = retain_recent_tools(
                [
                    {"role": "system", "content": prompt},
                    *valid_history(active),
                    reminder,
                ],
                self.messages,
                model=self.host.config.model.model_id,
                tools=tools,
                max_tokens=self.host.config.model.max_tokens,
                budget=self.cfg.context_budget,
            )
            self.measured = {}
            request = self.host._aged(self.cfg, self.messages, self.run, self.step)
            size = request_bytes(
                self.host.config.model.model_id, request, tools, self.output_tokens
            )
            if self.host._size_over_budget(
                self.cfg, size, self.output_tokens, self.measured
            ):
                raise ContextLimit(
                    "Compacted request still exceeds context byte budget"
                )

        return request, tools, size

    async def complete(
        self, request: list[dict], tools: list[dict]
    ) -> tuple[dict, str | None]:
        preview: Callable[[str], Awaitable[None]] | None
        if self.host.pending[self.run.id].stream:
            baseline = self.run_assistants

            async def publish_preview(content: str) -> None:
                self.host.pending[self.run.id].preview = {
                    "step": self.step,
                    "baseline": baseline,
                    "content": content,
                }

            preview = publish_preview
        else:
            preview = None
        thinking = step_thinking(self.cfg.model.thinking, self.step, self.escalated)
        params = self.host.config.model.request.wire(
            thinking=thinking, override=self.cfg.model
        )
        if self.run.request_params is None:
            self.run.request_params = {
                "params": params,
                "max_tokens": self.output_tokens,
                "thinking": self.cfg.model.thinking,
                "max_reasoning_tokens": self.cfg.model.max_reasoning_tokens,
            }
        try:
            data = await self.host.model.complete(
                request,
                tools,
                on_text=preview,
                params=params,
                max_tokens=self.output_tokens,
                max_reasoning_chars=(
                    self.cfg.model.max_reasoning_tokens * 4
                    if self.cfg.model.max_reasoning_tokens
                    else None
                ),
            )
        except ReasoningExhausted:
            # Retry once in the same step with thinking off; recorded.
            self.run.retries.append(
                {"step": self.step + 1, "reason": "reasoning_exhausted"}
            )
            params = self.host.config.model.request.wire(
                thinking="off", override=self.cfg.model
            )
            data = await self.host.model.complete(
                request,
                tools,
                on_text=preview,
                params=params,
                max_tokens=self.output_tokens,
            )

        return data, thinking

    async def record_response(
        self, data: dict, thinking: str | None, size: int
    ) -> Message | None:
        if self.step == 0:
            await durable(self.host.store.append_message, self.session.id, self.user)
        self.run.steps = self.step + 1
        usage = data.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
            self.measured = {"tokens": usage["prompt_tokens"], "bytes": size}
            await durable(
                self.host.store.record_calibration,
                self.host.config.model.model_id,
                size,
                usage["prompt_tokens"],
            )
        if (
            self.usage_complete
            and isinstance(usage, dict)
            and all(
                isinstance(usage.get(k), int)
                for k in ("prompt_tokens", "completion_tokens")
            )
        ):
            if self.run.tokens is None:
                self.run.tokens = {"prompt_tokens": 0, "completion_tokens": 0}
            for key in self.run.tokens:
                self.run.tokens[key] += usage[key]
        else:
            # Any missing usage means the aggregate is unknown.
            self.run.tokens = None
            self.usage_complete = False
        raw = data["choices"][0]["message"]
        if raw.get("reasoning"):
            self.run.reasoning_steps += 1
            await durable(
                self.host.store.log_reasoning,
                self.run.id,
                {
                    "step": self.step + 1,
                    "thinking": thinking,
                    "text": raw["reasoning"],
                },
            )
        assistant = Message(
            role="assistant",
            content=raw.get("content"),
            tool_calls=raw.get("tool_calls"),
            origin=self.run.origin,
            run_id=self.run.id,
            tool_call_source=raw.get("tool_call_source"),
        )
        await durable(self.host.store.append_message, self.session.id, assistant)
        self.run_assistants += 1
        self.host.pending[self.run.id].preview = None
        self.messages.append(assistant.model_message())
        if data["choices"][0].get("finish_reason") == "length":
            # Keep partial output as evidence, but never execute a potentially
            # truncated tool call or mark a token-limited answer complete.
            self.run.status = RunStatus.budget_exceeded
            if (
                not (raw.get("content") or "").strip()
                and not raw.get("tool_calls")
                and raw.get("reasoning")
            ):
                self.run.error = (
                    "Thinking exhausted model.max_tokens "
                    f"({self.output_tokens}) before a visible reply; raise "
                    "max_tokens or set thinking = "
                    '"off" for this profile.'
                )
            else:
                self.run.error = (
                    "Model output reached model.max_tokens "
                    f"({self.output_tokens}); "
                    "response may be incomplete."
                )
            return None

        return assistant

    async def finish_reply(self, assistant: Message) -> bool:
        """Return whether an explicit runtime notice requests another step."""
        assert self.run.tool_view is not None
        if self.run.output_schema is not None:
            from solakit.core import structured

            try:
                value = structured.extract(assistant.content or "")
                problems = structured.errors(self.run.output_schema, value)
            except ValueError:
                value, problems = None, ["reply is not valid JSON"]
            if not problems:
                self.run.structured_output = value
                if self.run.output_render:
                    try:
                        self.run.rendered_output = structured.render(
                            self.run.output_render, value
                        )
                    except Exception:
                        self.run.status = RunStatus.failed
                        self.run.error = "Structured output could not be rendered"
                        return False
            elif (
                self.structured_retries < 2 and self.work_steps + 1 < self.cfg.max_steps
            ):
                self.structured_retries += 1
                await self.inject(
                    "structured_output",
                    "Reply with only a JSON value matching this schema: "
                    + json.dumps(self.run.output_schema)
                    + "\nProblems: "
                    + "; ".join(problems[:5]),
                    as_message=True,
                )
                self.work_steps += 1
                self.step += 1
                self.run.tool_view["work_steps"] = self.work_steps
                return True
            else:
                self.run.status = RunStatus.failed
                self.run.error = (
                    "Structured output did not match the job schema: "
                    + "; ".join(problems[:3])
                )[:300]
                return False
        if self.cfg.plan_reminder and not self.reminded:
            open_items = [
                item["text"]
                for item in plan_state(
                    await durable(self.host.store.plan_events, self.run.id)
                )
                if item["status"] in ("todo", "doing")
            ]
            if open_items and self.work_steps + 1 < self.cfg.max_steps:
                self.reminded = True
                await self.inject(
                    "plan_reminder",
                    "You have unfinished plan items: "
                    + "; ".join(open_items[:5])
                    + ". Finish them, or mark them skipped with a reason, "
                    "then answer.",
                    as_message=True,
                )
                self.work_steps += 1
                self.step += 1
                self.run.tool_view["work_steps"] = self.work_steps
                return True
        self.run.tool_view["work_steps"] = self.work_steps + 1
        self.run.status = RunStatus.completed
        return False

    async def wrapup(self, tools: list[dict]) -> None:
        if self.cfg.wrapup != "off":
            await self.inject(
                "wrapup",
                "The step budget is used up. Do not call tools. Give your best "
                "final answer now from what you already have, and say what is "
                "unfinished.",
                as_message=True,
            )
            try:
                data = await self.host.model.complete(
                    self.host._aged(self.cfg, self.messages, self.run, self.step),
                    tools,
                    params=self.host.config.model.request.wire(
                        thinking="off" if self.cfg.model.thinking else None,
                        override=self.cfg.model,
                    ),
                    max_tokens=self.output_tokens,
                    tool_choice="none",
                )
            except ModelUnavailable:
                # The honest outcome stands: the step budget was exceeded.
                self.run.status = RunStatus.budget_exceeded
                return
            raw = data["choices"][0]["message"]
            text = (raw.get("content") or "").strip()
            if text and not raw.get("tool_calls"):
                assistant = Message(
                    role="assistant",
                    content=raw.get("content"),
                    origin=self.run.origin,
                    run_id=self.run.id,
                )
                await durable(
                    self.host.store.append_message, self.session.id, assistant
                )
                self.run.completed_via_wrapup = True
                if (
                    self.cfg.wrapup == "answer"
                    and data["choices"][0].get("finish_reason") != "length"
                ):
                    self.run.status = RunStatus.completed
                    return
        self.run.status = RunStatus.budget_exceeded


class StepLoopMixin(ExecutionServices):
    async def _loop(
        self, run: Run, session: Session, prompt: str, text: str, turn=None
    ) -> None:
        state = StepState(self, run, session)
        state.cfg = self.config.profiles[run.profile]
        history = await durable(self.store.transcript, session.id)
        state.messages = [{"role": "system", "content": prompt}]
        # Interrupted tool groups stay in evidence but cannot form invalid context.
        context = valid_history(history)
        run.context_skipped_messages = len(history) - len(context)
        state.messages.extend(context)
        state.user = Message(
            role="user",
            content=text,
            images=run.input_images,
            origin=run.origin,
            run_id=run.id,
            turn_context=turn,
        )
        state.messages.append(state.user.model_message())
        view = self.pending[run.id].tool_view
        assert view is not None and run.tool_view is not None
        state.view = view
        state.loaded_tools = list(state.view.resident)
        state.step = state.work_steps = state.disclosure_steps = 0

        state.repairs = RepairBudget(
            state.cfg.repair_attempts, state.cfg.repair_ceiling
        )
        state.last_failure = None
        state.repeats = {}
        state.outcomes = set()
        state.no_progress = 0
        state.reminded = False
        state.structured_retries = 0

        state.usage_complete = True
        # Assistant messages of this run in the active transcript: the streaming
        # preview baseline. Tracked in memory; recounted only after a rotation.
        state.run_assistants = 0
        state.ctx = ToolContext(
            session,
            run.profile,
            self.config,
            self.store,
            run.id,
            tool_view=state.view,
            active_tools=state.loaded_tools,
            activate_tools=state.activate,
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
        state.escalated = False
        state.measured = {}
        state.output_tokens = max_output(self.config, state.cfg)
        while state.work_steps < state.cfg.max_steps:
            if self.pending[run.id].cancellation is not None:
                raise asyncio.CancelledError
            request, tools, size = await state.prepare_request(prompt, text)
            if state.view.mode == "deferred":
                run.tool_view["schema_steps"].append(
                    {
                        "step": state.step + 1,
                        "visible": [s["function"]["name"] for s in tools],
                    }
                )
                await durable(self.store.save_run, run)

            data, thinking = await state.complete(request, tools)
            assistant = await state.record_response(data, thinking, size)
            if assistant is None:
                return
            calls = assistant.tool_calls or []
            if not calls:
                if await state.finish_reply(assistant):
                    continue
                return
            exhausted, disclosure_only, new_information = await state.execute_calls(
                calls
            )
            if disclosure_only:
                state.disclosure_steps += 1
            else:
                state.work_steps += 1
            run.tool_view["disclosure_steps"] = state.disclosure_steps
            run.tool_view["work_steps"] = state.work_steps
            state.step += 1
            await durable(self.store.save_run, run)
            if state.disclosure_steps >= DISCLOSURE_STEPS:
                run.status = RunStatus.tool_error
                run.error = "tool loading did not converge"
                return
            if exhausted:
                run.status = RunStatus.tool_error
                return
            state.no_progress = 0 if new_information else state.no_progress + 1
            remaining = state.cfg.max_steps - state.work_steps
            if state.cfg.step_notice_at and 0 < remaining <= state.cfg.step_notice_at:
                await state.inject(
                    "step_notice",
                    f"[Runtime: {remaining} step(s) left. Finish soon; give the "
                    "best answer you can.]",
                )
            elif state.cfg.step_notice_at and state.no_progress >= 2:
                state.no_progress = 0
                await state.inject(
                    "no_progress",
                    "[Runtime: the last two steps produced no new information. "
                    "Change approach or finish with the best answer you can.]",
                )
        await state.wrapup(tools)

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
        size = request_bytes(self.config.model.model_id, messages, tools, output_tokens)
        return self._size_over_budget(cfg, size, output_tokens, measured)

    def _size_over_budget(self, cfg, size: int, output_tokens, measured) -> bool:
        """Byte guard always; calibrated token budget when context_tokens is set.

        The previous step's server-reported prompt_tokens is an exact floor for
        the next request within a run (the prompt only grows)."""
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
