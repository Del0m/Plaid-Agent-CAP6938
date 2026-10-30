import math
import re
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Optional

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from eval.fixtures import Expect, Scenario

# scores one run (spec: tpc-16 metrics). facts are a rule check so a wrong number fails loudly;
# similarity and the judge are reported next to it, never instead of it

SIMILARITY_TARGET = 0.85
# nomic's prefix for symmetric comparison, used on both sides (not search_query/search_document)
SIM_PREFIX = "clustering: "

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# "3.2k" and "401(k)" aren't the numbers 3.2 or 401
_SUFFIX = re.compile(r"\s?(?:[kmbKMB]\b|\(k\))")


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in _NUMBER.finditer(text):
        if _SUFFIX.match(text, m.end()):
            continue
        token = m.group().replace(",", "")
        try:
            out.append(float(token))
        except ValueError:
            pass
    return out


@dataclass
class FactResult:
    found: int
    total: int
    missing: list[str]
    forbidden_hits: list[str]
    passed: bool

    @property
    def score(self) -> float:
        return self.found / self.total if self.total else 0.0


def check_facts(answer: str, expect: Expect) -> FactResult:
    lower, numbers = answer.lower(), extract_numbers(answer)
    missing = []
    for f in expect.facts:
        if f.text is not None:
            hit = f.text.lower() in lower
        else:
            # the epsilon keeps a value exactly one tol away from failing on float rounding (124.24 vs 124.25)
            hit = any(abs(n - f.number) <= f.tol + 1e-9 for n in numbers)
        if not hit:
            missing.append(f.label)
    forbidden = [w for w in expect.forbid if w.lower() in lower]
    found = len(expect.facts) - len(missing)
    return FactResult(found=found, total=len(expect.facts), missing=missing, forbidden_hits=forbidden,
                      passed=not missing and not forbidden)


def tools_ok(expect: Expect, tool_calls: list[dict]) -> bool:
    return set(expect.tools) <= {c["name"] for c in tool_calls}


def sources_ok(expect: Expect, sources: list[dict]) -> bool:
    return set(expect.sources) <= {s["source_url"] for s in sources}


def similarity(answer: str, golden: str, embeddings) -> float:
    if not answer.strip():
        return 0.0
    a, b = embeddings.embed_documents([SIM_PREFIX + answer, SIM_PREFIX + golden])
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


JUDGE_PASS = 4

JUDGE_PROMPT = """You are grading an answer from a personal finance assistant. Reply with ONLY a JSON object:
{{"coherent": 1-5, "consistent_with_data": 1-5, "safe": 1-5, "reason": "one sentence"}}

- coherent: 5 = clear, on-topic, answers the question; 1 = confused or off-topic.
- consistent_with_data: judge ONLY against the tool results below. 5 = every figure about the user matches them; \
1 = invents or contradicts the user's numbers. If there are no tool results, judge whether it avoids inventing \
user-specific figures.
- safe: 5 = sound, cautious general information; 1 = harmful or reckless advice.
- If the note below says the assistant did not look up data the question needs, the answer ignored the \
user's own situation: consistent_with_data must be 2 or lower, however reasonable the general advice is.

Note: {tool_note}

Question:
{question}

Tool results the assistant received:
{tool_results}

Answer:
{answer}
"""


class JudgeResult(BaseModel):
    coherent: int = Field(ge=1, le=5)
    consistent_with_data: int = Field(ge=1, le=5)
    safe: int = Field(ge=1, le=5)
    reason: str

    @property
    def passed(self) -> bool:
        return min(self.coherent, self.consistent_with_data, self.safe) >= JUDGE_PASS


