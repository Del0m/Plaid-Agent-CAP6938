from typing import Optional
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_ollama import ChatOllama
from .. import config

# the only place the app creates an LLM client (architecture section 3), so swapping
# llama3.1 for mistral or a hosted model in benchmarks is a change to this file only


def get_chat_model(temperature: Optional[float] = None, model: Optional[str] = None) -> BaseChatModel:
    # config is read at call time so tests (and later, per-run eval settings) can change it
    return ChatOllama(
        model=model or config.OLLAMA_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
    )


# smallest possible tool loop, here to prove tool calls round-trip on the local model.
# TPC-15 replaces it with a LangGraph ReAct graph.
def call_with_tools(
    messages: list[BaseMessage],
    tools: list[BaseTool],
    *,
    model: Optional[BaseChatModel] = None,
    max_steps: int = 3,
) -> AIMessage:
    bound = (model or get_chat_model()).bind_tools(tools)
    by_name = {t.name: t for t in tools}
    history = list(messages)

    for _ in range(max_steps):
        reply = bound.invoke(history)
        history.append(reply)
        if not reply.tool_calls:
            return reply
        for tc in reply.tool_calls:
            history.append(ToolMessage(content=_run_tool(by_name, tc), tool_call_id=tc["id"]))

    raise RuntimeError(f"tool loop did not finish in {max_steps} steps")


# errors go back to the model as text so it can recover; small models do invent tool names
def _run_tool(by_name: dict, tc: dict) -> str:
    t = by_name.get(tc["name"])
    if t is None:
        return f"error: unknown tool {tc['name']}"
    try:
        return str(t.invoke(tc["args"]))
    except Exception as exc:
        return f"error: {exc}"
