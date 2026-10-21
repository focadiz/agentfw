"""A scripted chat model: replays a fixed sequence of replies, so the full agent loop runs offline.

Each script step is an AIMessage, or a function that receives the messages the
model was given and returns an AIMessage (to react to tool results). Use
tool_call() to build steps that call tools.
"""
from __future__ import annotations

import itertools
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

_ids = itertools.count(1)


def tool_call(name: str, **args: Any) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}])


class ScriptedModel(BaseChatModel):
    script: list[Any] = Field(default_factory=list)       # AIMessage | Callable[[list[BaseMessage]], AIMessage]
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    when_done: str = "Done."

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        step = self.script.pop(0) if self.script else AIMessage(self.when_done)
        message = step(list(messages)) if callable(step) else step
        message = message.model_copy(update={"usage_metadata": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}})
        return ChatResult(generations=[ChatGeneration(message=message)])
