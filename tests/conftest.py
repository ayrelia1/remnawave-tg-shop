"""Shared fixtures for the Remnawave panel integration tests.

The tests exercise the bot against the Remnawave 3.2.1 API contract
(https://github.com/remnawave/backend/tree/3.2.1/libs/contract) without
touching the network or a database: the HTTP layer and the DAL are stubbed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.services.panel_api_service import PanelApiService  # noqa: E402


class RecordingPanelApiService(PanelApiService):
    """PanelApiService with `_request` replaced by a scripted queue.

    Every call is recorded as (method, endpoint, kwargs) so tests can assert on
    the exact route and payload sent to the panel.
    """

    def __init__(self, settings, responses: Optional[List[Any]] = None):
        super().__init__(settings)
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.responses: List[Any] = list(responses or [])

    async def _request(self, method: str, endpoint: str, log_full_response: bool = False, **kwargs):
        self.calls.append((method.upper(), endpoint, kwargs))
        if not self.responses:
            return None
        return self.responses.pop(0)

    @property
    def last_call(self) -> Tuple[str, str, Dict[str, Any]]:
        return self.calls[-1]


@pytest.fixture
def panel_settings() -> SimpleNamespace:
    return SimpleNamespace(
        PANEL_API_URL="https://panel.example.com/api",
        PANEL_API_KEY="test-token",
        LOG_LEVEL="INFO",
        USER_HWID_DEVICE_LIMIT=None,
    )


@pytest.fixture
def panel(panel_settings) -> RecordingPanelApiService:
    return RecordingPanelApiService(panel_settings)


def ok(response: Any) -> Dict[str, Any]:
    """A successful panel envelope: every 2xx JSON body is {"response": ...}."""
    return {"response": response}


def api_error(status_code: int, error_code: Optional[str] = None) -> Dict[str, Any]:
    """The shape `PanelApiService._request` returns for a non-2xx answer."""
    details: Dict[str, Any] = {"message": f"Request failed with status {status_code}"}
    if error_code:
        details["errorCode"] = error_code
    return {"error": True, "status_code": status_code, "details": details}


def empty_body(status_code: int = 204) -> Dict[str, Any]:
    """The shape `_request` returns for 204/202 answers (3.x DELETE & bulk)."""
    return {
        "status": "success",
        "code": status_code,
        "response": None,
        "empty_body": True,
    }


def panel_user(
    user_id: int = 42,
    telegram_id: Optional[int] = 111,
    username: str = "tg_111",
    **extra: Any,
) -> Dict[str, Any]:
    """A user object shaped like Remnawave 3.x ExtendedUsersSchema (no `uuid`)."""
    user: Dict[str, Any] = {
        "id": user_id,
        "shortUuid": "short-abc",
        "username": username,
        "status": "ACTIVE",
        "trafficLimitBytes": 0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": "2026-12-31T00:00:00.000Z",
        "telegramId": telegram_id,
        "email": None,
        "description": None,
        "tag": None,
        "hwidDeviceLimit": None,
        "externalSquadUuid": None,
        "subscriptionUrl": "https://panel.example.com/api/sub/short-abc",
        "activeInternalSquads": [],
        "userTraffic": {
            "usedTrafficBytes": 0,
            "lifetimeUsedTrafficBytes": 0,
            "onlineAt": None,
            "firstConnectedAt": None,
            "lastConnectedNodeUuid": None,
        },
    }
    user.update(extra)
    return user
