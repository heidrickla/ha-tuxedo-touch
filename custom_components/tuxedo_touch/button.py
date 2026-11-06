"""Button platform for Honeywell Tuxedo Touch: renew the panel's certificate."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .certificate import CertificateRenewalFailed
from .const import DOMAIN
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator
from .entity import TuxedoTouchEntity

# One install at a time; the coordinator's lock holds a second press anyway.
PARALLEL_UPDATES = 1

RENEW_CERTIFICATE = "renew_certificate"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuxedoTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Only for a tuxweb panel over HTTPS that declares certificate_install.

    Anywhere else there is no button, and one left in the registry from
    when the panel did declare it is removed rather than shown unavailable.
    """
    coordinator = entry.runtime_data
    unique_id = f"{entry.entry_id}_{RENEW_CERTIFICATE}"
    if not coordinator.client.certificate_install:
        registry = er.async_get(hass)
        if entity_id := registry.async_get_entity_id(
            Platform.BUTTON, DOMAIN, unique_id
        ):
            registry.async_remove(entity_id)
        return
    async_add_entities([TuxedoRenewCertificate(coordinator, entry, unique_id)])


class TuxedoRenewCertificate(TuxedoTouchEntity, ButtonEntity):
    """Issue the panel a new certificate from the entry's authority and install it.

    Renews the panel's own TLS certificate and nothing else. A failure names
    the reason and leaves the entry as it was.
    """

    _attr_translation_key = "renew_certificate"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TuxedoTouchCoordinator,
        entry: TuxedoTouchConfigEntry,
        unique_id: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = unique_id

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_renew_certificate()
        except CertificateRenewalFailed as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="certificate_renewal_failed",
                translation_placeholders={"reason": err.reason},
            ) from err
