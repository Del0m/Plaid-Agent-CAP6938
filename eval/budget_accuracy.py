"""Score the initial budget against the following month's actual spending, median vs mean.

Each synthetic user in eval/budget_users.py is seeded into a throwaway database. The real
initialize_budget builds and stores its first budget, which is printed so you can check it looks
sensible. Then the budget is fitted with the median and with the mean, and each is scored by MAPE
against the next month. Results go to eval/results/<date>/budget_accuracy/results.json.

Run from the repo root:
    venv/bin/python eval/budget_accuracy.py
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# make `eval` and `app` importable however the script is launched (same trick as run_eval.py)
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))

from cryptography.fernet import Fernet

# app.security fails at import without a key; nothing here decrypts anything
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

from app.budget_engine.fit import fit_budget
from app.budget_engine.txns import add_months, category_totals, month_end, month_start, spending
from app.budget_service.initialize import initialize_budget, load_txns
from eval.budget_users import PROFILES, Profile, seed_profile
from eval.fixtures import temp_db
from eval.metrics import mape
from eval.run_eval import RESULTS_DIR, _git_commit, _write, results_dir

CENTERS = ("median", "mean")


# lines vs next month's actuals. a line whose category had $0 can't be scored by a percentage, so it's
# counted instead; so is spending that had no line at all
def score(lines, actuals: dict[str, int]) -> dict:
    rows = [{"category": ln.category, "kind": ln.kind, "limit_cents": ln.limit_cents,
             "actual_cents": actuals.get(ln.category, 0)} for ln in lines]
    budgeted = {ln.category for ln in lines}
    return {
        "mape": mape([(r["limit_cents"], r["actual_cents"]) for r in rows if r["actual_cents"] > 0]),
        "lines": rows,
        "zero_actual_lines": sorted(r["category"] for r in rows if r["actual_cents"] == 0),
        "unbudgeted": sorted(cat for cat, total in actuals.items() if total > 0 and cat not in budgeted),
    }


def run_profile(profile: Profile) -> dict:
    current = month_start(profile.today)
    with temp_db() as Session, Session() as s:
        uid = seed_profile(s, profile)
        period = initialize_budget(s, uid, profile.today)
        budget = {
            "lines": [{"category": ln.category, "kind": ln.kind, "limit_cents": ln.limit_cents,
                       "outlier_months": [o["month"] for o in ln.fit_json["outliers"]]}
                      for ln in sorted(period.lines, key=lambda ln: ln.category)],
            "skipped": period.fit_json["skipped"],
        }
        history = load_txns(s, uid, add_months(current, -3), current - timedelta(days=1))
        actuals = category_totals(spending(load_txns(s, uid, current, month_end(current))),
                                  current, month_end(current))
    result = {"id": profile.id, "description": profile.description, "budget": budget}
    for center in CENTERS:
        result[center] = score(fit_budget(history, profile.today, center=center).lines, actuals)
    return result


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _pct(x) -> str:
    return "-" if x is None else f"{x:.1f}%"


def _print_budget(r: dict) -> None:
    print(f"\n{r['id']}: {r['description']}")
    for ln in r["budget"]["lines"]:
        outliers = f"  (outlier: {', '.join(ln['outlier_months'])})" if ln["outlier_months"] else ""
        print(f"  {ln['category']:<22} {ln['kind']:<9} {_dollars(ln['limit_cents']):>11}{outliers}")
    if r["budget"]["skipped"]:
        print(f"  skipped: {', '.join(r['budget']['skipped'])}")


def _print_table(results: list[dict], pooled: dict) -> None:
    print(f"\n{'profile':<12} {'median MAPE':>12} {'mean MAPE':>10}  zero-actual (mean)  unbudgeted")
    for r in results:
        print(f"{r['id']:<12} {_pct(r['median']['mape']):>12} {_pct(r['mean']['mape']):>10}  "
              f"{len(r['mean']['zero_actual_lines']):>18}  {len(r['median']['unbudgeted']):>10}")
    print(f"{'pooled':<12} {_pct(pooled['median']):>12} {_pct(pooled['mean']):>10}")


def run(profiles, out_root: Path, day: date) -> Path:
    results = [run_profile(p) for p in profiles]
    # pooled over every scored line of every profile, so each line counts once
    pooled = {c: mape([(ln["limit_cents"], ln["actual_cents"]) for r in results for ln in r[c]["lines"]
                       if ln["actual_cents"] > 0]) for c in CENTERS}
    for r in results:
        _print_budget(r)
    _print_table(results, pooled)
    path = results_dir(out_root, day, "budget_accuracy") / "results.json"
    _write(path, {"git_commit": _git_commit(), "profiles": results, "pooled": pooled})
    print(f"\nwrote {path}")
    return path


def main(argv=None) -> int:
    run(PROFILES, RESULTS_DIR, date.today())
    return 0


if __name__ == "__main__":
    sys.exit(main())
