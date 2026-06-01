"""Integration tests — hit the real Gixen web interface.

These tests require GIXEN_USERNAME and GIXEN_PASSWORD env vars.
Run with: pytest -m integration
"""

import os
import pytest
from decimal import Decimal

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gixen_client import GixenClient, GixenSnipeNotFoundError

# Skip all tests in this file if credentials are not set
pytestmark = pytest.mark.integration

GIXEN_USERNAME = os.getenv("GIXEN_USERNAME", "")
GIXEN_PASSWORD = os.getenv("GIXEN_PASSWORD", "")

requires_creds = pytest.mark.skipif(
    not GIXEN_USERNAME or not GIXEN_PASSWORD,
    reason="GIXEN_USERNAME and GIXEN_PASSWORD not set",
)


@pytest.fixture
def client():
    return GixenClient(username=GIXEN_USERNAME, password=GIXEN_PASSWORD)


@requires_creds
class TestLogin:
    def test_login_establishes_session(self, client):
        sid = client.login()
        assert sid
        assert sid.isdigit()
        assert client.session_id == sid


@requires_creds
class TestListSnipes:
    def test_list_returns_list(self, client):
        snipes = client.list_snipes()
        assert isinstance(snipes, list)
        if snipes:
            assert "item_id" in snipes[0]
            assert "max_bid" in snipes[0]
            assert "dbidid" in snipes[0]


@requires_creds
class TestPurge:
    def test_purge_completed(self, client):
        result = client.purge_completed()
        assert result is True
