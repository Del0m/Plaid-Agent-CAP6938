import math
import re
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Optional

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
            hit = any(abs(n - f.number) <= f.tol for n in numbers)
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


def judge(question: str, tool_results: list[str], answer: str, judge_model):
    return None


# record is a runner.RunRecord (duck-typed so this module doesn't depend on the runner)
def score_run(record, scenario: Scenario, *, embeddings, judge_model) -> dict:
    answer = "" if record.error else record.answer
    facts = check_facts(answer, scenario.expect)
    if record.error:
        sim, verdict = 0.0, None
    else:
        sim = similarity(answer, scenario.golden_answer, embeddings)
        verdict = judge(scenario.question, record.tool_results, answer, judge_model) if answer.strip() else None
    return {
        "facts": {**asdict(facts), "score": facts.score},
        "tools_ok": tools_ok(scenario.expect, record.tool_calls),
        "sources_ok": sources_ok(scenario.expect, record.sources),
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
