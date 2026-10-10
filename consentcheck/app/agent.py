"""Bounded agent orchestration; provider adapters own conversation formatting."""
from dataclasses import dataclass
from typing import Any, Protocol
import re

from .tools import ToolRunner


@dataclass
class ToolCall:
    name: str
    arguments: dict
    call_id: str | None = None


@dataclass
class Turn:
    calls: list[ToolCall]
    text: str = ""
    native: Any = None


class ProviderError(Exception):
    """Contains a safe, user-facing message, never raw provider exceptions."""


class Provider(Protocol):
    def start(self, task: str) -> None: ...
    def next_turn(self) -> Turn: ...
    def record_results(self, turn: Turn, results: list[dict]) -> None: ...


def run_agent(task: str, provider: Provider, max_turns: int = 8) -> dict:
    runner = ToolRunner()
    activity: list[dict] = []
    provider.start(task)
    status, message = "limit_reached", "Agent stopped at its turn limit. Try a more specific task."
    try:
        for _ in range(max_turns):
            turn = provider.next_turn()
            if not turn.calls:
                status = "completed"
                message = turn.text.strip()
                # Execution confirmations must come from the backend, never model prose.
                if re.search(
                    r"\b(?:sent|emailed|delivered|executed|approved)\b", message, re.IGNORECASE
                ):
                    message = ""
                if runner.proposals:
                    message = (message + "\n\n" if message else "") + "Please review the action below and choose Approve or Reject."
                elif not message:
                    message = "I can help prepare or summarize documents. Send proposals require your approval and are simulated only. What would you like to work on?"
                break
            if len(activity) + len(turn.calls) > 24:
                message = "Agent stopped at its tool-call limit."
                break
            results = []
            for call in turn.calls:
                result = runner.execute(call.name, call.arguments)
                activity.append({"tool": call.name, "arguments": call.arguments, "result": result})
                results.append(result)
            provider.record_results(turn, results)
    except ProviderError as exc:
        status, message = "error", str(exc)
    return {"status": status, "message": message, "activity": activity,
            "proposals": runner.proposals, "sent": False}
