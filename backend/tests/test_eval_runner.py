import json
import time
from datetime import date
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app import config
from eval.fixtures import load_scenario
import eval.runner as runner
from eval.runner import RunRecord, run_once
from tests.fakes import FakeKB, ScriptedChatModel, ollama_has, tool_call
from tests.test_eval_fixtures import write

REPO = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 27)


def model(*replies):
    return ScriptedChatModel(replies=list(replies))


def test_run_once_captures_everything(tmp_path):
    scenario = load_scenario(write(tmp_path))
    audit = tmp_path / "audit.jsonl"
    rec = run_once(scenario, model=model(tool_call("get_debts"), AIMessage("Pay the Platinum Card first.")),
                   kb=FakeKB(), audit_path=audit, run=2, today=TODAY)

    assert isinstance(rec, RunRecord)
    assert (rec.scenario_id, rec.run, rec.error) == ("debt-highest-apr", 2, None)
    assert rec.answer == "Pay the Platinum Card first."
    assert rec.tool_calls == [{"name": "get_debts", "args": {}}]
    assert "Platinum Card" in rec.tool_results[0] and "24.99" in rec.tool_results[0]
    assert rec.sources == [FakeKB.RESULT]
    assert rec.latency_s > 0
    [line] = audit.read_text().splitlines()
    assert json.loads(line)["purpose"] == "debts"


def test_run_once_model_error_recorded(tmp_path):
    scenario = load_scenario(write(tmp_path))
    rec = run_once(scenario, model=model(ConnectionError("down")), kb=FakeKB(),
                   audit_path=tmp_path / "a.jsonl", today=TODAY)

    assert rec.error == "ConnectionError: down"
    assert rec.answer == "" and rec.tool_calls == []



# review on #10: latency is the agent's answer time; database setup and seeding aren't part of it
def test_latency_excludes_setup(tmp_path, monkeypatch):
    real_seed = runner.seed

    def slow_seed(*args):
        time.sleep(0.3)
        return real_seed(*args)

    monkeypatch.setattr(runner, "seed", slow_seed)
    rec = run_once(load_scenario(write(tmp_path)), model=model(AIMessage("Pay the card.")), kb=FakeKB(),
                   audit_path=tmp_path / "a.jsonl", today=TODAY)

    assert 0 < rec.latency_s < 0.25

def test_runs_are_isolated(tmp_path):
    first = load_scenario(write(tmp_path))

    def rename(d):
        d["id"] = "other"
        d["user"]["accounts"][2]["name"] = "Gold Card"
        d["user"]["transactions"] = []

    second = load_scenario(write(tmp_path, rename))
    for s in (first, second):
        rec = run_once(s, model=model(tool_call("get_accounts"), AIMessage("ok")), kb=FakeKB(),
                       audit_path=tmp_path / "a.jsonl", today=TODAY)

    assert "Gold Card" in rec.tool_results[0]
    assert "Platinum Card" not in rec.tool_results[0]


def _kb_ready() -> bool:
    try:
        from app.rag.store import KnowledgeBase
        return KnowledgeBase().count() > 0
    except Exception:
        return False


LIVE_FILE = REPO / "eval" / "scenarios" / "debt-highest-apr.json"


@pytest.mark.skipif(not ollama_has(config.OLLAMA_MODEL, config.OLLAMA_EMBED_MODEL, "gemma3:12b"),
                    reason="needs ollama with OLLAMA_MODEL, OLLAMA_EMBED_MODEL and gemma3:12b pulled")
@pytest.mark.skipif(not _kb_ready(), reason="needs an ingested knowledge base (scripts/ingest_kb.py)")
@pytest.mark.skipif(not LIVE_FILE.exists(), reason="eval/scenarios/debt-highest-apr.json not written yet")
def test_live_debt_highest_apr(tmp_path):
    from app.agents.llm import get_chat_model, get_embeddings
    from app.rag.store import KnowledgeBase
    from eval.metrics import score_run

    scenario = load_scenario(LIVE_FILE)
    rec = run_once(scenario, model=get_chat_model(temperature=0), kb=KnowledgeBase(),
                   audit_path=tmp_path / "audit.jsonl")
    scores = score_run(rec, scenario, embeddings=get_embeddings(),
                       judge_model=get_chat_model(temperature=0, model="gemma3:12b"))

    assert rec.error is None
    assert scores["judge"] is not None
