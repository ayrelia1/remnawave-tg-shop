from types import SimpleNamespace

from bot.handlers.admin.statistics import _format_payment_user_info


def test_payment_user_info_escapes_telegram_html() -> None:
    payment = SimpleNamespace(
        user_id=123456789,
        user=SimpleNamespace(username=None, first_name="<> & <b>name</b>"),
    )

    assert _format_payment_user_info(payment) == (
        "User 123456789 (&lt;&gt; &amp; &lt;b&gt;name&lt;/b&gt;)"
    )
