"""Score the chat agent on the golden scenarios in eval/scenarios/.

Each scenario seeds a synthetic user into a throwaway database, asks the real agent its
question --runs times, and scores every answer on golden facts, semantic similarity and a
coherence judge. Results go to eval/results/<date>/<model>/, one file per run as it finishes.

Run from the repo root:
    venv/bin/python eval/run_eval.py [--model qwen2.5:14b] [--runs 3] [--only <id>] [--judge gemma3:12b]
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# make `eval` and `app` importable however the script is launched (same trick as backend/scripts/)
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))

from cryptography.fernet import Fernet

# app.security fails at import without a key; the eval never decrypts anything
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

import httpx

from app import config
from app.agents.llm import get_chat_model, get_embeddings
from app.rag.store import KnowledgeBase
from eval.fixtures import ScenarioError, load_scenarios
from eval.metrics import score_run, summarize
from eval.runner import run_once

JUDGE_MODEL = "gemma3:12b"
SCENARIOS_DIR = REPO / "eval" / "scenarios"
RESULTS_DIR = REPO / "eval" / "results"


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default=config.OLLAMA_MODEL, help="agent model (default: OLLAMA_MODEL)")
    p.add_argument("--judge", default=JUDGE_MODEL, help=f"judge model (default: {JUDGE_MODEL})")
    p.add_argument("--runs", type=int, default=3, help="runs per scenario (default: 3)")
    p.add_argument("--only", help="run just this scenario id")
    args = p.parse_args(argv)
    if args.runs < 1:
        p.error("--runs must be at least 1")
    return args


# None = ollama unreachable; a bare name also matches "<name>:latest"
def missing_models(models: list[str]):
    try:
        resp = httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=5)
        names = {m["name"] for m in resp.json()["models"]}
    except Exception:
        return None
    return [m for m in models if m not in names and f"{m}:latest" not in names]


def preflight(agent: str, judge: str, kb) -> list[str]:
    needed = list(dict.fromkeys([agent, config.OLLAMA_EMBED_MODEL, judge]))
    missing = missing_models(needed)
    if missing is None:
        return [f"Ollama isn't reachable at {config.OLLAMA_BASE_URL}. Start it with: ollama serve"]
    problems = [f"Model {m} isn't pulled. Run: ollama pull {m}" for m in missing]
    if kb.count() == 0:
        problems.append("The knowledge base is empty. Run: venv/bin/python backend/scripts/ingest_kb.py")
    return problems


# a second run on the same day gets -2, -3... so earlier results are never overwritten
def results_dir(root: Path, day: date, model: str) -> Path:
    base = root / day.isoformat() / model.replace(":", "_").replace("/", "_")
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.name}-{n}")
    path.mkdir(parents=True)
    return path


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _pct(x) -> str:
    return f"{x * 100:5.0f}%"


def _print_table(summary: dict) -> None:
    print(f"\n{'scenario':<28}{'facts':>7}{'sim':>7}{'judge':>7}{'tools':>7}{'errors':>8}")
    rows = list(summary["scenarios"].items()) + [("OVERALL", summary["overall"])]
    for sid, s in rows:
        print(f"{sid:<28}{_pct(s['fact_pass_rate']):>7}{s['similarity_mean']:7.2f}"
              f"{_pct(s['judge_pass_rate']):>7}{_pct(s['tool_accuracy']):>7}{s['errors']:>8}")


def main(argv=None) -> int:
    args = parse_args(argv)
    kb = KnowledgeBase()
    problems = preflight(args.model, args.judge, kb)
    if problems:
        print("Can't run the eval yet:\n" + "\n".join(f"- {p}" for p in problems))
        return 2
    try:
        scenarios = load_scenarios(SCENARIOS_DIR, only=args.only)
    except ScenarioError as exc:
        print(f"Bad scenario: {exc}")
        return 2

    out = results_dir(RESULTS_DIR, date.today(), args.model)
    cfg = {"model": args.model, "judge": args.judge, "embeddings": config.OLLAMA_EMBED_MODEL,
           "runs": args.runs, "commit": _git_commit(), "scenarios": [s.id for s in scenarios],
           "started_at": _now()}
    _write(out / "config.json", cfg)

    model = get_chat_model(temperature=0, model=args.model)
    judge_model = get_chat_model(temperature=0, model=args.judge)
    embeddings = get_embeddings()
    scored = []
    for scenario in scenarios:
        for run in range(1, args.runs + 1):
            print(f"{scenario.id} run {run}/{args.runs}...", flush=True)
            rec = run_once(scenario, model=model, kb=kb, audit_path=out / "audit.jsonl", run=run)
            item = {"record": rec.to_dict(),
                    "scores": score_run(rec, scenario, embeddings=embeddings, judge_model=judge_model)}
            # written now, so a crash or ctrl-c keeps every finished run
            _write(out / f"{scenario.id}.run{run}.json", item)
            scored.append(item)

    summary = summarize(scored)
    _write(out / "summary.json", summary)
    _write(out / "config.json", {**cfg, "finished_at": _now()})
    _print_table(summary)
    print(f"\nResults: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
