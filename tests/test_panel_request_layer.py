"""The HTTP envelope handling in PanelApiService._request.

Remnawave 3.x answers 204 No Content / 202 Accepted with an empty body for
every DELETE, the async bulk operations and node restarts, so `_request` has to
turn "no body" into a success envelope rather than a parse failure.
"""

import json

import pytest

from bot.services.panel_api_service import PanelApiService, PanelUnavailableError


class FakeResponse:
    def __init__(self, status: int, body: str = "", content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self._response


@pytest.fixture
def make_panel(panel_settings, monkeypatch):
    def _make(response: FakeResponse) -> tuple[PanelApiService, FakeSession]:
        service = PanelApiService(panel_settings)
        session = FakeSession(response)

        async def _get_session():
            return session

        monkeypatch.setattr(service, "_get_session", _get_session)
        return service, session

    return _make


async def test_204_empty_body_becomes_success_envelope(make_panel):
    service, _ = make_panel(FakeResponse(204, body="", content_type=""))

    result = await service._request("DELETE", "/users/42")

    assert result["empty_body"] is True
    assert result["code"] == 204
    assert result["response"] is None
    assert not result.get("error")


async def test_202_accepted_empty_body_is_success(make_panel):
    service, _ = make_panel(FakeResponse(202, body="", content_type=""))

    result = await service._request("POST", "/users/bulk/delete")

    assert result["empty_body"] is True
    assert not result.get("error")


async def test_200_with_whitespace_only_body_is_success(make_panel):
    service, _ = make_panel(FakeResponse(200, body="   \n", content_type="application/json"))

    result = await service._request("POST", "/users/42/actions/enable")

    assert result["empty_body"] is True
    assert not result.get("error")


async def test_json_body_is_parsed(make_panel):
    payload = {"response": {"id": 42, "username": "tg_111"}}
    service, _ = make_panel(FakeResponse(200, body=json.dumps(payload)))

    result = await service._request("GET", "/users/42")

    assert result == payload


async def test_error_status_surfaces_error_code(make_panel):
    body = json.dumps({"errorCode": "A025", "message": "User not found"})
    service, _ = make_panel(FakeResponse(404, body=body))

    result = await service._request("GET", "/users/42")

    assert result["error"] is True
    assert result["status_code"] == 404
    assert result["details"]["errorCode"] == "A025"


async def test_5xx_is_classified_transient(make_panel):
    service, _ = make_panel(FakeResponse(502, body="bad gateway", content_type="text/plain"))

    result = await service._request("GET", "/system/stats")

    assert PanelApiService._is_transient_error(result) is True
    with pytest.raises(PanelUnavailableError):
        service._raise_if_transient(result, "test")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_4xx_is_not_transient(make_panel, status):
    service, _ = make_panel(FakeResponse(status, body="{}"))

    result = await service._request("GET", "/users/42")

    assert PanelApiService._is_transient_error(result) is False


async def test_request_targets_the_configured_base_url(make_panel):
    service, session = make_panel(FakeResponse(200, body='{"response": {}}'))

    await service._request("GET", "/users/stream", params={"size": 500})

    method, url, kwargs = session.requests[0]
    assert method == "GET"
    assert url == "https://panel.example.com/api/users/stream"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["params"] == {"size": 500}


async def test_missing_base_url_short_circuits(panel_settings):
    panel_settings.PANEL_API_URL = None
    service = PanelApiService(panel_settings)

    result = await service._request("GET", "/users")

    assert result["error"] is True
    assert result["status_code"] == 0


def test_api_key_is_redacted_from_logged_payloads():
    sanitized = PanelApiService._sanitize_payload_for_log(
        {"id": 42, "apiKey": "secret", "nested": {"password": "hunter2", "keep": 1}}
    )

    assert sanitized["id"] == 42
    assert sanitized["apiKey"] == "***"
    assert sanitized["nested"]["password"] == "***"
    assert sanitized["nested"]["keep"] == 1
