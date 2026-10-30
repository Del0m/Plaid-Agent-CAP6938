# Tests: `backend/tests/test_migrations.py`

These tests check Alembic migrations against databases the test suite doesn't run. Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_migrations.py -v
```

## Shared setup

- `load(name)` imports one file from `alembic/versions/` as a module.
- `postgres_downgrade_sql(name)` runs that migration's `downgrade()` in Alembic's offline mode with the `postgresql` dialect and returns the SQL it would run. It needs no Postgres server and no driver.

---

## `test_budget_tables_downgrade_drops_enum_types`

**What it does:** Renders the Postgres SQL for the budget-tables migration's (`9e005ad8cd4c`) downgrade, and checks that it drops the `budget_period_status`, `budget_line_kind` and `budget_line_source` types.

**Why it's there:** On Postgres an `Enum` column creates a native type, and dropping the table leaves the type behind. So downgrading and then upgrading again failed with `type "budget_period_status" already exists`. The decision log plans for Postgres, but the suite only runs SQLite, which has no enum types, so this could only be caught by rendering the Postgres SQL. The review on #11 found it. The downgrade now runs `DROP TYPE IF EXISTS` for each type on Postgres only. A real SQLite upgrade, downgrade and upgrade still succeeds.
