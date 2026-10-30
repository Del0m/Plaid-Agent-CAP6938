# Tests: `backend/tests/test_budget_accuracy.py`

These tests cover `eval/budget_accuracy.py`, the "initial budget accuracy" metric (TPC-18, architecture 10.2). For each synthetic user in `eval/budget_users.py`, it:
- runs the real `initialize_budget` and prints the stored budget;
- fits the same history with the median and with the mean;
- scores each against the next month's actual spending: MAPE over lines whose category had spending, plus counts of lines with $0 actual and of spending with no line.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_accuracy.py -v
```

## Shared setup

- `profile(id)` looks a profile up in `PROFILES`.
- `test_run_writes_results` writes into pytest's `tmp_path`, never into `eval/results/`.

---

## `test_score_counts`

**What it does:** A $1.10 food line against $1.00 actual scores 10%. A shopping line with no August spending is listed in `zero_actual_lines`, and travel spending with no line is listed in `unbudgeted`.

**Why it's there:** MAPE can't divide by a $0 actual, and it can't see a category with no limit. Both are real budget mistakes, so they're counted next to the MAPE instead of being dropped silently.

## `test_run_writes_results`

**What it does:** Running two profiles writes `results.json` under `<date>/budget_accuracy/`, with per-profile scores for both centers and a pooled MAPE for each. Vacation's median MAPE is below its mean MAPE, and the printed output names the profiles.

**Why it's there:** It checks the whole script end to end. The vacation result is the one comparison the median is designed to win (one $1,900 trip), so if it ever flips, something in the fit or the profile has changed.

## `test_stored_budget_matches_median_fit`

**What it does:** The limits `initialize_budget` stored are the same as the median fit the script scores.

**Why it's there:** The script scores fits directly so it can compare centers. This keeps the scored median the same budget a real user would get.

## `test_early_rent_fixed_line_is_one_rent`

**What it does:** In the early-rent profile (two rents in June, none in July), the fitted rent is $1,500 and August's actual rent is $1,500.

**Why it's there:** It's the double-month case that changed the fixed rule, checked through the database and the real loader instead of hand-built `Txn`s.

## `test_occasional_mean_has_zero_actual_line`

**What it does:** In the occasional profile, the mean fit gives the one-off June purchase a line that August can't score, while the median skips it, and the stored budget lists it as skipped.

**Why it's there:** It's the mean's known weakness, and it only shows up in `zero_actual_lines`, not in the MAPE. Checking it here makes sure the report surfaces it.

## `test_history_window_follows_history_months`

**What it does:** Sets `fit.HISTORY_MONTHS` to 4, runs the vacation profile on Sep 1, and records the history passed to each scored fit. Every fit's history must start in May, four months back.

**Why it's there:** The eval loaded history with a hardcoded `-3`, a copy of `initialize_budget`'s window. If `HISTORY_MONTHS` changed, the stored budget and the median/mean fits being scored would use different windows, and the comparison would no longer be like for like. The window now reads `fit.HISTORY_MONTHS` at call time. The review on #11 found it.

## `test_profile_without_a_budget_is_reported`

**What it does:** Runs the steady profile with `today` set to May 20, when it has no complete month of history, and checks that the result's `budget` is `None`.

**Why it's there:** `initialize_budget` returns `None` when there's nothing to fit, and `run_profile` then crashed on `period.lines`, stopping the whole eval before `results.json` was written. The review on #11 found it.

## `test_run_with_an_unbudgeted_profile`

**What it does:** Runs the eval over one normal profile and one with no budget. It checks that the pooled median MAPE is still computed and that the printout says "no budget".

**Why it's there:** A profile with no budget has no scores, so pooling and the results table only include profiles that got one.
