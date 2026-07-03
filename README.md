# HomeAssistant

Custom Home Assistant integrations.

## [tuxedo_touch](custom_components/tuxedo_touch)

Local (no cloud) integration for the Honeywell Tuxedo Touch WIFI security controller.
Provides an `alarm_control_panel` entity (arm home/away/night, disarm) by talking directly
to the panel's local web API - the same reverse-engineered login/encryption flow documented
in [docs/tuxedo_touch_api_notes.md](docs/tuxedo_touch_api_notes.md).

Ported from the [heidrickla/Hubitat](https://github.com/heidrickla/Hubitat) driver of the
same name after migrating off Hubitat to Home Assistant.
