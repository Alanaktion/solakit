"""Static operator advice for failed runs. Advice only: evidence is unchanged."""


def failure_hint(run, config, tool_logs: list[dict] | None = None) -> str | None:
    status = str(run.status)
    error = run.error or ""
    profile = config.profiles.get(run.profile)
    if status == "budget_exceeded":
        if "Thinking exhausted" in error:
            return (
                f'Set [profiles.{run.profile}.model] thinking = "off" or raise '
                "max_tokens for this profile."
            )
        if "max_tokens" in error:
            return "Raise [model].max_tokens or the profile's model.max_tokens."
        return (
            f"The run used all {profile.max_steps if profile else '?'} steps: raise "
            "max_steps, enable wrapup, or narrow the task."
        )
    if status == "context_limit":
        if config.compaction.strategy == "openviking" and not config.memory.enabled:
            return 'Enable local compaction: [compaction] strategy = "auto".'
        return "Raise context_budget/context_tokens or compact/archive the session."
    if status == "tool_error":
        if "did not converge" in error:
            return "Deferred tool loading did not converge; add tool_resident entries."
        failing = [
            log
            for log in (tool_logs or [])
            if (log.get("result") or {}).get("repairable")
        ]
        if failing:
            last = failing[-1]
            return (
                f"Repeated invalid calls to {last.get('name')}; check its schema "
                "on the Model health page or set repair_ceiling."
            )
        return "A tool failed repeatedly; inspect the tool log on this run."
    if status == "model_unavailable":
        if "Empty completion" in error:
            return (
                "The backend returned no usable text or tool calls; inspect this "
                "run's rejected-attempt diagnostics and offered tool schemas."
            )
        return "Run sola doctor to check the model endpoint and pinned id."
    if status == "memory_unavailable":
        return "Check the OpenViking endpoint, user and roots (sola doctor)."
    return None
