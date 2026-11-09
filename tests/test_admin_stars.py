from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.methods import AnswerPreCheckoutQuery, SendInvoice, SendMessage
from aiogram.types import (
    Chat,
    Message,
    PreCheckoutQuery,
    RefundedPayment,
    SuccessfulPayment,
    Update,
    User,
)

from bot.app.controllers import dispatcher_controller
from bot.middlewares.i18n import JsonI18n


@pytest.fixture
def stars_dispatcher(monkeypatch):
    settings = SimpleNamespace(
        BOT_TOKEN="123456:TEST",
        DEFAULT_LANGUAGE="ru",
        PREMIUM_EMOJI_ENABLED=False,
        ADMIN_IDS=[100, 101],
        STARS_ENABLED=False,
    )
    i18n = JsonI18n("locales", default="ru", premium_emoji=False)
    monkeypatch.setattr(dispatcher_controller, "get_i18n_instance", lambda **_: i18n)
    db = Mock(side_effect=AssertionError("Opened a database session"))
    dispatcher, bot, _ = dispatcher_controller.build_dispatcher(settings, db)
    request = AsyncMock()
    monkeypatch.setattr(bot.session, "make_request", request)
    return dispatcher, bot, request, db


def _user(user_id=100):
    return User(id=user_id, is_bot=False, first_name="Tester", language_code="en")


def _message(user_id=100, chat_type="private", **fields):
    return Message(
        message_id=1,
        date=0,
        chat=Chat(id=user_id if chat_type == "private" else -123, type=chat_type),
        from_user=_user(user_id),
        **fields,
    )


@pytest.mark.asyncio
async def test_admin_stars_payment_runs_without_database(stars_dispatcher):
    dispatcher, bot, request, db = stars_dispatcher
    await dispatcher.feed_update(bot, Update(update_id=1, message=_message(text="/stars 500")))

    request.assert_awaited_once()
    invoice = request.await_args.args[1]
    assert isinstance(invoice, SendInvoice)
    assert invoice.chat_id == 100
    assert invoice.currency == "XTR"
    assert [price.amount for price in invoice.prices] == [500]
    assert invoice.start_parameter

    request.reset_mock()
    query = PreCheckoutQuery(
        id="checkout",
        from_user=_user(),
        currency="XTR",
        total_amount=500,
        invoice_payload=invoice.payload,
    )
    await dispatcher.feed_update(bot, Update(update_id=2, pre_checkout_query=query))
    request.assert_awaited_once()
    answer = request.await_args.args[1]
    assert isinstance(answer, AnswerPreCheckoutQuery)
    assert answer.ok is True

    request.reset_mock()
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=500,
        invoice_payload=invoice.payload,
        telegram_payment_charge_id="telegram",
        provider_payment_charge_id="provider",
    )
    for update_id in (3, 4):
        await dispatcher.feed_update(
            bot, Update(update_id=update_id, message=_message(successful_payment=payment))
        )
    refund = RefundedPayment(
        total_amount=500,
        invoice_payload=invoice.payload,
        telegram_payment_charge_id="telegram",
    )
    await dispatcher.feed_update(
        bot, Update(update_id=5, message=_message(refunded_payment=refund))
    )
    request.assert_not_awaited()
    db.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "0", "-1", "1.5", "10 20", "1_000", "9" * 30])
async def test_admin_stars_requires_positive_integer(stars_dispatcher, args):
    dispatcher, bot, request, db = stars_dispatcher
    await dispatcher.feed_update(
        bot, Update(update_id=1, message=_message(text=f"/stars {args}"))
    )
    request.assert_awaited_once()
    reply = request.await_args.args[1]
    assert isinstance(reply, SendMessage)
    assert "/stars 500" in reply.text
    db.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id, chat_type", [(200, "private"), (100, "group")])
async def test_stars_command_only_uses_early_route_for_admins_in_private(
    stars_dispatcher, user_id, chat_type
):
    dispatcher, bot, request, db = stars_dispatcher
    message = _message(user_id, chat_type, text="/stars 500")
    with pytest.raises(AssertionError, match="Opened a database session"):
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    db.assert_called_once()
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_id, currency, amount",
    [(101, "XTR", 500), (200, "XTR", 500), (100, "USD", 500), (100, "XTR", 501)],
)
async def test_stars_checkout_rejects_a_mismatch(stars_dispatcher, user_id, currency, amount):
    dispatcher, bot, request, db = stars_dispatcher
    await dispatcher.feed_update(bot, Update(update_id=1, message=_message(text="/stars 500")))
    invoice = request.await_args.args[1]
    request.reset_mock()

    query = PreCheckoutQuery(
        id="checkout",
        from_user=_user(user_id),
        currency=currency,
        total_amount=amount,
        invoice_payload=invoice.payload,
    )
    await dispatcher.feed_update(bot, Update(update_id=2, pre_checkout_query=query))
    request.assert_awaited_once()
    answer = request.await_args.args[1]
    assert isinstance(answer, AnswerPreCheckoutQuery)
    assert answer.ok is False
    assert answer.error_message
    db.assert_not_called()


@pytest.mark.asyncio
async def test_shop_stars_checkout_keeps_database_route(stars_dispatcher):
    dispatcher, bot, request, db = stars_dispatcher
    query = PreCheckoutQuery(
        id="checkout",
        from_user=_user(),
        currency="XTR",
        total_amount=500,
        invoice_payload="123:1:subscription",
    )
    with pytest.raises(AssertionError, match="Opened a database session"):
        await dispatcher.feed_update(bot, Update(update_id=1, pre_checkout_query=query))
    db.assert_called_once()
    request.assert_not_awaited()
