"""Guards on the Alembic chain and on migration/model parity.

A column that exists on the model but not in the migration only blows up in
production, on the first query against a freshly migrated database — so it is
worth asserting here.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from db.models import (  # noqa: E402
    AdCampaign,
    CampaignAccrual,
    CurrencyRate,
    Payment,
    PartnerPayout,
)

MIGRATIONS = REPO_ROOT / "alembic" / "versions"
HEAD = "0007_payment_base_amount"


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_single_alembic_head(script_directory):
    heads = script_directory.get_heads()
    assert len(heads) == 1, f"expected one head, got {heads}"


def test_every_revision_is_reachable_from_head(script_directory):
    head = script_directory.get_heads()[0]
    walked = {rev.revision for rev in script_directory.walk_revisions("base", head)}
    on_disk = {
        rev.revision for rev in script_directory.walk_revisions("base", "heads")
    }
    assert walked == on_disk


def test_latest_migration_is_the_head(script_directory):
    assert list(script_directory.get_heads()) == [HEAD]


def _load(name):
    path = MIGRATIONS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captured_tables(module, builder_name):
    """Run a migration's table builder against a fake `op` and collect columns."""
    captured = {}

    def fake_create_table(name, *columns, **kwargs):
        captured[name] = [c for c in columns if isinstance(c, sa.Column)]

    fake_op = SimpleNamespace(
        create_table=fake_create_table,
        create_index=lambda *a, **kw: None,
    )
    with patch.object(module, "op", fake_op):
        getattr(module, builder_name)()
    return captured


def test_ad_campaign_new_columns_match_the_model():
    module = _load("0005_partner_programs")
    migration_columns = {name: factory() for name, factory in module.CAMPAIGN_COLUMNS}

    assert set(migration_columns) == {
        "campaign_type",
        "partner_user_id",
        "partner_percent",
    }

    model_columns = AdCampaign.__table__.columns
    for name, column in migration_columns.items():
        model_column = model_columns[name]
        assert type(column.type) is type(model_column.type), name
        assert column.nullable == model_column.nullable, name


def _apply_0007(table: str, columns: dict) -> dict:
    """Replay 0007's renames and drops so the comparison sees the final shape."""
    module = _load("0007_payment_base_amount")
    for target, old, new in module.RENAMES:
        if target == table and old in columns:
            columns[new] = columns.pop(old)
    if table == "campaign_accruals":
        columns.pop("rate", None)
    return columns


def test_partner_payouts_columns_match_the_model_across_revisions():
    """0005 creates the table, 0006 adds the frozen-rate columns, 0007 renames them."""
    created = _captured_tables(_load("0005_partner_programs"), "_create_payouts_table")
    columns = {c.name: c for c in created["partner_payouts"]}
    columns.update(
        {name: factory() for name, factory in _load("0006_campaign_accruals").PAYOUT_COLUMNS}
    )
    columns = _apply_0007("partner_payouts", columns)

    model_columns = {c.name: c for c in PartnerPayout.__table__.columns}
    assert set(columns) == set(model_columns)
    for name, column in columns.items():
        assert type(column.type) is type(model_columns[name].type), name


def test_campaign_accruals_table_matches_the_model():
    created = _captured_tables(_load("0006_campaign_accruals"), "_create_accruals_table")
    columns = _apply_0007(
        "campaign_accruals", {c.name: c for c in created["campaign_accruals"]}
    )
    model_columns = {c.name: c for c in CampaignAccrual.__table__.columns}

    assert set(columns) == set(model_columns)
    for name, column in columns.items():
        assert type(column.type) is type(model_columns[name].type), name

    # The ledger must not be able to hold two rows for one payment.
    assert any(c.name == "payment_id" for c in created["campaign_accruals"])


def test_currency_rates_table_matches_the_model():
    created = _captured_tables(_load("0007_payment_base_amount"), "_create_rates_table")
    columns = {c.name: c for c in created["currency_rates"]}
    model_columns = {c.name: c for c in CurrencyRate.__table__.columns}

    assert set(columns) == set(model_columns)
    for name, column in columns.items():
        assert type(column.type) is type(model_columns[name].type), name


def test_payment_normalisation_columns_match_the_model():
    module = _load("0007_payment_base_amount")
    columns = {name: factory() for name, factory in module.PAYMENT_COLUMNS}
    assert set(columns) == {"base_amount", "fx_rate"}

    model_columns = Payment.__table__.columns
    for name, column in columns.items():
        assert type(column.type) is type(model_columns[name].type), name
        # Both must stay nullable: an unconfigured currency leaves them unset.
        assert column.nullable is True, name
        assert model_columns[name].nullable is True, name


def test_existing_payments_are_valued_one_to_one():
    """The upgrade must not change any historical total."""
    module = _load("0007_payment_base_amount")
    import inspect

    source = inspect.getsource(module._value_existing_payments)
    assert "fx_rate = 1.0" in source
    assert "base_amount = amount" in source
    assert "WHERE base_amount IS NULL" in source


def test_rate_seed_covers_the_base_currency_and_stars():
    module = _load("0007_payment_base_amount")
    seeded = module._seed_rates()
    assert seeded["RUB"] == 1.0
    assert seeded["XTR"] == 1.0


def test_final_backfill_completes_the_ledger_for_every_valued_payment():
    """0006 could only backfill known currencies; 0007 finishes the job."""
    module = _load("0007_payment_base_amount")
    sql = module.BACKFILL_ACCRUALS_SQL

    assert "INSERT INTO campaign_accruals" in sql
    # Driven by the payment's own frozen value, not by any rate table.
    assert "p.base_amount IS NOT NULL" in sql
    assert "p.status = 'succeeded'" in sql
    assert "p.created_at >= a.first_start_at" in sql
    assert "COALESCE(c.partner_percent, 0)" in sql
    assert "NOT EXISTS" in sql


def test_rate_seed_also_covers_every_currency_already_in_payments():
    """Nothing existing may be left without a rate, or it would drop out of totals."""
    import inspect

    module = _load("0007_payment_base_amount")
    source = inspect.getsource(module._insert_seed_rates)
    assert "SELECT DISTINCT UPPER(currency), 1.0 FROM payments" in source
    assert "ON CONFLICT (currency) DO NOTHING" in source


def test_campaign_type_backfills_existing_rows_as_ad():
    """Existing ad campaigns must keep behaving as ads after the upgrade."""
    module = _load("0005_partner_programs")
    campaign_type = dict(module.CAMPAIGN_COLUMNS)["campaign_type"]()
    assert campaign_type.server_default.arg == "ad"
    assert campaign_type.nullable is False


def test_accrual_backfill_only_values_currencies_with_a_known_rate():
    """Face-value backfill of an unpriced currency is exactly the bug to avoid."""
    module = _load("0006_campaign_accruals")
    assert module.BACKFILL_RATES == {"RUB": 1.0, "XTR": 1.0}

    sql = module._backfill_sql()
    assert "INSERT INTO campaign_accruals" in sql
    # Restricted to the priced currencies...
    assert "UPPER(p.currency) IN ('RUB', 'XTR')" in sql
    # ...only succeeded payments made after attribution...
    assert "p.status = 'succeeded'" in sql
    assert "p.created_at >= a.first_start_at" in sql
    # ...covering ad campaigns as well as partner ones...
    assert "COALESCE(c.partner_percent, 0)" in sql
    # ...and never duplicating a payment already in the ledger.
    assert "NOT EXISTS" in sql
