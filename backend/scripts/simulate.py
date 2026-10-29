"""Advance a simulated user day by day, running the real budget agent each day.

Each day injects that day's generated transactions, records the month-end slot on the 1st, and runs
the budget check. Seed a simulated user first:
    ../venv/bin/python scripts/seed_budget_user.py --profile overspend-one-time --replace --start default

Run from backend/ (needs Ollama for flagged days):
    ../venv/bin/python scripts/simulate.py --user budget-overspend-one-time --days 30
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
# make `app`, `tests` and `eval` importable however the script is launched
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))

from cryptography.fernet import Fernet

# app.security fails at import without a key; simulating never decrypts anything
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

from langgraph.checkpoint.sqlite import SqliteSaver

from app import config
from app.agents.llm import get_chat_model
from app.budget_service.daily import user_lock
from app.budget_service.simulate import NotSimulated, advance_days


def _print_day(day: dict) -> None:
    flags = ", ".join(day["flags"]) or "-"
    print(f"{day['date']}  +{day['injected']} txns  {day['status']}  flags: {flags}  alerts: {day['alerts']}")


def main(argv=None, session_factory=None, model=None) -> int:
    parser = argparse.ArgumentParser(description="Advance a simulated user's date, running the budget agent daily.")
    parser.add_argument("--user", required=True)
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--model", help="ollama model, default OLLAMA_MODEL")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    lock = user_lock(args.user)          # in-process only: a running API server has its own locks
    if not lock.acquire(blocking=False):
        print("A budget run is already in progress.")
        return 1
    config.BUDGET_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(config.BUDGET_CHECKPOINT_PATH), check_same_thread=False))
    try:
        result = advance_days(session_factory, args.user, args.days,
                              model=model or get_chat_model(model=args.model), checkpointer=saver, on_day=_print_day)
    except NotSimulated:
        print("not a simulated user; seed it with --start")
        return 1
    except Exception as exc:
        print(f"simulation stopped: {type(exc).__name__}: {exc}")
        return 1
    finally:
        lock.release()
    print(f"simulated {result['days_done']} day(s): {result['start_date']} → {result['date']}, "
          f"{len(result['run_ids'])} runs, {result['alerts_written']} alerts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
