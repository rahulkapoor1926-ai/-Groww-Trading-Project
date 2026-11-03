from datetime import datetime

import pytest

from app import create_app
from app.groww_client import IST, GrowwSession, next_token_expiry

FAKE_PROFILE = {
    "vendor_user_id": "abc-123",
    "ucc": "123456",
    "nse_enabled": True,
    "bse_enabled": False,
    "ddpi_enabled": True,
    "active_segments": ["CASH", "FNO"],
}
FAKE_MARGINS = {
    "clear_cash": 12345.5,
    "net_margin_used": 100.0,
    "collateral_available": 0,
    "equity_margin_details": {"cnc_balance_available": 12000.25},
}


class FakeClient:
    def get_user_profile(self, timeout=None):
        return FAKE_PROFILE

    def get_available_margin_details(self, timeout=None):
        return FAKE_MARGINS


class FakeSession(GrowwSession):
    def login(self):
        self._client = FakeClient()
        self._expires_at = next_token_expiry(datetime.now(IST))
        return self._client


@pytest.fixture
def client():
    app = create_app(FakeSession(api_key="k", totp_secret="JBSWY3DPEHPK3PXP"))
    app.config["TESTING"] = True
    return app.test_client()


def test_shows_connect_button_before_login(client):
    html = client.get("/").get_data(as_text=True)
    assert "Connect to Groww" in html


def test_login_shows_profile_and_funds(client):
    resp = client.post("/login", follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Connected to Groww" in html
    assert "123456" in html
    assert "12,345.50" in html
    assert "Cnc balance available" in html


def test_logout(client):
    client.post("/login")
    html = client.post("/logout", follow_redirects=True).get_data(as_text=True)
    assert "Connect to Groww" in html


def test_missing_config_shows_setup():
    app = create_app(GrowwSession(api_key=None, totp_secret=None))
    html = app.test_client().get("/").get_data(as_text=True)
    assert "Setup needed" in html


def test_token_expiry_is_next_6am_ist():
    before = datetime(2026, 1, 1, 5, 0, tzinfo=IST)
    after = datetime(2026, 1, 1, 9, 0, tzinfo=IST)
    assert next_token_expiry(before) == datetime(2026, 1, 1, 6, 0, tzinfo=IST)
    assert next_token_expiry(after) == datetime(2026, 1, 2, 6, 0, tzinfo=IST)
