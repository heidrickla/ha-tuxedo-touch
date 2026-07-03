"""Constants for the Honeywell Tuxedo Touch integration."""
from datetime import timedelta

DOMAIN = "tuxedo_touch"

CONF_PARTITION = "partition"
CONF_USE_HTTPS = "use_https"
CONF_CODE = "code"

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

# Tuxedo panel status strings we know about, mapped in alarm_control_panel.py.
# See ../../../docs/tuxedo_touch_api_notes.md for the full list observed and
# where this list came from.
STATUS_NOT_AVAILABLE = "Not available"
STATUS_ERROR = "Error"
