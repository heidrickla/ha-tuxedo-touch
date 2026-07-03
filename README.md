# Honeywell Tuxedo Touch for Home Assistant

Local (no cloud) Home Assistant integration for the Honeywell Tuxedo Touch WIFI security
controller. Provides an `alarm_control_panel` entity (arm home/away/night, disarm) by
talking directly to the panel's local web API - the same reverse-engineered
login/encryption flow documented in [docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md).

See [custom_components/tuxedo_touch](custom_components/tuxedo_touch) for installation and
usage.

Ported from the [heidrickla/Hubitat](https://github.com/heidrickla/Hubitat) driver of the
same name after migrating off Hubitat to Home Assistant.
