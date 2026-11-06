"""The download of an entry's certificate authority, for a computer to trust."""

from __future__ import annotations

import re

from aiohttp import web
from homeassistant.components.http.decorators import require_admin
from homeassistant.const import CONF_HOST
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import CA_DOWNLOAD_URL, CONF_CA_CERT, DOMAIN


def download_name(host: str) -> str:
    """The file name offered: the host with dots, and anything else odd, as dashes."""
    return f"tuxedo-touch-ca-{re.sub(r'[^A-Za-z0-9-]', '-', host)}.crt"


class CertificateAuthorityView(HomeAssistantView):
    """Serve the certificate of an entry's own authority to an administrator.

    Authenticated as every API view is, a signed path included, and refused
    to anyone not an administrator. The certificate only: the key never
    leaves the entry. 404 for an entry that is not this integration's or has
    no authority.
    """

    url = CA_DOWNLOAD_URL
    name = f"api:{DOMAIN}:ca"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            return web.Response(status=404)
        authority = entry.data.get(CONF_CA_CERT)
        if not authority:
            return web.Response(status=404)
        return web.Response(
            body=authority.encode("ascii"),
            content_type="application/x-x509-ca-cert",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{download_name(entry.data[CONF_HOST])}"'
                )
            },
        )
