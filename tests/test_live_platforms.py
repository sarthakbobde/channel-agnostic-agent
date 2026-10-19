"""Checks that need a real Microsoft tenant or Google Workspace. Skipped unless the credentials are in the environment:
    TEAMS_LIVE_APP_ID, TEAMS_LIVE_APP_PASSWORD, TEAMS_LIVE_TENANT_ID   (an Azure Bot registration)
    GCHAT_LIVE=1                                                       (internet access to Google's certificates)
Run with:  python -m pytest -m live tests/test_live_platforms.py
The default run (python -m pytest) leaves them out."""
import os

import pytest

from app.channels.manager import ChannelManager

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.getenv("TEAMS_LIVE_APP_ID"), reason="needs a real Azure Bot registration")
def test_teams_credentials_are_accepted_by_microsoft():
    ok, msg = ChannelManager().test("teams", {"app_id": os.environ["TEAMS_LIVE_APP_ID"], "app_password": os.environ["TEAMS_LIVE_APP_PASSWORD"],
                                              "tenant_id": os.getenv("TEAMS_LIVE_TENANT_ID", "")})
    assert ok, msg


@pytest.mark.skipif(not os.getenv("GCHAT_LIVE"), reason="needs internet access to Google")
def test_gchat_signing_certificates_are_reachable():
    ok, msg = ChannelManager().test("gchat", {"auth": "project_number", "audience": "1"})
    assert ok, msg
