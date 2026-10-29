import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.budget_graph import main
from eval.budget_users import PLANTED, seed_profile
from tests.fakes import ScriptedChatModel


def seeded(db):
    with db() as s:
        return seed_profile(s, next(p for p in PLANTED if p.id == "overspend-one-time"))


def replies():
    return [AIMessage(content=json.dumps({"cause": "one_time", "evidence": "One $600 purchase."})),
            AIMessage(content=json.dumps({"action": "alert", "message": "A $600 purchase put you over."}))]


@pytest.fixture(autouse=True)
def audit_in_tmp(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")


def test_cli_prints_report(db, capsys):
    uid = seeded(db)
    code = main(["--user", uid, "--date", "2026-10-20", "--no-sync"], session_factory=db,
                model=ScriptedChatModel(replies=replies()))
    out = capsys.readouterr().out
    assert code == 0
    for text in ("status: ok", "GENERAL_MERCHANDISE", "one_time", "alert", "A $600 purchase put you over.",
                 "alerts written: 1"):
        assert text in out, text


def test_cli_error_exit(db, capsys):
    uid = seeded(db)
    code = main(["--user", uid, "--date", "2026-10-20", "--no-sync"], session_factory=db,
                model=ScriptedChatModel(replies=[ConnectionError("down")]))
    assert code == 1
    assert "ConnectionError" in capsys.readouterr().out


def test_cli_bad_date(db):
    with pytest.raises(SystemExit) as exc:
        main(["--user", "x", "--date", "2026-13-01"], session_factory=db)
    assert exc.value.code == 2
