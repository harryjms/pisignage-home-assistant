"""TV power switch for piSignage screens whose player supports HDMI-CEC."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PiSignageError
from .const import DOMAIN
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator
from .entity import PiSignageEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PiSignageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a TV switch for every screen that reports CEC support.

    Only CEC-capable players are given one: piSignage accepts the command for
    any player and reports success, but a TV that cannot be reached over CEC
    simply ignores it, and a switch that silently does nothing is worse than no
    switch at all.

    Support is read from the player rather than decided once at setup, because
    players report it after they have probed the TV — a screen can start up
    saying ``isCecSupported: false`` and correct itself later.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_cec_players() -> None:
        new_ids = [
            player_id
            for player_id, player in coordinator.data.players.items()
            if player_id not in known and player.get("isCecSupported") is True
        ]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            PiSignageTvSwitch(coordinator, player_id) for player_id in new_ids
        )

    _async_add_cec_players()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_cec_players))


class PiSignageTvSwitch(PiSignageEntity, SwitchEntity):
    """Turns the TV attached to a screen on and off."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_translation_key = "tv"

    def __init__(self, coordinator: PiSignageCoordinator, player_id: str) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, player_id, "tv")
        # Holds the just-set value until a poll confirms it. The player only
        # reports every minute or so, and without this the switch springs back
        # and looks like the command failed.
        self._optimistic_is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """Whether the TV is on.

        ``tvStatus`` is the player's own view of TV power, which also moves when
        piSignage's schedule switches the screen off, so the switch tracks the
        TV however it was turned off.
        """
        player = self.player
        if player is None:
            return None

        status = player.get("tvStatus")
        polled = status if isinstance(status, bool) else None

        if self._optimistic_is_on is not None:
            if polled == self._optimistic_is_on:
                self._optimistic_is_on = None
            else:
                return self._optimistic_is_on
        return polled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the TV on."""
        await self._async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the TV off."""
        await self._async_set_power(False)

    async def _async_set_power(self, on: bool) -> None:
        self._optimistic_is_on = on
        self.async_write_ha_state()

        try:
            await self.coordinator.client.async_set_tv_power(self._player_id, on)
        except PiSignageError as err:
            self._optimistic_is_on = None  # fall back to the polled value
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_tv_failed",
                translation_placeholders={
                    "state": "on" if on else "off",
                    "error": str(err),
                },
            ) from err
        else:
            if on:
                # Switching off moves the player to the special TV_OFF playlist,
                # and switching back on does not move it off again, so the screen
                # comes back powered but blank until a playlist is deployed.
                _LOGGER.debug(
                    "Switched the TV on for %s; if the screen stays on TV_OFF, "
                    "select its playlist again to bring the content back",
                    self._player_id,
                )
        finally:
            await self.coordinator.async_request_refresh()