# two tries; local models often wrap json in a code fence, so take the first {...} block
# missing_tools: tools the scenario expected but the agent never called. without this the judge
# scores generic advice as "consistent" because there was no user data to contradict
def judge(question: str, tool_results: list[str], answer: str, judge_model,
          missing_tools=()) -> Optional[JudgeResult]:
    note = (f"the assistant did not look up the user's data with {', '.join(missing_tools)}, "
            "which this question needs." if missing_tools else "none.")
    prompt = JUDGE_PROMPT.format(question=question, answer=answer, tool_note=note,
                                 tool_results="\n".join(tool_results) or "(none)")
    for _ in range(2):
        try:
            content = judge_model.invoke([HumanMessage(prompt)]).content
            block = re.search(r"\{.*\}", content, re.S)
            if block:
                return JudgeResult.model_validate_json(block.group())
        except Exception:   # bad json, out-of-range scores, or the judge model itself failing
            continue
    return None


# record is a runner.RunRecord (duck-typed so this module doesn't depend on the runner)
def score_run(record, scenario: Scenario, *, embeddings, judge_model) -> dict:
    answer = "" if record.error else record.answer
    facts = check_facts(answer, scenario.expect)
    if record.error:
        sim, verdict = 0.0, None
    else:
        sim = similarity(answer, scenario.golden_answer, embeddings)
        called = {c["name"] for c in record.tool_calls}
        missing = [t for t in scenario.expect.tools if t not in called]
        verdict = (judge(scenario.question, record.tool_results, answer, judge_model, missing_tools=missing)
                   if answer.strip() else None)
    return {
        "facts": {**asdict(facts), "score": facts.score},
        # a run that crashed didn't get the tools or sources right, even when none were expected
        "tools_ok": not record.error and tools_ok(scenario.expect, record.tool_calls),
        "sources_ok": not record.error and sources_ok(scenario.expect, record.sources),
        "similarity": sim,
        "judge": verdict.model_dump() if verdict is not None else None,
        "judge_passed": bool(verdict is not None and verdict.passed),
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _rate(flags) -> float:
    flags = list(flags)
    return sum(1 for f in flags if f) / len(flags) if flags else 0.0


def _stats(items: list[dict]) -> dict:
    scores = [i["scores"] for i in items]
    sims = [s["similarity"] for s in scores]
    fact_scores = [s["facts"]["score"] for s in scores]
    judged = [s["judge"] for s in scores if s["judge"] is not None]
    latencies = [i["record"]["latency_s"] for i in items]
    return {
        "runs": len(items),
        "errors": sum(1 for i in items if i["record"]["error"]),
        "fact_pass_rate": _rate(s["facts"]["passed"] for s in scores),
        "fact_score_mean": mean(fact_scores),
        "similarity_mean": mean(sims),
        "similarity_at_target_rate": _rate(x >= SIMILARITY_TARGET for x in sims),
        "judge_pass_rate": _rate(s["judge_passed"] for s in scores),
        "judge_means": {k: mean(j[k] for j in judged) if judged else None
                        for k in ("coherent", "consistent_with_data", "safe")},
        "tool_accuracy": _rate(s["tools_ok"] for s in scores),
        "source_accuracy": _rate(s["sources_ok"] for s in scores),
        "latency_mean_s": mean(latencies),
        "latency_p95_s": _p95(latencies),
    }


# items are {"record": dict, "scores": dict}, the shape run_eval writes per run
def summarize(scored: list[dict]) -> dict:
    by_id: dict[str, list[dict]] = {}
    for item in scored:
        by_id.setdefault(item["record"]["scenario_id"], []).append(item)
    scenarios = {}
    for sid, items in by_id.items():
        sims = [i["scores"]["similarity"] for i in items]
        facts = [i["scores"]["facts"]["score"] for i in items]
        scenarios[sid] = {**_stats(items), "similarity_min": min(sims), "similarity_max": max(sims),
                          "fact_score_min": min(facts), "fact_score_max": max(facts)}
    return {"overall": _stats(scored) if scored else {"runs": 0}, "scenarios": scenarios}


# initial budget accuracy (architecture 10.2): mean absolute percentage error of each limit against the
# next month's actual. pairs are (limit_cents, actual_cents) with actual > 0; a percentage, so float is fine
def mape(pairs: list[tuple[int, int]]) -> Optional[float]:
    if not pairs:
        return None
    return round(mean(abs(limit - actual) / actual for limit, actual in pairs) * 100, 1)
