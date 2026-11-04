"""Small provider-independent boundary for future assistant implementations."""

from typing import Protocol


class Agent(Protocol):
    async def run(self, message: str) -> str:
        """Process a message and return an assistant response."""
        ...


class Runtime:
    def __init__(self, agent: Agent | None = None) -> None:
        self.agent = agent

    async def run(self, message: str) -> str:
        if self.agent is None:
            raise AgentNotConfigured
        return await self.agent.run(message)


class AgentNotConfigured(Exception):
    """No agent implementation has been configured."""
