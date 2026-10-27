from typing import Optional
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from .llm import _run_tool, get_chat_model

# the m2 chat agent (architecture section 4): a hand-built react loop, agent <-> tools.
# hand-built rather than create_react_agent so tool errors go through _run_tool, which only
# forwards text meant for the model (the prebuilt ToolNode sends raw exception text back)

SYSTEM_PROMPT = (
    "You are a personal finance assistant for one user. For anything about the user's own accounts, "
    "debts or spending, call get_accounts, get_debts or get_spending and use only the numbers they return; "
    "never guess the user's numbers. For general rules and strategies, call search_kb and cite the "
    "source_url of each passage you rely on. When a figure applies to a specific year, say which year. "
    'Keep answers short and concrete. End with one line: "This is general information, not financial advice."'
)
# a model that keeps calling tools gets one last call without them, so the user still gets an answer
MAX_TOOL_ROUNDS = 6


class ChatState(MessagesState):
    pseudonym: str      # the privacy gateway's pseudonym for this thread, reused every turn
    tool_rounds: int    # tool rounds in the current turn; the caller resets it to 0 per question


def build_chat_graph(tools: list[BaseTool], *, model: Optional[BaseChatModel] = None,
                     checkpointer: Optional[BaseCheckpointSaver] = None):
    model = model or get_chat_model()
    by_name = {t.name: t for t in tools}

    # the system prompt is sent every call but never stored, so a prompt change applies to old threads
    def agent(state: ChatState) -> dict:
        llm = model.bind_tools(tools) if state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS else model
        return {"messages": [llm.invoke([SystemMessage(SYSTEM_PROMPT)] + state["messages"])]}

    def run_tools(state: ChatState) -> dict:
        calls = state["messages"][-1].tool_calls
        return {
            "messages": [ToolMessage(_run_tool(by_name, tc), tool_call_id=tc["id"], name=tc["name"]) for tc in calls],
            "tool_rounds": state.get("tool_rounds", 0) + 1,
        }

    def route(state: ChatState) -> str:
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    graph = StateGraph(ChatState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
