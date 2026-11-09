"""Pure step-loop helpers: repair accounting, thinking schedule, history
validity and output budgets. No queues, locks or I/O."""

from solakit.core.config import Config
from solakit.core.schemas import Message


class RepairBudget:
    """Repairable-error accounting.

    Without a ceiling (default) this is the historical rule: more than
    ``attempts`` repairs in a run is exhaustion. With a ceiling, each tool +
    error class gets ``attempts`` repairs, the run total is capped by the
    ceiling, and a successful call of the tool that just failed refunds one."""

    def __init__(self, attempts: int, ceiling: int | None = None):
        self.attempts, self.ceiling = attempts, ceiling
        self.total = 0
        self.classes: dict[tuple, int] = {}
        self.last_failed: str | None = None

    def fail(self, tool: str | None, error_class: str | None) -> bool:
        self.total += 1
        if self.ceiling is None:
            return self.total > self.attempts
        key = (tool, error_class)
        self.classes[key] = self.classes.get(key, 0) + 1
        self.last_failed = tool
        return self.classes[key] > self.attempts or self.total > self.ceiling

    def success(self, tool: str | None) -> None:
        if self.ceiling is not None and tool == self.last_failed and self.total:
            self.total -= 1
        self.last_failed = None


def max_output(config: Config, profile) -> int:
    return profile.model.max_tokens or config.model.max_tokens


def step_thinking(mode: str | None, step: int, escalated: bool) -> str | None:
    """Per-step thinking. ``auto``: on for the fresh user message and after a
    repairable tool error; off for plain tool-result continuations."""
    if mode == "auto":
        return "on" if step == 0 or escalated else "off"
    return mode


def count_run_assistants(messages: list[dict], run_id: str) -> int:
    return sum(
        m["role"] == "assistant"
        and m.get("kind", "message") == "message"
        and m["run_id"] == run_id
        for m in messages
    )


def valid_history(history: list[dict]) -> list[dict]:
    """Include only complete assistant/tool groups; retain every source entry."""
    result = []
    index = 0
    while index < len(history):
        message = Message.model_validate(history[index])
        calls = message.tool_calls or []
        if calls:
            following = history[index + 1 : index + 1 + len(calls)]
            expected = [call["id"] for call in calls]
            actual = [
                item.get("tool_call_id")
                for item in following
                if item.get("role") == "tool"
            ]
            if expected == actual:
                result.append(message.model_message())
                result.extend(
                    Message.model_validate(item).model_message() for item in following
                )
                index += len(calls)
        elif message.role != "tool":
            result.append(message.model_message())
        index += 1
    return result
