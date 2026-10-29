import argparse
import sys
from dataclasses import asdict
from datetime import date, timedelta
from typing import Callable, Optional, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.budget_llm import Action, Diagnosis, decide_messages, diagnose_messages, invoke_structured
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


def _label(category: str) -> str:
    return category.replace("_", " ").capitalize()


# the message when the model can't decide: the plain numbers, written by code
def _fallback_message(flag: dict) -> str:
    spent, limit = flag["spent_cents"] / 100, flag["limit_cents"] / 100
    if flag["reason"] == "over_limit":
        return f"{_label(flag['category'])} is over its ${limit:,.2f} limit: ${spent:,.2f} so far."
    return (f"{_label(flag['category'])} is at {flag['pace']:.0%} of its pace: ${spent:,.2f} of ${limit:,.2f} "
            f"by day {flag['days_elapsed']}.")


def build_budget_graph(session_factory, *, model: BaseChatModel, gateway: PrivacyGateway,
                       sync_fn: Optional[SyncFn] = None):
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
        for flag in state["flags"]:
            cat = flag["category"]
            f = overspend_facts(PaceResult(**flag), fits.get(cat), txns, history, period.start_date)
            facts[cat] = {**asdict(f), "proposed_limit_cents": proposed_limit_cents(f)}
            contexts[cat] = gateway.build_context("diagnose", facts=f, transactions=txns)
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
                f["proposed_limit_cents"]))
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
        ids = []
        with session_factory() as s:
            for flag in state["flags"]:
                cat, act, d = flag["category"], state["actions"][flag["category"]], state["diagnoses"][flag["category"]]
                if act["action"] == "no_op":
                    continue
                alert = Alert(user_id=state["user_id"], period_id=state["period_id"], kind="overspend",
                              category=cat, severity="high" if flag["reason"] == "over_limit" else "medium",
                              cause=d["cause"] if d else None, action=act["action"], message=act["message"],
                              proposed_limit_cents=act["proposed_limit_cents"], status="pending_user")
                s.add(alert)
                s.add(AdviceLog(user_id=state["user_id"], run_id=state["run_id"], category=cat,
                                advice_text=act["message"]))
                s.flush()
                ids.append(alert.id)
            s.commit()
        return {"alert_ids": ids}

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

    graph = StateGraph(BudgetState)
    for name, fn in (("load_state", load_state), ("sync", sync), ("categorize", categorize),
                     ("update_totals", update_totals), ("detect_overspend", detect_overspend),
                     ("diagnose", diagnose), ("decide_action", decide_action), ("log_advice", log_advice),
                     ("persist_state", persist_state)):
        graph.add_node(name, fn)
    graph.add_edge(START, "load_state")
    graph.add_conditional_edges("load_state", after_load, ["sync", "persist_state"])
    graph.add_edge("sync", "categorize")
    graph.add_edge("categorize", "update_totals")
    graph.add_edge("update_totals", "detect_overspend")
    graph.add_conditional_edges("detect_overspend", after_detect, ["diagnose", "persist_state"])
    graph.add_edge("diagnose", "decide_action")
    graph.add_edge("decide_action", "log_advice")
    graph.add_edge("log_advice", "persist_state")
    graph.add_edge("persist_state", END)
    return graph.compile()


# one run for one user and date. the agent_runs row is written before the graph starts, so a crash in any
# node still leaves a run marked error (then re-raised)
def run_budget(session_factory, user_id: str, today: date, *, model: Optional[BaseChatModel] = None,
               sync_fn: Optional[SyncFn] = None, audit_path=None) -> dict:
    model = model or get_chat_model()
    with session_factory() as s:
        run = AgentRun(user_id=user_id, graph="budget", status="running")
        s.add(run)
        s.commit()
        run_id = run.id
    with session_factory() as gs:
        gateway = PrivacyGateway(gs, user_id, audit_path=audit_path)    # one pseudonym per run
        graph = build_budget_graph(session_factory, model=model, gateway=gateway, sync_fn=sync_fn)
        try:
            return graph.invoke({"user_id": user_id, "today": today.isoformat(), "run_id": run_id,
                                 "status": "running", "llm_failures": 0})
        except Exception as exc:
            with session_factory() as s:
                failed = s.get(AgentRun, run_id)
                failed.status, failed.finished_at, failed.summary_json = "error", utcnow(), {"error": type(exc).__name__}
                s.commit()
            raise


def _money(cents: Optional[int]) -> str:
    return "-" if cents is None else f"${cents / 100:,.2f}"


# a failed sync means the budget was checked on stale data; say so instead of a bare "ok"
def _sync_line(sync: Optional[dict]) -> str:
    if not sync or sync.get("skipped"):
        return "skipped"
    line = f"{sync['items']} item(s)"
    return f"{line}, errors: {', '.join(sync['errors'])} (data may be stale)" if sync["errors"] else line


def _print_report(state: dict) -> None:
    print(f"run {state['run_id']}  status: {state['status']}  period: {state.get('period_id')}")
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
    try:
        state = run_budget(session_factory, args.user, args.date, model=model or get_chat_model(model=args.model),
                           sync_fn=None if args.no_sync else plaid_sync)
    except Exception as exc:
        print(f"run failed: {type(exc).__name__}: {exc}")
        return 1
    _print_report(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
