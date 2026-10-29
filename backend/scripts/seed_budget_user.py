"""Seed a synthetic budget user into dev.db, for running the budget agent on it.

The profiles live in eval/budget_users.py: the five accuracy-eval users and the three planted
overspends (one per cause). The user is `budget-<profile>`; the script prints the command that
runs the budget agent on it, on the profile's run date.

Run from backend/:
    ../venv/bin/python scripts/seed_budget_user.py --profile overspend-one-time [--replace] [--start default]
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
# make `app`, `tests` and `eval` importable however the script is launched
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))

from cryptography.fernet import Fernet

# app.security fails at import without a key; seeding never decrypts anything
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (Account, AdviceLog, AgentRun, Alert, BudgetLine, BudgetPeriod, Liability, PlaidItem,
                           Transaction, User)
from eval.budget_users import PLANTED, PROFILES, default_start, seed_profile


# everything the user owns, children before parents so foreign keys hold
def delete_user(session: Session, user_id: str) -> None:
    periods = select(BudgetPeriod.id).where(BudgetPeriod.user_id == user_id)
    session.execute(delete(AdviceLog).where(AdviceLog.user_id == user_id))
    session.execute(delete(Alert).where(Alert.user_id == user_id))
    session.execute(delete(AgentRun).where(AgentRun.user_id == user_id))
    session.execute(delete(BudgetLine).where(BudgetLine.period_id.in_(periods)))
    for model in (BudgetPeriod, Transaction, Liability, Account, PlaidItem):
        session.execute(delete(model).where(model.user_id == user_id))
    session.execute(delete(User).where(User.id == user_id))
    session.commit()


def main(argv=None, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description="Seed a synthetic budget user into dev.db.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--replace", action="store_true", help="delete the user and everything it owns first")
    parser.add_argument("--start", help="YYYY-MM-DD or 'default': seed history only up to this day and simulate from it")
    args = parser.parse_args(argv)
    profiles = {p.id: p for p in PROFILES + PLANTED}
    profile = profiles.get(args.profile)
    if profile is None:
        print(f"unknown profile {args.profile!r}; choose one of: {', '.join(profiles)}")
        return 2
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    user_id = f"budget-{profile.id}"
    with session_factory() as s:
        if s.get(User, user_id) is not None:
            if not args.replace:
                print(f"{user_id} already exists; pass --replace to delete it and seed it again")
                return 1
            delete_user(s, user_id)
        start = None
        if args.start:
            start = default_start(profile) if args.start == "default" else date.fromisoformat(args.start)
        seed_profile(s, profile, until=start)
    print(f"seeded {user_id}: {profile.description}")
    if start is not None:
        print(f"simulated from {start}; advance it: python scripts/simulate.py --user {user_id} --days 30")
    else:
        run_date = profile.run_date or profile.today
        print(f"run it: python -m app.agents.budget_graph --user {user_id} --date {run_date} --no-sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
