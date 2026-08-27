"""The admin currency-rates screen, driven the way the handlers drive it."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.handlers.admin import currency_rates as screen  # noqa: E402
from db.dal import currency_dal, payment_dal  # noqa: E402
from db.models import Base, CurrencyRate, Payment, User  # noqa: E402

ADMIN_ID = 42


class FakeI18n:
    """Returns the key, so assertions read as "which string was shown"."""

    def gettext(self, lang, key, **kwargs):
        return key


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.from_user = SimpleNamespace(id=ADMIN_ID)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        return SimpleNamespace(message_id=1)


class FakeState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self._state = None
        self.cleared = False

    async def get_data(self):
        return dict(self._data)

    async def set_data(self, data):
        self._data = dict(data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        self._state = state

    async def clear(self):
        self.cleared = True
        self._data = {}
        self._state = None


def make_settings(**overrides):
    from config.settings import Settings

    base = {"BOT_TOKEN": "test:token", "_env_file": None}
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(CurrencyRate(currency="RUB", rate=1.0))
        db.add(CurrencyRate(currency="XTR", rate=1.0))
        db.add(User(user_id=1, username="buyer"))
        await db.flush()
        yield db
    await engine.dispose()


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [("1", 1.0), ("95.5", 95.5), ("95,5", 95.5), (" 2 ", 2.0)],
)
def test_rate_parsing_accepts_sane_numbers(raw, expected):
    assert screen._parse_rate(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "", "1e12"])
def test_rate_parsing_rejects_the_rest(raw):
    assert screen._parse_rate(raw) is None


@pytest.mark.parametrize(
    "raw, code, rate",
    [
        ("USDT 95", "USDT", "95"),
        ("usdt=95", "usdt", "95"),
        ("TON  300,5", "TON", "300,5"),
    ],
)
def test_new_currency_parsing(raw, code, rate):
    match = screen.NEW_CURRENCY_RE.match(raw)
    assert match is not None
    assert match.group(1) == code
    assert match.group(2) == rate


@pytest.mark.parametrize("raw", ["USDT", "95", "US DT 95", "USDT 95 extra"])
def test_new_currency_parsing_rejects_malformed_input(raw):
    assert screen.NEW_CURRENCY_RE.match(raw) is None


# --------------------------------------------------------------------------- #
# The screen itself
# --------------------------------------------------------------------------- #


async def test_render_lists_configured_rates(session):
    text, markup = await screen._render(session, FakeI18n(), "ru")

    assert "admin_currency_rates_header" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # The base currency is listed but marked, and it is not an edit button.
    assert "RUB = 1 (admin_currency_base_label)" in labels
    assert "XTR = 1" in labels
    assert "admin_currency_rate_add_button" in labels
    # Nothing unvalued yet, so no warning.
    assert "admin_currency_rates_unvalued" not in text


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_base_currency_row_is_not_editable(session):
    _text, markup = await screen._render(session, FakeI18n(), "ru")
    data = _callbacks(markup)
    assert "admin_rates:base" in data
    assert "admin_rates:edit:RUB" not in data
    assert "admin_rates:edit:XTR" in data


async def test_setting_the_base_currency_rate_is_refused(session):
    with pytest.raises(ValueError) as exc:
        await currency_dal.set_rate(session, "RUB", 2.0, base_currency="RUB")
    assert str(exc.value) == "currency_rate_base_is_fixed"
    assert await currency_dal.get_rate(session, "RUB") == 1.0

    # Writing the identity back is harmless and allowed.
    await currency_dal.set_rate(session, "rub", 1.0, base_currency="RUB")
    assert await currency_dal.get_rate(session, "RUB") == 1.0


async def test_deleting_the_base_currency_rate_is_refused(session):
    with pytest.raises(ValueError) as exc:
        await currency_dal.delete_rate(session, "RUB", base_currency="RUB")
    assert str(exc.value) == "currency_rate_base_is_fixed"
    assert await currency_dal.get_rate(session, "RUB") == 1.0


async def test_save_path_refuses_the_base_currency(session):
    message, state = FakeMessage(), FakeState()
    await screen._save(
        message, state, session, FakeI18n(), "ru", "RUB", 2.0
    )
    assert message.answers == ["admin_currency_base_locked"]
    assert await currency_dal.get_rate(session, "RUB") == 1.0
    assert state.cleared is False


async def test_base_rate_self_heals_if_tampered_with(session):
    """A row edited straight in the database is forced back to the identity."""
    row = await session.get(CurrencyRate, "RUB")
    row.rate = 7.0
    await session.flush()

    await currency_dal.ensure_base_rate(session, "RUB")
    assert await currency_dal.get_rate(session, "RUB") == 1.0


async def test_base_rate_is_created_when_missing(session):
    await session.delete(await session.get(CurrencyRate, "RUB"))
    await session.flush()
    assert await currency_dal.get_rate(session, "RUB") is None

    await currency_dal.ensure_base_rate(session, "RUB")
    assert await currency_dal.get_rate(session, "RUB") == 1.0


async def test_render_warns_about_unvalued_payments(session):
    payment = await payment_dal.create_payment_record(
        session,
        {
            "user_id": 1,
            "amount": 5.0,
            "currency": "USDT",
            "status": "succeeded",
            "provider": "cryptopay",
        },
    )
    await session.flush()
    assert payment.base_amount is None

    text, _markup = await screen._render(session, FakeI18n(), "ru")
    assert "admin_currency_rates_unvalued" in text


async def test_saving_a_rate_values_waiting_payments(session):
    await payment_dal.create_payment_record(
        session,
        {
            "user_id": 1,
            "amount": 5.0,
            "currency": "USDT",
            "status": "succeeded",
            "provider": "cryptopay",
        },
    )
    await session.flush()

    message, state = FakeMessage(), FakeState()
    await screen._save(
        message, state, session, FakeI18n(), "ru", "USDT", 95.0
    )

    assert await currency_dal.get_rate(session, "USDT") == 95.0
    assert state.cleared is True
    # Confirmation, then the refreshed list.
    assert message.answers[0] == "admin_currency_rate_saved\nadmin_currency_rate_valued"
    assert "admin_currency_rates_header" in message.answers[1]

    valued = (await session.execute(__import__("sqlalchemy").select(Payment))).scalars().all()
    assert [(p.currency, p.fx_rate, p.base_amount) for p in valued] == [("USDT", 95.0, 475.0)]
    assert await payment_dal.count_unvalued_payments(session) == 0


async def test_saving_a_rate_with_nothing_waiting_says_so_quietly(session):
    message, state = FakeMessage(), FakeState()
    await screen._save(
        message, state, session, FakeI18n(), "ru", "TON", 300.0
    )
    assert await currency_dal.get_rate(session, "TON") == 300.0
    # No "previously unvalued payments" line when there were none.
    assert message.answers[0] == "admin_currency_rate_saved"


async def test_editing_a_rate_does_not_touch_valued_payments(session):
    payment = await payment_dal.create_payment_record(
        session,
        {
            "user_id": 1,
            "amount": 250.0,
            "currency": "XTR",
            "status": "succeeded",
            "provider": "telegram_stars",
        },
    )
    await session.flush()
    assert (payment.fx_rate, payment.base_amount) == (1.0, 250.0)

    message, state = FakeMessage(), FakeState()
    await screen._save(
        message, state, session, FakeI18n(), "ru", "XTR", 3.0
    )

    refreshed = await payment_dal.get_payment_by_db_id(session, payment.payment_id)
    assert (refreshed.fx_rate, refreshed.base_amount) == (1.0, 250.0)

    # ...but the next payment uses the new rate.
    later = await payment_dal.create_payment_record(
        session,
        {
            "user_id": 1,
            "amount": 100.0,
            "currency": "XTR",
            "status": "pending_stars",
            "provider": "telegram_stars",
        },
    )
    assert (later.fx_rate, later.base_amount) == (3.0, 300.0)


async def test_receive_rate_rejects_bad_input_without_clearing_state(session):
    message, state = FakeMessage("не число"), FakeState({"currency_code": "XTR"})
    await screen.receive_rate(
        message,
        state,
        make_settings(),
        {"current_language": "ru", "i18n_instance": FakeI18n()},
        session,
    )
    assert message.answers == ["admin_currency_rate_invalid_value"]
    assert state.cleared is False
    assert await currency_dal.get_rate(session, "XTR") == 1.0


async def test_receive_new_currency_rejects_malformed_input(session):
    message, state = FakeMessage("USDT"), FakeState()
    await screen.receive_new_currency(
        message,
        state,
        make_settings(),
        {"current_language": "ru", "i18n_instance": FakeI18n()},
        session,
    )
    assert message.answers == ["admin_currency_rate_add_invalid"]
    assert await currency_dal.get_rate(session, "USDT") is None
