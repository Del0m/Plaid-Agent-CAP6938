import logging
from typing import Optional
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, ToolException
from langchain_ollama import ChatOllama
from pydantic import ValidationError
from .. import config

logger = logging.getLogger(__name__)

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


# errors go back to the model as text so it can recover; small models do invent tool names.
# only messages meant for the model are forwarded: any other exception text can carry user
# data (sqlalchemy prints bound parameters) and this path bypasses the privacy gateway
def _run_tool(by_name: dict, tc: dict) -> str:
    name = tc["name"]
    t = by_name.get(name)
    if t is None:
        return f"error: unknown tool {name}"
    try:
        return str(t.invoke(tc["args"]))
    except (ToolException, ValidationError) as exc:
        return f"error: {exc}"
    except Exception as exc:
        logger.warning("tool %s failed", name, exc_info=True)   # full detail stays in local logs
        return f"error: {name} failed ({type(exc).__name__})"
