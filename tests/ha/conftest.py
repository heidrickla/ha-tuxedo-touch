"""Fixtures for the Home Assistant layer tests.

These need Home Assistant, which needs Python 3.14; given that, they run
anywhere, Windows included. The conftest lives in its own directory so its
autouse fixture does not attach itself to the pure-logic suite one level up,
which loads api.py by path and needs no Home Assistant.
"""

import asyncio
from unittest.mock import patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import (
    CONF_CODE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuxedo_touch.api import TuxedoStatus, TuxedoTouchError
from custom_components.tuxedo_touch.const import (
    CONF_MAC,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DOMAIN,
)
from tests.fake_panel import FakePanel

HOST = "10.10.52.60"
PORT = 443
MAC = "aa:bb:cc:dd:ee:ff"

ENTRY_DATA = {
    CONF_HOST: HOST,
    CONF_PORT: PORT,
    CONF_USE_HTTPS: True,
    CONF_USERNAME: "installer",
    CONF_PASSWORD: "secret",
    CONF_CODE: "1234",
    CONF_PARTITION: 1,
}


STATUS = "custom_components.tuxedo_touch.api.TuxedoTouchClient.get_status"
PUSH_RUN = "custom_components.tuxedo_touch.push.TuxedoPushStream.async_run"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Required for Home Assistant to load a custom component in tests."""
    return


async def _idle_stream(self):
    """A stream task that connects to nothing and waits to be cancelled."""
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def no_panel_behind_the_fixtures(request):
    """Nothing in these tests may dial the address in ENTRY_DATA.

    Setting an entry up starts two things that talk to a panel: the poll and
    the push stream. Every test that wants either patches it; this is what is
    underneath, so a path nobody patched fails here rather than opening a
    socket to whatever is at that address on the machine running the suite.

    A test that brings its own `fake_panel` is opted out - it has a real
    server on 127.0.0.1 and wants the real client to reach it.
    """
    if "fake_panel" in request.fixturenames:
        yield
        return
    with (
        patch(STATUS, side_effect=TuxedoTouchError("no panel in this test")),
        patch(PUSH_RUN, _idle_stream),
    ):
        yield


@pytest.fixture
async def fake_panel(socket_enabled):
    """A panel on 127.0.0.1 speaking the real wire format. See fake_panel.py.

    socket_enabled because pytest-homeassistant-custom-component blocks
    socket creation for every test; the connect() guard that stays in place
    alongside it allows only 127.0.0.1, which is where this panel listens.
    """
    panel = FakePanel()
    await panel.start()
    yield panel
    await panel.close()


@pytest.fixture
def panel_entry(fake_panel):
    """An entry pointing at the fake panel, plain HTTP."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tuxedo Touch (127.0.0.1)",
        unique_id=f"127.0.0.1:{fake_panel.port}:1",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: fake_panel.port,
            CONF_USE_HTTPS: False,
            CONF_USERNAME: fake_panel.username,
            CONF_PASSWORD: fake_panel.password,
            CONF_CODE: "1234",
            CONF_PARTITION: 1,
        },
    )


@pytest.fixture
def config_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{HOST}:{PORT}:1",
        data=dict(ENTRY_DATA),
    )


@pytest.fixture
def ready():
    """A panel reporting the disarmed-and-ready status."""
    return TuxedoStatus(status="Ready To Arm", color="green")


@pytest.fixture
def config_entry_no_code():
    """An entry with no stored code, so the user is prompted to type one."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{HOST}:{PORT}:1",
        data={k: v for k, v in ENTRY_DATA.items() if k != CONF_CODE},
    )


@pytest.fixture
def config_entry_with_mac():
    """An entry whose identity is the panel's MAC, as a discovered one has."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Tuxedo Touch ({HOST})",
        unique_id=f"{MAC}_1",
        data={**ENTRY_DATA, CONF_MAC: MAC},
    )
