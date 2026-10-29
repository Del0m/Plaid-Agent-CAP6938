import json
import threading
import time
from typing import Any, Optional

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field

from app import config

# test doubles shared by the chat graph, chat api and tools tests, plus the live-test gate


# replays scripted replies (AIMessage, or an Exception to raise) and records every call.
# a real BaseChatModel, so langgraph's "messages" stream mode sees its tokens; text replies
# stream word by word. bind_tools goes through bind(), so each call records the tools it had
class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    replies: list
    seen: list = Field(default_factory=list)      # messages sent, one list per call
    tools_seen: list = Field(default_factory=list)  # tool names bound on each call, None if unbound
    kwargs_seen: list = Field(default_factory=list)  # every call's kwargs (e.g. a bound ollama `format`)
    gate: Optional[threading.Event] = None         # first reply waits on it (holds a stream open)
    token_delay: float = 0.0                       # pause between streamed words (a slow model mid-answer)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[t.name for t in tools])

    def _next(self, messages, kwargs) -> AIMessage:
        self.seen.append(list(messages))
        self.tools_seen.append(kwargs.get("tools"))
        self.kwargs_seen.append(dict(kwargs))
        if self.gate is not None and len(self.seen) == 1:
            self.gate.wait(timeout=10)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages, kwargs))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any):
        reply = self._next(messages, kwargs)
        if reply.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content=reply.content, tool_call_chunks=[
                {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                for i, tc in enumerate(reply.tool_calls)]))
            return
        words = reply.content.split(" ")
        for i, word in enumerate(words):
            if i:
                time.sleep(self.token_delay)
            text = word if i == len(words) - 1 else word + " "
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=text))
            if run_manager:
                run_manager.on_llm_new_token(text, chunk=chunk)
            yield chunk


def tool_call(name: str, args: Optional[dict] = None, id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": id}])


# stands in for KnowledgeBase: returns one canned result and records each search
class FakeKB:
    RESULT = {"title": "How to reduce your debt", "section": "Paying down debt",
              "source_url": "https://www.consumerfinance.gov/about-us/blog/how-reduce-your-debt/",
              "text": "Pay the highest interest debt first."}

    def __init__(self, chunks=1):
        self.chunks, self.searches = chunks, []

    def count(self):
        return self.chunks

    def search(self, query, k=4):
        self.searches.append((query, k))
        return [self.RESULT] if self.chunks else []   # like KnowledgeBase: nothing ingested, nothing found


# live tests run only when ollama lists every model they need; a bare name means ":latest"
def ollama_has(*models: str) -> bool:
    try:
        names = [m["name"] for m in httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=2).json()["models"]]
    except Exception:
        return False
    return all(m in names or f"{m}:latest" in names for m in models)



# answers any structured call for the schema it was bound to (tpc-21's simulations can't script every reply):
# a one_time diagnosis, then "alert" for a category's first flag of the month and "no_op" once it was alerted
class SchemaReplyModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "schema-reply"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        title = (kwargs.get("format") or {}).get("title")
        if title == "Diagnosis":
            body = {"cause": "one_time", "evidence": "One purchase explains it."}
        elif "Earlier alerts for this category this month: none" in str(messages[-1].content):
            body = {"action": "alert", "message": "This category is over budget."}
        else:
            body = {"action": "no_op", "message": "Already alerted."}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(body)))])
