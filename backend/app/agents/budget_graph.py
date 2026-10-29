import argparse
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, timedelta
from typing import Callable, Optional, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.budget_llm import Action, Diagnosis, decide_messages, diagnose_messages, invoke_structured
from app import config
from app.agents.llm import get_chat_model
from app.budget_engine.diagnose import overspend_facts, proposed_limit_cents
from app.budget_engine.pace import BudgetLine as EngineLine, PaceResult, evaluate
from app.budget_engine.txns import add_months
from app.budget_service.initialize import initialize_budget, load_txns
from app.db.models import AdviceLog, AgentRun, Alert, BudgetPeriod, PlaidItem, utcnow
from app.plaid_service.sync import sync_item
from app.privacy.gateway import PrivacyGateway

# the daily budget agent (architecture 6.1-6.2, spec: tpc-19). the budget engine does the maths; the llm
# names each overspend's cause from facts the code computed and picks an action. an llm that can't answer
# never hides an overspend: the graph falls back to a plain alert

SyncFn = Callable[[Session, str], dict]


# plain values only, so tpc-20 can add a checkpointer and interrupt() without reshaping it
class BudgetState(TypedDict, total=False):
    user_id: str
    today: str
    run_id: int
    period_id: Optional[int]
    status: str                 # running | ok | no_budget | period_ended | error
    sync: dict
    uncategorized: int
    results: list[dict]         # every PaceResult
    flags: list[dict]           # the flagged ones
    facts: dict[str, dict]      # category -> OverspendFacts, plus its proposed limit
    contexts: dict[str, str]    # category -> the gateway's prompt context, built (and audited) once
    diagnoses: dict[str, Optional[dict]]
    actions: dict[str, dict]
    alert_ids: list[int]
    adjust_alert_ids: list[int]   # alerts that would change the budget, so the run waits for the user
    decisions: dict               # alert id -> accepted | dismissed, from the user
    waiting_for: list[int]
    llm_failures: int


# default sync: every active plaid item the user has. one failing item doesn't stop the others
def plaid_sync(session: Session, user_id: str) -> dict:
    items = session.scalars(select(PlaidItem).where(PlaidItem.user_id == user_id,
                                                    PlaidItem.status == "active")).all()
    errors = []
    for item in items:
        try:
            sync_item(session, item)
        except Exception as exc:
            session.rollback()
            errors.append(type(exc).__name__)
    return {"items": len(items), "errors": errors}


def _severity(reason: str) -> str:
    return "high" if reason == "over_limit" else "medium"


# memory, read by code (tpc-20): this period's overspend alerts for the category, oldest first
def _this_month_alerts(s: Session, user_id: str, period_id: int, category: str) -> list[dict]:
    alerts = s.scalars(select(Alert).where(Alert.user_id == user_id, Alert.period_id == period_id,
                                           Alert.kind == "overspend", Alert.category == category)
                       .order_by(Alert.created_at, Alert.id))
    return [{"severity": a.severity, "action": a.action, "status": a.status} for a in alerts]


# the category's advice in this budget month and the 3 before it, newest first. keyed by the alert's budget
# month, not created_at: runs can be simulated on other dates than the wall clock
def _past_advice(s: Session, user_id: str, period_start: date, category: str) -> list[dict]:
    rows = s.execute(
        select(AdviceLog, Alert, BudgetPeriod)
        .join(Alert, AdviceLog.alert_id == Alert.id)
        .join(BudgetPeriod, Alert.period_id == BudgetPeriod.id)
        .where(AdviceLog.user_id == user_id, AdviceLog.category == category,
               BudgetPeriod.start_date >= add_months(period_start, -3), BudgetPeriod.start_date <= period_start)
        .order_by(BudgetPeriod.start_date.desc(), AdviceLog.id.desc())
        .limit(5)).all()
    return [{"month": p.start_date.strftime("%B %Y"), "action": a.action, "outcome": adv.outcome}
            for adv, a, p in rows]


def _label(category: str) -> str:
    return category.replace("_", " ").capitalize()


