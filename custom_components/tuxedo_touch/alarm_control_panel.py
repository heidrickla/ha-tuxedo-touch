"""Alarm control panel platform for Honeywell Tuxedo Touch."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.const import CONF_CODE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import TuxedoStatus, TuxedoTouchError
from .const import COUNTDOWN_RE, DOMAIN, STATUS_STATES
from .coordinator import TuxedoTouchConfigEntry, TuxedoTouchCoordinator
from .entity import TuxedoTouchEntity

# The panel is a fragile embedded web server with per-session crypto state;
# serialize entity service calls so concurrent arm/disarm from automations
# can't interleave against it (coordinator polling is already centralized,
# and the API client additionally locks its login sequence).
PARALLEL_UPDATES = 1

# The status strings the panel reports, as alarm states. One map serves both
# sources. "Ready To Arm" and the exit-delay countdown ("59  Secs Remaining")
# are the texts seen on the push stream; the armed spellings are the ones
# GetSecurityStatus returns, and the stream is assumed - not observed - to
# spell them the same way. If it does not, a streamed text outside this map
# settles nothing and the poll is let through to name the mode, so the
# assumption being wrong costs granularity rather than correctness.
# const.STATUS_STATES holds it as plain text so the coordinator can ask
# whether a text names a state without importing a platform module; this is
# the same table with Home Assistant's enum in place of those strings.
# The countdown is matched separately (COUNTDOWN_RE); anything else
# unrecognized falls back to None (unknown) rather than guessing.
# "Not available" is deliberately absent and never arrives here: it is the
# REST cache's placeholder, the stream cannot produce it, and the coordinator
# fails the poll on it rather than rendering it as a state.
STATUS_MAP: dict[str, AlarmControlPanelState] = {
    text: AlarmControlPanelState(state) for text, state in STATUS_STATES.items()
}


def reports_armed(status: TuxedoStatus) -> bool | None:
    """Whether a reported status means the partition is armed, or arming.

    The push stream says so outright: its 0xFE/0xFF flag is the panel's own
    answer, independent of the display text. A poll carries display text and
    nothing else, so the map above decides - and a text neither the map nor
    the countdown knows settles nothing, which is None rather than False.

    Used to decide whether what the panel just reported is the command that
    was sent taking effect; see TuxedoTouchCoordinator.async_send_command.
    """
    if status.armed is not None:
        return status.armed
    state = STATUS_MAP.get(status.status)
    if state is not None:
        return state is not AlarmControlPanelState.DISARMED
    if COUNTDOWN_RE.match(status.status.strip()):
        return True
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuxedoTouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TuxedoAlarmPanel(entry.runtime_data, entry)])


class TuxedoAlarmPanel(TuxedoTouchEntity, AlarmControlPanelEntity):
    """Represents one Tuxedo Touch partition."""

    # Named after the partition, not the device: two partition entries on one
    # panel are two devices with the same name, and the entities would
    # otherwise be identical rows apart from a numeric suffix.
    _attr_translation_key = "partition"
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(
        self, coordinator: TuxedoTouchCoordinator, entry: TuxedoTouchConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        # No partition suffix: the entry's OWN unique id already carries the
        # partition, and a partition change is a reconfigure of the same entry
        # - a suffix here orphaned the registry row on every such change.
        self._attr_unique_id = entry.entry_id
        self._attr_translation_placeholders = {"partition": str(coordinator.partition)}
        # A code is required unless one is stored in config for automations
        # to use without prompting. code_format follows the same logic: with a
        # stored code the dashboard must not demand one for disarm either.
        stored_code = bool(entry.data.get(CONF_CODE))
        self._attr_code_arm_required = not stored_code
        self._attr_code_format = None if stored_code else CodeFormat.NUMBER

    @property
    def available(self) -> bool:
        """Available while either source is working.

        Not the CoordinatorEntity default, which follows the poll alone: the
        stream is the primary source here, and a poll answering the firmware's
        "Not available" placeholder while the stream is delivering real
        statuses is not an outage of anything.

        And unavailable whatever either source is doing once the Tuxedo has
        said it cannot see the VISTA panel: both go on answering, and neither
        is reading the alarm. See TuxedoTouchCoordinator.panel_available.
        """
        return self.coordinator.panel_available

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status = self.coordinator.data
        if status is None:
            return None
        if (state := STATUS_MAP.get(status.status)) is not None:
            return state
        if COUNTDOWN_RE.match(status.status.strip()):
            return AlarmControlPanelState.ARMING
        # A text neither map nor countdown knows. The stream's flag says
        # whether the partition is armed but not in which mode, and naming a
        # mode the panel did not would be a guess; the next status settles it,
        # and the poll's own reading is what disambiguates in the meantime.
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.data
        if status is None:
            return {}
        return {
            "tuxedo_status": status.status,
            "tuxedo_color": status.color,
            # Which source reported it: "stream" (the panel pushed it),
            # "poll" (GetSecurityStatus), "assumed" (neither could report
            # the command that was just sent) or "command" (neither could,
            # but the panel confirmed it in the reply - tuxweb only).
            "tuxedo_source": status.source,
            # Seconds left of the exit delay, straight from the countdown the
            # panel pushes once a second; None whenever it is not counting.
            "arming_seconds_remaining": status.seconds_remaining,
        }

    def _resolve_code(self, code: str | None) -> str:
        resolved = code or self._entry.data.get(CONF_CODE)
        if not resolved:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_code"
            )
        return resolved

    async def _async_command(
        self,
        command: Coroutine[Any, Any, dict[str, Any]],
        expect_armed: bool,
        assumed_status: str,
    ) -> None:
        """Run one panel command and wait for the panel to report the change.

        Arm and disarm answer with a zero-byte body; what they did shows up
        on the push stream. `expect_armed` is what to watch for there - the
        stream's own armed flag, which needs no display text to be read -
        and the coordinator falls back to a poll, then to the assumed status,
        if the panel never reports it. On tuxweb the reply itself says
        whether the panel acted, and a command it did not act on fails here
        with the reason rather than leaving anything assumed.
        """

        def confirms(status: TuxedoStatus) -> bool | None:
            """Three answers, because the panel gives three.

            True the panel reports what was asked for, False it reports the
            opposite, None the reading settles nothing. `reports_armed`
            already computes exactly that; collapsing it with `is
            expect_armed` made a refusal and a silence the same answer, and
            the coordinator then wrote the requested status over a poll that
            had just reported the partition in the other state.
            """
            armed = reports_armed(status)
            return None if armed is None else armed is expect_armed

        try:
            await self.coordinator.async_send_command(command, confirms, assumed_status)
        except TuxedoTouchError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_arm(
        self, mode: str, assumed_status: str, code: str | None
    ) -> None:
        await self._async_command(
            self.coordinator.client.arm(
                mode, self._resolve_code(code), self.coordinator.partition
            ),
            True,
            assumed_status,
        )

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._async_command(
            self.coordinator.client.disarm(
                self._resolve_code(code), self.coordinator.partition
            ),
            False,
            "Ready To Arm",
        )

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._async_arm("STAY", "Armed Stay", code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._async_arm("AWAY", "Armed Away", code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._async_arm("NIGHT", "Armed Night", code)
