"""Payment-attempt recovery and callback/creation response race regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.user.subscription import payments_2328 as handler
from db.dal import payment_dal


@pytest.fixture
def payment_flow(monkeypatch):
    row = SimpleNamespace(payment_id=7, user_id=111, provider="2328", status="pending_2328",
                          provider_payment_id=None, description="Original invoice description")
    callback = SimpleNamespace(data="pay_2328:1:129.0:subscription", from_user=SimpleNamespace(id=111),
                               message=SimpleNamespace(), answer=AsyncMock())
    service = SimpleNamespace(configured=True, create_invoice=AsyncMock(return_value=(True, {
        "uuid": "invoice-1", "url": "https://2328.io/pay/invoice-1", "payment_status": "check",
    })))
    session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    lookup = AsyncMock(return_value=None)
    create = AsyncMock(return_value=row)
    update = AsyncMock()
    change_status = AsyncMock()
    monkeypatch.setattr(handler, "ensure_panel_available_or_alert", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "resolve_fiat_offer_price_for_user", AsyncMock(return_value=129.0))
    monkeypatch.setattr(handler, "safe_edit_text", AsyncMock())
    monkeypatch.setattr(handler, "get_payment_url_keyboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(payment_dal, "get_unconfirmed_provider_payment", lookup)
    monkeypatch.setattr(payment_dal, "create_payment_record", create)
    monkeypatch.setattr(payment_dal, "get_payment_by_db_id", AsyncMock(return_value=row))
    monkeypatch.setattr(payment_dal, "update_provider_payment_and_status", update)
    monkeypatch.setattr(payment_dal, "update_payment_status_by_db_id", change_status)

    async def run():
        await handler.pay_2328_callback_handler(
            callback, SimpleNamespace(DEFAULT_LANGUAGE="ru"),
            {"i18n_instance": SimpleNamespace(gettext=lambda lang, key, **kwargs: key)},
            service, None, session, promo_code_service=object(),
        )

    return SimpleNamespace(row=row, service=service, lookup=lookup, create=create,
                           update=update, change_status=change_status, run=run)


async def test_retry_reuses_unconfirmed_order_and_its_description(payment_flow):
    flow = payment_flow
    flow.row.status = "pending_2328_unknown"
    flow.lookup.return_value = flow.row
    await flow.run()
    flow.create.assert_not_awaited()
    request = flow.service.create_invoice.await_args.kwargs
    assert request["payment_db_id"] == 7
    assert request["description"] == "Original invoice description"
    assert request["promo_code_service"] is None
    flow.update.assert_awaited_once()
    assert flow.update.await_args.args[-1] == "pending_2328"


@pytest.mark.parametrize("creation_succeeds", [True, False])
async def test_fast_webhook_cannot_be_overwritten_by_creation_response(payment_flow, creation_succeeds):
    flow = payment_flow
    result = flow.service.create_invoice.return_value

    async def settle_during_request(**kwargs):
        flow.row.status = "succeeded"
        return result if creation_succeeds else (False, {"uncertain": True})

    flow.service.create_invoice.side_effect = settle_during_request
    await flow.run()
    flow.update.assert_not_awaited()
    flow.change_status.assert_not_awaited()
    assert flow.row.status == "succeeded"


async def test_unconfirmed_creation_remains_retryable(payment_flow):
    flow = payment_flow
    flow.service.create_invoice.return_value = (False, {"uncertain": True})
    await flow.run()
    flow.change_status.assert_awaited_once()
    assert flow.change_status.await_args.args[-1] == "pending_2328_unknown"
