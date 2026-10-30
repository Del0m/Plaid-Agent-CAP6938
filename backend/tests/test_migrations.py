import importlib.util
import io
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def load(name):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# the sql a migration's downgrade would run on postgres, rendered offline (no server or driver needed)
def postgres_downgrade_sql(name) -> str:
    out = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": out})
    with Operations.context(ctx):
        load(name).downgrade()
    return out.getvalue()


# review on #11: postgres keeps native enum types after their tables are dropped, so a later upgrade would fail
def test_budget_tables_downgrade_drops_enum_types():
    sql = postgres_downgrade_sql("9e005ad8cd4c_budget_tables")

    for enum in ("budget_period_status", "budget_line_kind", "budget_line_source"):
        assert f"DROP TYPE IF EXISTS {enum}" in sql
