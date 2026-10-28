import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.fixtures import Expect, Fact, Scenario
from eval.metrics import (SIM_PREFIX, check_facts, extract_numbers, mape, score_run, similarity, sources_ok,
                          summarize, tools_ok)

CHART = "https://www.irs.gov/retirement-plans/roth-comparison-chart"


def expect(facts=(), forbid=(), tools=(), sources=()):
    return Expect(facts=list(facts), forbid=list(forbid), tools=list(tools), sources=list(sources))


def scenario(**kw):
    exp = kw.pop("expect", expect([Fact(text="Platinum Card"), Fact(number=24.99, tol=0.01)]))
    return Scenario(id=kw.get("id", "s1"), question="q?", user={}, golden_answer="gold", expect=exp,
                    path=Path("s1.json"))


def record(answer="", error=None, latency=1.0, sid="s1", tools=(), sources=()):
    return SimpleNamespace(scenario_id=sid, answer=answer, error=error, latency_s=latency,
                           tool_calls=[{"name": t, "args": {}} for t in tools], tool_results=[],
                           sources=[{"source_url": u} for u in sources])


# returns preset vectors in order and records what it was asked to embed
class FakeEmbeddings:
    def __init__(self, vectors):
        self.vectors, self.seen = vectors, []

    def embed_documents(self, texts):
        self.seen.append(texts)
        return self.vectors


class Explodes:
    def __getattr__(self, name):
        raise AssertionError(f"model should not be called ({name})")


def test_extract_numbers_formats():
    nums = extract_numbers("$3,200.00 at 24.99% and 24.99 percent, refund -12.50")
    assert 3200.0 in nums and 24.99 in nums and -12.5 in nums
    k = extract_numbers("about 3.2k a month")
    assert 3200.0 not in k and 3.2 not in k


def test_401k_does_not_match_401_fact():
    assert check_facts("put it in a 401(k) plan", expect([Fact(number=401)])).found == 0


def test_number_fact_tolerance():
    e = expect([Fact(number=3200, tol=1)])
    assert check_facts("$3,200.99", e).passed
    assert not check_facts("$3,201.50", e).passed


def test_text_fact_case_insensitive():
    assert check_facts("Pay the PLATINUM CARD first", expect([Fact(text="platinum card")])).passed


def test_forbidden_fails_complete_answer():
    r = check_facts("Platinum Card at 24.99%, see https://x", expect(
        [Fact(text="Platinum Card"), Fact(number=24.99, tol=0.01)], forbid=["http"]))
    assert (r.found, r.total, r.passed, r.forbidden_hits) == (2, 2, False, ["http"])


def test_missing_lists_labels():
    r = check_facts("nothing useful", expect([Fact(text="Platinum Card"), Fact(number=24.99, tol=0.01)]))
    assert r.missing == ["Platinum Card", "24.99±0.01"]
    assert r.score == 0.0


def test_tools_and_sources_subset():
    calls = [{"name": "get_debts", "args": {}}, {"name": "get_accounts", "args": {}}]
    assert tools_ok(expect(tools=["get_debts"]), calls)
    assert not tools_ok(expect(tools=["get_spending"]), calls)
    assert tools_ok(expect(), [])
    assert sources_ok(expect(sources=[CHART]), [{"source_url": CHART}, {"source_url": "x"}])
    assert not sources_ok(expect(sources=[CHART]), [{"source_url": "x"}])
    assert sources_ok(expect(), [])


def test_similarity_cosine():
    fake = FakeEmbeddings([[1.0, 0.0], [1.0, 1.0]])
    assert similarity("answer", "golden", fake) == pytest.approx(1 / math.sqrt(2))
    assert fake.seen == [[SIM_PREFIX + "answer", SIM_PREFIX + "golden"]]
    assert SIM_PREFIX == "clustering: "


def test_similarity_empty_answer_is_zero():
    assert similarity("  ", "golden", Explodes()) == 0.0


def test_score_errored_run_skips_models():
    s = score_run(record(error="ConnectionError: down"), scenario(), embeddings=Explodes(), judge_model=Explodes())
    assert s["similarity"] == 0.0 and s["judge"] is None and s["judge_passed"] is False
    assert s["facts"]["found"] == 0 and s["facts"]["passed"] is False


def scored(sid, *, error=None, fact_found=1, sim=0.9, judge=True, latency=1.0, tools_ok=True):
    return {"record": {"scenario_id": sid, "error": error, "latency_s": latency},
            "scores": {"facts": {"found": fact_found, "total": 1, "passed": fact_found == 1,
                                 "score": float(fact_found)},
                       "tools_ok": tools_ok, "sources_ok": True, "similarity": sim,
                       "judge": {"coherent": 5, "consistent_with_data": 4, "safe": 5} if judge else None,
                       "judge_passed": judge}}


