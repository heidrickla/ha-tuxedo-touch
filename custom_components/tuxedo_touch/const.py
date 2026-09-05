"""Constants for the Honeywell Tuxedo Touch integration."""

from datetime import timedelta

DOMAIN = "tuxedo_touch"

CONF_MAC = "mac"
CONF_PARTITION = "partition"
CONF_USE_HTTPS = "use_https"

# Repair issue translation keys. The issue id appends the entry id so two
# entries can be in the same state without sharing one issue.
ISSUE_HTTPS_REDIRECT = "https_redirect"
ISSUE_DUPLICATE_ENTRY = "duplicate_entry"

DEFAULT_PARTITION = 1
DEFAULT_PORT_HTTP = 80
DEFAULT_PORT_HTTPS = 443
# Even when the login/tuxedoapi.html pages tolerate plain HTTP, the actual
# /system_http_api/API_REV01/... command endpoints get 302-redirected to
# HTTPS whenever "Secured Web Server Access (HTTPS)" is enabled on the unit
# (the common/default state) - so default to HTTPS here even though the
# sibling Hubitat driver in heidrickla/Hubitat defaults to HTTP.
DEFAULT_USE_HTTPS = True

API_REV = "API_REV01"
API_BASE_PATH = f"/system_http_api/{API_REV}"
LOGIN_PATH = "/authenticated/index.html"
KEYS_PATH = "/tuxedoapi.html"

SCAN_INTERVAL = timedelta(seconds=30)


def issue_id(key: str, entry_id: str) -> str:
    """A repair issue id: the condition, then the entry it is about."""
    return f"{key}_{entry_id}"


# Tuxedo panel status strings we know about, mapped in alarm_control_panel.py.
# See ../../../docs/tuxedo_touch_api_notes.md for the full list observed and
# where this list came from.
STATUS_NOT_AVAILABLE = "Not available"