# the message when the model can't decide: the plain numbers, written by code
def _fallback_message(flag: dict) -> str:
    spent, limit = flag["spent_cents"] / 100, flag["limit_cents"] / 100
    if flag["reason"] == "over_limit":
        return f"{_label(flag['category'])} is over its ${limit:,.2f} limit: ${spent:,.2f} so far."
    return (f"{_label(flag['category'])} is at {flag['pace']:.0%} of its pace: ${spent:,.2f} of ${limit:,.2f} "
            f"by day {flag['days_elapsed']}.")


def build_budget_graph(session_factory, *, model: Optional[BaseChatModel], gateway: Optional[PrivacyGateway],
                       sync_fn: Optional[SyncFn] = None, checkpointer: Optional[BaseCheckpointSaver] = None):
    def today_of(state) -> date:
        return date.fromisoformat(state["today"])

    def load_state(state: BudgetState) -> dict:
        today = today_of(state)
        with session_factory() as s:
            period = initialize_budget(s, state["user_id"], today)
            if period is None or today < period.start_date:
                return {"status": "no_budget", "period_id": None}
            if today > period.end_date:
                return {"status": "period_ended", "period_id": period.id}   # closing it is tpc-22's job
            return {"period_id": period.id}

    def sync(state: BudgetState) -> dict:
        if sync_fn is None:
            return {"sync": {"skipped": True}}
        with session_factory() as s:
            try:
                return {"sync": sync_fn(s, state["user_id"])}
            except Exception as exc:
                # plaid being down shouldn't stop the budget check; it runs on the data already stored
                s.rollback()
                return {"sync": {"items": 0, "errors": [type(exc).__name__]}}

    def period_txns(s, state):
        period = s.get(BudgetPeriod, state["period_id"])
        return period, load_txns(s, state["user_id"], period.start_date, today_of(state))

    def categorize(state: BudgetState) -> dict:
        # llm categorization is a follow-up; for now the run only reports how much plaid left uncategorized
        with session_factory() as s:
            _, txns = period_txns(s, state)
        return {"uncategorized": sum(1 for t in txns if t.category == "UNCATEGORIZED")}

    def update_totals(state: BudgetState) -> dict:
        with session_factory() as s:
            period, txns = period_txns(s, state)
            results = evaluate([EngineLine(ln.category, ln.kind, ln.limit_cents) for ln in period.lines], txns,
                               today_of(state))
            by_cat = {r.category: r for r in results}
            for ln in period.lines:
                ln.spent_cents, ln.last_pace = by_cat[ln.category].spent_cents, by_cat[ln.category].pace
            s.commit()
        return {"results": [asdict(r) for r in results]}

    def detect_overspend(state: BudgetState) -> dict:
        return {"flags": [r for r in state["results"] if r["flagged"]]}

    def diagnose(state: BudgetState) -> dict:
        facts, contexts, diagnoses, failures = {}, {}, {}, state.get("llm_failures", 0)
        with session_factory() as s:
            period, txns = period_txns(s, state)
            history = load_txns(s, state["user_id"], add_months(period.start_date, -3),
                                period.start_date - timedelta(days=1))
            fits = {ln.category: ln.fit_json for ln in period.lines}
            memory = {f["category"]: (_this_month_alerts(s, state["user_id"], period.id, f["category"]),
                                      _past_advice(s, state["user_id"], period.start_date, f["category"]))
                      for f in state["flags"]}
        for flag in state["flags"]:
            cat = flag["category"]
            f = overspend_facts(PaceResult(**flag), fits.get(cat), txns, history, period.start_date)
            facts[cat] = {**asdict(f), "proposed_limit_cents": proposed_limit_cents(f)}
            contexts[cat] = gateway.build_context("diagnose", facts=f, transactions=txns,
                                                  this_month_alerts=memory[cat][0], past_advice=memory[cat][1])
            d = invoke_structured(model, Diagnosis, diagnose_messages(contexts[cat]))
            diagnoses[cat] = d.model_dump() if d else None
            failures += d is None
        return {"facts": facts, "contexts": contexts, "diagnoses": diagnoses, "llm_failures": failures}

    def decide_action(state: BudgetState) -> dict:
        actions, failures = {}, state["llm_failures"]
        for flag in state["flags"]:
            cat = flag["category"]
            d, f = state["diagnoses"][cat], state["facts"][cat]
            a = invoke_structured(model, Action, decide_messages(
                state["contexts"][cat], Diagnosis(**d) if d else None, f["projected_cents"],
                f["proposed_limit_cents"], _severity(flag["reason"])))
            fallback = a is None
            if fallback:
                failures += 1
                a = Action(action="alert", message=_fallback_message(flag))
            # only a recurring cause may change next month's budget; the model's message would quote a limit
            # it shouldn't have proposed, so the code writes the alert instead
            downgraded = a.action == "adjust_next_budget" and (d or {}).get("cause") != "recurring"
            if downgraded:
                a = Action(action="alert", message=_fallback_message(flag))
            actions[cat] = {"action": a.action, "message": a.message, "fallback": fallback, "downgraded": downgraded,
                            "proposed_limit_cents": f["proposed_limit_cents"] if a.action == "adjust_next_budget" else None}
        return {"actions": actions, "llm_failures": failures}

    def log_advice(state: BudgetState) -> dict:
        ids, adjust = [], []
        with session_factory() as s:
            for flag in state["flags"]:
                cat, act, d = flag["category"], state["actions"][flag["category"]], state["diagnoses"][flag["category"]]
                if act["action"] == "no_op":
                    continue
                alert = Alert(user_id=state["user_id"], period_id=state["period_id"], kind="overspend",
                              category=cat, severity=_severity(flag["reason"]),
                              cause=d["cause"] if d else None, action=act["action"], message=act["message"],
                              proposed_limit_cents=act["proposed_limit_cents"], status="pending_user")
                s.add(alert)
                s.flush()       # the advice row needs the alert's id
                s.add(AdviceLog(user_id=state["user_id"], run_id=state["run_id"], alert_id=alert.id, category=cat,
                                advice_text=act["message"]))
                ids.append(alert.id)
                if act["action"] == "adjust_next_budget":
                    adjust.append(alert.id)
            s.commit()
        return {"alert_ids": ids, "adjust_alert_ids": adjust}

    # architecture 6.6: a proposal that changes the budget waits for the user. each resume carries one
    # [alert_id, decision] pair (not a dict: langgraph reads a dict resume as interrupt id -> value);
    # decisions can arrive in any order, and the run finishes once all are in
    def await_approval(state: BudgetState) -> dict:
        decisions = dict(state.get("decisions") or {})
        while set(state["adjust_alert_ids"]) - set(decisions):
            alert_id, decision = interrupt({"waiting_for": sorted(set(state["adjust_alert_ids"]) - set(decisions))})
            decisions[int(alert_id)] = decision
        return {"decisions": decisions}

    def persist_state(state: BudgetState) -> dict:
        status = state["status"] if state.get("status") in ("no_budget", "period_ended") else "ok"
        flags = state.get("flags", [])
        summary = {
            "period_id": state.get("period_id"),
            "flags": [{k: f[k] for k in ("category", "reason", "pace", "spent_cents", "limit_cents")} for f in flags],
            "causes": {c: (d or {}).get("cause") for c, d in state.get("diagnoses", {}).items()},
            "actions": {c: a["action"] for c, a in state.get("actions", {}).items()},
            "alert_ids": state.get("alert_ids", []),
            "unbudgeted": [r["category"] for r in state.get("results", []) if r["kind"] == "unbudgeted"],
            "uncategorized": state.get("uncategorized", 0),
            "sync": state.get("sync"),
            "llm_failures": state.get("llm_failures", 0),
            "decisions": {str(k): v for k, v in (state.get("decisions") or {}).items()},
        }
        with session_factory() as s:
            run = s.get(AgentRun, state["run_id"])
            run.status, run.finished_at, run.summary_json = status, utcnow(), summary
            s.commit()
        return {"status": status}

    def after_load(state: BudgetState) -> str:
        return "persist_state" if state.get("status") in ("no_budget", "period_ended") else "sync"

    def after_detect(state: BudgetState) -> str:
        return "diagnose" if state["flags"] else "persist_state"

    # without a checkpointer there is nowhere to pause (the cli before tpc-20, tests), so nothing waits
    def after_advice(state: BudgetState) -> str:
        return "await_approval" if checkpointer is not None and state.get("adjust_alert_ids") else "persist_state"

    graph = StateGraph(BudgetState)
    for name, fn in (("load_state", load_state), ("sync", sync), ("categorize", categorize),
                     ("update_totals", update_totals), ("detect_overspend", detect_overspend),
                     ("diagnose", diagnose), ("decide_action", decide_action), ("log_advice", log_advice),
                     ("await_approval", await_approval), ("persist_state", persist_state)):
        graph.add_node(name, fn)
    graph.add_edge(START, "load_state")
    graph.add_conditional_edges("load_state", after_load, ["sync", "persist_state"])
    graph.add_edge("sync", "categorize")
    graph.add_edge("categorize", "update_totals")
    graph.add_edge("update_totals", "detect_overspend")
    graph.add_conditional_edges("detect_overspend", after_detect, ["diagnose", "persist_state"])
    graph.add_edge("diagnose", "decide_action")
    graph.add_edge("decide_action", "log_advice")
    graph.add_conditional_edges("log_advice", after_advice, ["await_approval", "persist_state"])
    graph.add_edge("await_approval", "persist_state")
    graph.add_edge("persist_state", END)
    return graph.compile(checkpointer=checkpointer)