def test_summarize_counts_errors_and_rates():
    items = [scored("a", error="Boom: x", fact_found=0, sim=0.0, judge=False, latency=3.0, tools_ok=False),
             scored("a", sim=0.9, latency=1.0),
             scored("b", sim=0.5, latency=2.0)]
    out = summarize(items)
    o = out["overall"]
    assert (o["runs"], o["errors"]) == (3, 1)
    assert o["similarity_at_target_rate"] == pytest.approx(1 / 3)
    assert o["fact_pass_rate"] == pytest.approx(2 / 3)
    assert o["judge_pass_rate"] == pytest.approx(2 / 3)
    assert o["judge_means"]["consistent_with_data"] == 4
    assert o["tool_accuracy"] == pytest.approx(2 / 3)
    assert o["latency_p95_s"] == 3.0 and o["latency_mean_s"] == 2.0
    a = out["scenarios"]["a"]
    assert (a["runs"], a["errors"]) == (2, 1)
    assert (a["similarity_min"], a["similarity_max"]) == (0.0, 0.9)
    assert (a["fact_score_min"], a["fact_score_max"]) == (0.0, 1.0)


# --- coherence judge (task 3) ---

from langchain_core.messages import AIMessage

from eval.metrics import JUDGE_PASS, judge
from tests.fakes import ScriptedChatModel

GOOD = '{"coherent": 4, "consistent_with_data": 5, "safe": 4, "reason": "fine"}'


def judge_with(*replies):
    model = ScriptedChatModel(replies=[r if isinstance(r, Exception) else AIMessage(r) for r in replies])
    return judge("Which debt first?", ['{"data": [{"name": "Platinum Card"}]}'], "Pay the Platinum Card.", model), model


def test_judge_parses_json():
    result, model = judge_with(GOOD)
    assert (result.coherent, result.consistent_with_data, result.safe) == (4, 5, 4)
    assert result.passed and JUDGE_PASS == 4
    prompt = model.seen[0][-1].content
    assert "Which debt first?" in prompt and "Platinum Card" in prompt and "Pay the Platinum Card." in prompt


def test_judge_extracts_from_code_fence():
    result, _ = judge_with(f"Here you go:\n```json\n{GOOD}\n```\nHope that helps.")
    assert result is not None and result.reason == "fine"


def test_judge_retries_once():
    result, model = judge_with("not json", GOOD)
    assert result is not None and len(model.seen) == 2


def test_judge_two_failures_returns_none():
    result, model = judge_with("nope", "still nope")
    assert result is None and len(model.seen) == 2


def test_judge_out_of_range_rejected():
    bad = GOOD.replace('"coherent": 4', '"coherent": 7')
    assert judge_with(bad, bad)[0] is None


def test_judge_model_exception_returns_none():
    assert judge_with(TimeoutError(), TimeoutError())[0] is None


def test_judge_pass_threshold():
    result, _ = judge_with('{"coherent": 3, "consistent_with_data": 5, "safe": 5, "reason": "x"}')
    assert result is not None and not result.passed


def test_score_run_uses_judge():
    model = ScriptedChatModel(replies=[AIMessage(GOOD)])
    s = score_run(record(answer="Pay the Platinum Card at 24.99%."), scenario(),
                  embeddings=FakeEmbeddings([[1.0, 0.0], [1.0, 0.0]]), judge_model=model)
    assert s["judge_passed"] is True and s["judge"]["consistent_with_data"] == 5


def test_judge_told_when_expected_tools_missing():
    model = ScriptedChatModel(replies=[AIMessage(GOOD)])
    judge("Card or emergency fund?", [], "Generic advice.", model, missing_tools=["get_debts"])
    prompt = model.seen[0][-1].content
    assert "did not look up" in prompt and "get_debts" in prompt


def test_score_run_passes_missing_tools_to_judge():
    model = ScriptedChatModel(replies=[AIMessage(GOOD)])
    sc = scenario(expect=expect([Fact(text="x")], tools=["get_debts"]))
    score_run(record(answer="Generic advice."), sc, embeddings=FakeEmbeddings([[1.0], [1.0]]), judge_model=model)
    assert "get_debts" in model.seen[0][-1].content


def test_mape_known_case():
    assert mape([(110, 100), (90, 100), (200, 100)]) == 40.0


def test_mape_empty():
    assert mape([]) is None


def test_mape_rounds():
    assert mape([(1, 3)]) == 66.7
