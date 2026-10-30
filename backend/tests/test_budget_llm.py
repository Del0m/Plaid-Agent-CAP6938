import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.budget_llm import Action, Diagnosis, decide_messages, diagnose_messages, invoke_structured
from tests.fakes import ScriptedChatModel

GOOD = json.dumps({"cause": "one_time", "evidence": "One $600 purchase at ElectroMart."})


def model(*replies):
    return ScriptedChatModel(replies=[r if isinstance(r, Exception) else AIMessage(content=r) for r in replies])


def test_valid_reply_parsed():
    m = model(GOOD)
    got = invoke_structured(m, Diagnosis, diagnose_messages("{}"))
    assert got == Diagnosis(cause="one_time", evidence="One $600 purchase at ElectroMart.")
    assert m.kwargs_seen[0]["format"] == Diagnosis.model_json_schema()


def test_invalid_then_valid_retries():
    m = model("not json", GOOD)
    assert invoke_structured(m, Diagnosis, diagnose_messages("{}")).cause == "one_time"
    assert len(m.seen) == 2


def test_two_invalid_gives_none():
    m = model("{}", "{}")
    assert invoke_structured(m, Diagnosis, diagnose_messages("{}")) is None
    assert len(m.seen) == 2


def test_fenced_json_parses():
    m = model(f"```json\n{GOOD}\n```")
    assert invoke_structured(m, Diagnosis, diagnose_messages("{}")).cause == "one_time"
    assert len(m.seen) == 1


def test_empty_text_is_invalid():
    m = model(json.dumps({"cause": "one_time", "evidence": ""}), GOOD)
    assert invoke_structured(m, Diagnosis, diagnose_messages("{}")).evidence.startswith("One $600")
    assert len(m.seen) == 2


def test_whitespace_text_is_invalid():
    m = model(json.dumps({"cause": "one_time", "evidence": "   "}), GOOD)
    assert invoke_structured(m, Diagnosis, diagnose_messages("{}")).evidence.startswith("One $600")
    assert len(m.seen) == 2


# review on #12: an unreachable model gives None at once (no retry), so the graph writes its fallback alert
def test_unreachable_model_returns_none():
    m = model(ConnectionError("down"), json.dumps({"cause": "one_time", "evidence": "x"}))
    assert invoke_structured(m, Diagnosis, diagnose_messages("{}")) is None
    assert len(m.seen) == 1


# anything other than the model being unreachable is a bug, and still fails the run
def test_other_model_error_propagates():
    with pytest.raises(KeyError):
        invoke_structured(model(KeyError("bug")), Diagnosis, diagnose_messages("{}"))


def test_action_schema():
    m = model(json.dumps({"action": "adjust_next_budget", "message": "Raise it to $160."}))
    assert invoke_structured(m, Action, decide_messages("{}", None, 15500, 16000)).action == "adjust_next_budget"


def test_decide_messages_carry_numbers():
    diag = Diagnosis(cause="recurring", evidence="A new $90 gym.")
    text = decide_messages('{"category":"PERSONAL_CARE"}', diag, 15500, 16000)[-1].content
    assert "$155.00" in text and "$160.00" in text and "recurring" in text and "PERSONAL_CARE" in text
    assert "unknown" in decide_messages("{}", None, 15500, 16000)[-1].content


def test_no_proposal_unless_recurring():
    text = decide_messages("{}", Diagnosis(cause="seasonal", evidence="Holiday gifts."), 155000, 160000)[-1].content
    assert "$1,600.00" not in text and "only for a recurring cause" in text
    assert "only for a recurring cause" in decide_messages("{}", None, 15500, 16000)[-1].content
