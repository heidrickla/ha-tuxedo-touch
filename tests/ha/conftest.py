"""Fixtures for the Home Assistant layer tests.

These need Home Assistant, which needs Python 3.14; given that, they run
anywhere, Windows included. The conftest lives in its own directory so its
autouse fixture does not attach itself to the pure-logic suite one level up,
which loads api.py by path and needs no Home Assistant.
"""

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

from custom_components.tuxedo_touch.api import TuxedoStatus
from custom_components.tuxedo_touch.const import (
    CONF_MAC,
    CONF_PARTITION,
    CONF_USE_HTTPS,
    DOMAIN,
)

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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Required for Home Assistant to load a custom component in tests."""
    return


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
