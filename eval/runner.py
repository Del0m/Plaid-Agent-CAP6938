import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.chat_graph import build_chat_graph
from app.agents.tools import make_tools
from app.privacy.gateway import PrivacyGateway
from eval.fixtures import Scenario, seed, temp_db

# one scenario, one run, through the same graph and tools POST /chat builds (spec: tpc-16 runner).
# never raises: a failed run is a result to score, not a reason to stop the eval


@dataclass
class RunRecord:
    scenario_id: str
    run: int
    answer: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    latency_s: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def run_once(scenario: Scenario, *, model, kb, audit_path: Path, run: int = 1,
             today: Optional[date] = None) -> RunRecord:
    rec = RunRecord(scenario_id=scenario.id, run=run)
    start = None
    try:
        with temp_db() as Session, Session() as session:
            user_id = seed(session, scenario, today or date.today())
            tools = make_tools(PrivacyGateway(session, user_id, audit_path=audit_path), kb)
            graph = build_chat_graph(tools, retrieve=kb.search, model=model)
            # timed from here: the answer, not the throwaway database and seeding around it
            start = time.perf_counter()
            state = graph.invoke({"messages": [HumanMessage(scenario.question)], "tool_rounds": 0})
            rec.latency_s = time.perf_counter() - start
        messages = state["messages"]
        for m in messages:
            if isinstance(m, AIMessage):
                rec.tool_calls += [{"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls]
            elif isinstance(m, ToolMessage):
                rec.tool_results.append(str(m.content))
        final = [m for m in messages if isinstance(m, AIMessage) and not m.tool_calls]
        rec.answer = final[-1].content if final else ""
        rec.sources = list(state.get("sources", []))
    except Exception as exc:
        rec.error = f"{type(exc).__name__}: {exc}"
        rec.tool_calls, rec.tool_results, rec.sources, rec.answer = [], [], [], ""
        # a run that failed during setup never started answering
        rec.latency_s = time.perf_counter() - start if start is not None else 0.0
    return rec