def _thread(run_id: int) -> dict:
    return {"configurable": {"thread_id": f"budget-{run_id}"}}


def _interrupted(result: dict) -> Optional[list[int]]:
    found = result.get("__interrupt__")
    return sorted(i for it in found for i in it.value["waiting_for"]) if found else None


def _mark(session_factory, run_id: int, status: str, summary: Optional[dict] = None, finished: bool = True) -> None:
    with session_factory() as s:
        row = s.get(AgentRun, run_id)
        row.status = status
        if finished:
            row.finished_at = utcnow()
        if summary is not None:
            row.summary_json = summary
        s.commit()


# the alert ids a paused run is waiting on; [] when it isn't paused or its checkpoint is gone
def waiting_for(session_factory, checkpointer: BaseCheckpointSaver, run_id: int) -> list[int]:
    graph = build_budget_graph(session_factory, model=None, gateway=None, checkpointer=checkpointer)
    snapshot = graph.get_state(_thread(run_id))
    return sorted(i for task in snapshot.tasks for it in task.interrupts for i in it.value["waiting_for"])


# one run for one user and date. the agent_runs row is written before the graph starts, so a crash in any
# node still leaves a run marked error (then re-raised)
def run_budget(session_factory, user_id: str, today: date, *, model: Optional[BaseChatModel] = None,
               sync_fn: Optional[SyncFn] = None, audit_path=None,
               checkpointer: Optional[BaseCheckpointSaver] = None,
               on_start: Optional[Callable[[int], None]] = None) -> dict:
    model = model or get_chat_model()
    with session_factory() as s:
        run = AgentRun(user_id=user_id, graph="budget", status="running")
        s.add(run)
        s.commit()
        run_id = run.id
    if on_start is not None:
        on_start(run_id)        # the api answers with the id while the run goes on in the background
    with session_factory() as gs:
        gateway = PrivacyGateway(gs, user_id, audit_path=audit_path)    # one pseudonym per run
        graph = build_budget_graph(session_factory, model=model, gateway=gateway, sync_fn=sync_fn,
                                   checkpointer=checkpointer)
        try:
            result = graph.invoke({"user_id": user_id, "today": today.isoformat(), "run_id": run_id,
                                   "status": "running", "llm_failures": 0},
                                  _thread(run_id) if checkpointer is not None else None)
        except Exception as exc:
            _mark(session_factory, run_id, "error", {"error": type(exc).__name__})
            raise
    waiting = _interrupted(result)
    if waiting is not None:
        _mark(session_factory, run_id, "awaiting_user", finished=False)
        return {**result, "status": "awaiting_user", "waiting_for": waiting}
    return {**result, "waiting_for": []}


