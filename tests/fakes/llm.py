"""Scripted LLM doubles. Tests queue responses; any unexpected extra call fails loudly."""

from __future__ import annotations

from collections import deque
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, ConfigDict


class FakeLLM:
    def __init__(self) -> None:
        self.ai_queue: deque[AIMessage | Exception] = deque()
        self.structured_queue: deque[BaseModel | Exception] = deque()
        self.calls: list[list[BaseMessage]] = []
        self.structured_calls: list[dict[str, Any]] = []

    # --- scripting -----------------------------------------------------------
    def push_ai(self, msg: AIMessage) -> None:
        self.ai_queue.append(msg)

    def push_text(self, text: str) -> None:
        self.ai_queue.append(AIMessage(content=text))

    def push_structured(self, obj: BaseModel) -> None:
        self.structured_queue.append(obj)

    def push_error(self, exc: Exception, structured: bool = False) -> None:
        (self.structured_queue if structured else self.ai_queue).append(exc)

    # --- consumption ---------------------------------------------------------
    def pop_ai(self) -> AIMessage:
        if not self.ai_queue:
            raise AssertionError("FakeLLM: no scripted AI message left")
        item = self.ai_queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def pop_structured(self, schema: type[BaseModel] | None = None) -> BaseModel:
        if not self.structured_queue:
            raise AssertionError("FakeLLM: no scripted structured output left")
        item = self.structured_queue.popleft()
        if isinstance(item, Exception):
            raise item
        if schema is not None and not isinstance(item, schema):
            raise AssertionError(f"FakeLLM: expected {schema.__name__}, got {type(item).__name__}")
        return item

    # --- drop-in replacements for mavis.llm.models ----------------------------
    def chat_model(
        self, tier: Any = None, temperature: float = 0.6, model: str | None = None
    ) -> FakeToolChatModel:
        return FakeToolChatModel(fake=self)

    async def structured(
        self, schema: type[BaseModel], system: str, user: Any, tier: Any = None,
        priority: str = "interactive", fallback: bool | None = None,
    ) -> BaseModel:
        self.structured_calls.append({"schema": schema, "system": system, "user": user, "tier": tier})
        return self.pop_structured(schema)


class FakeToolChatModel(BaseChatModel):
    """A chat model that returns queued AIMessages (with or without tool_calls).

    Works inside LangGraph ReAct agents: bind_tools() returns a copy that remembers the tools.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    fake: Any
    bound_tools: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake-tool-chat"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.fake.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.fake.pop_ai())])

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolChatModel:  # type: ignore[override]
        return FakeToolChatModel(fake=self.fake, bound_tools=list(tools))

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:  # type: ignore[override]
        target = schema if isinstance(schema, type) else None
        return RunnableLambda(lambda _input: self.fake.pop_structured(target))
