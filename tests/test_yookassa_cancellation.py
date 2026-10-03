"""Canceled YooKassa payments are recorded without messaging the customer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.handlers.user.payment import process_cancelled_payment
from db.dal import payment_dal, user_dal


def _canceled_payment():
    return {
        "id": "yk-payment-1",
        "status": "canceled",
        "metadata": {"user_id": "111", "payment_db_id": "7"},
    }


async def test_canceled_payment_updates_status_without_notifying_user(monkeypatch):
    session = AsyncMock()
    bot = SimpleNamespace(send_message=AsyncMock())
    i18n = SimpleNamespace(gettext=Mock(return_value="payment_failed"))
    settings = SimpleNamespace(DEFAULT_LANGUAGE="ru")
    update_status = AsyncMock(return_value=SimpleNamespace(payment_id=7))
    monkeypatch.setattr(payment_dal, "update_payment_status_by_db_id", update_status)
    monkeypatch.setattr(
        user_dal, "get_user_by_id", AsyncMock(return_value=SimpleNamespace(language_code="ru"))
    )

    for _ in range(2):
        await process_cancelled_payment(session, bot, _canceled_payment(), i18n, settings)

    assert update_status.await_count == 2
    update_status.assert_awaited_with(
        session, payment_db_id=7, new_status="canceled", yk_payment_id="yk-payment-1"
    )
    bot.send_message.assert_not_awaited()


async def test_cancellation_database_error_is_still_propagated(monkeypatch):
    session = AsyncMock()
    bot = SimpleNamespace(send_message=AsyncMock())
    update_status = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(payment_dal, "update_payment_status_by_db_id", update_status)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await process_cancelled_payment(session, bot, _canceled_payment(), None, None)

    bot.send_message.assert_not_awaited()