# continue a paused run with one decision. it pauses again while other proposals are still undecided
def resume_budget_run(session_factory, run_id: int, alert_id: int, decision: str, *,
                      checkpointer: BaseCheckpointSaver, model: Optional[BaseChatModel] = None) -> dict:
    graph = build_budget_graph(session_factory, model=model, gateway=None, checkpointer=checkpointer)
    try:
        result = graph.invoke(Command(resume=[alert_id, decision]), _thread(run_id))
    except Exception as exc:
        _mark(session_factory, run_id, "error", {"error": type(exc).__name__})
        raise
    waiting = _interrupted(result)
    if waiting is not None:
        return {**result, "status": "awaiting_user", "waiting_for": waiting}
    return {**result, "waiting_for": []}


def _money(cents: Optional[int]) -> str:
    return "-" if cents is None else f"${cents / 100:,.2f}"


# a failed sync means the budget was checked on stale data; say so instead of a bare "ok"
def _sync_line(sync: Optional[dict]) -> str:
    if not sync or sync.get("skipped"):
        return "skipped"
    line = f"{sync['items']} item(s)"
    return f"{line}, errors: {', '.join(sync['errors'])} (data may be stale)" if sync["errors"] else line


def _print_report(state: dict) -> None:
    status = state["status"]
    if status == "awaiting_user":
        status += f" (waiting for alert(s) {', '.join(str(i) for i in state['waiting_for'])})"
    print(f"run {state['run_id']}  status: {status}  period: {state.get('period_id')}")
    for r in state.get("results", []):
        pace = "-" if r["pace"] is None else f"{r['pace']:.2f}"
        mark = f"  FLAG ({r['reason']})" if r["flagged"] else ""
        print(f"  {r['category']:<22} {r['kind']:<10} {_money(r['spent_cents']):>11} of "
              f"{_money(r['limit_cents']):>11}  pace {pace}{mark}")
    for flag in state.get("flags", []):
        cat = flag["category"]
        d, a = state["diagnoses"][cat], state["actions"][cat]
        print(f"\n{cat} ({flag['reason']})")
        print(f"  cause:  {d['cause']} - {d['evidence']}" if d else "  cause:  unknown (llm failed)")
        note = " (fallback)" if a["fallback"] else " (downgraded: only recurring adjusts)" if a.get("downgraded") else ""
        print(f"  action: {a['action']}{note}")
        print(f"  message: {a['message']}")
        if a["proposed_limit_cents"] is not None:
            print(f"  proposed limit: {_money(a['proposed_limit_cents'])}")
    unbudgeted = [r["category"] for r in state.get("results", []) if r["kind"] == "unbudgeted"]
    print(f"\nsync: {_sync_line(state.get('sync'))}")
    print(f"alerts written: {len(state.get('alert_ids', []))}")
    print(f"unbudgeted: {', '.join(unbudgeted) or 'none'}")
    print(f"llm failures: {state.get('llm_failures', 0)}")


# python -m app.agents.budget_graph --user <id> [--date YYYY-MM-DD] [--no-sync] [--model NAME], from backend/
def main(argv=None, session_factory=None, model: Optional[BaseChatModel] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the budget agent once for one user.")
    parser.add_argument("--user", required=True)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD, default today")
    parser.add_argument("--no-sync", action="store_true", help="skip the plaid sync (synthetic users)")
    parser.add_argument("--model", help="ollama model, default OLLAMA_MODEL")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    config.BUDGET_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(config.BUDGET_CHECKPOINT_PATH), check_same_thread=False))
    try:
        state = run_budget(session_factory, args.user, args.date, model=model or get_chat_model(model=args.model),
                           sync_fn=None if args.no_sync else plaid_sync, checkpointer=saver)
    except Exception as exc:
        print(f"run failed: {type(exc).__name__}: {exc}")
        return 1
    _print_report(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
