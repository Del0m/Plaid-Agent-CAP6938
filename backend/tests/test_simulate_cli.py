from datetime import date

import pytest

from app import config
from app.budget_service.daily import user_lock
from eval.budget_users import PLANTED, seed_profile
from scripts.simulate import main
from tests.fakes import SchemaReplyModel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "BUDGET_CHECKPOINT_PATH", tmp_path / "ck.db")


def seed(db, until=date(2026, 10, 9)):
    with db() as s:
        return seed_profile(s, next(p for p in PLANTED if p.id == "overspend-one-time"), until=until)


def test_cli_prints_days_and_summary(db, capsys):
    uid = seed(db)
    assert main(["--user", uid, "--days", "3"], session_factory=db, model=SchemaReplyModel()) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    day_lines = [ln for ln in lines if ln.startswith("2026-10-")]
    assert [ln.split()[0] for ln in day_lines] == ["2026-10-10", "2026-10-11", "2026-10-12"]
    assert "+1 txns" in day_lines[2] and "flags: GENERAL_MERCHANDISE" in day_lines[2]
    assert lines[-1].startswith("simulated 3 day(s): 2026-10-09 → 2026-10-12, 3 runs, ")


def test_cli_not_simulated_exit_1(db, capsys):
    uid = seed(db, until=None)
    assert main(["--user", uid, "--days", "1"], session_factory=db, model=SchemaReplyModel()) == 1
    assert "not a simulated user; seed it with --start" in capsys.readouterr().out


def test_cli_locked_exit_1(db, capsys):
    uid = seed(db)
    lock = user_lock(uid)
    lock.acquire()
    try:
        assert main(["--user", uid, "--days", "1"], session_factory=db, model=SchemaReplyModel()) == 1
    finally:
        lock.release()
    assert "A budget run is already in progress." in capsys.readouterr().out
