"""TV power switch for piSignage screens.

A screen gets a switch when its TV can actually be reached — either by the
player itself over HDMI-CEC, or through a ``media_player`` entity the user has
mapped to that screen in the integration's options. The two cover different
screens: a player that cannot drive its TV over CEC is often attached to a TV
Home Assistant already controls another way.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
)
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .api import PiSignageError
from .const import CONF_TV_MEDIA_PLAYERS, DOMAIN
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator
from .entity import PiSignageEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

#: Media player states that mean the TV is not showing anything.
_OFF_STATES = {STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN, "standby"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PiSignageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a TV switch for every screen whose TV can be reached.

    CEC support is read from the player on every poll rather than decided once,
    because a player probes its TV after booting and can start out reporting no
    CEC and correct itself later.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_controllable_players() -> None:
        mapped = entry.options.get(CONF_TV_MEDIA_PLAYERS) or {}
        new_ids = [
            player_id
            for player_id, player in coordinator.data.players.items()
            if player_id not in known
            and (player.get("isCecSupported") is True or mapped.get(player_id))
        ]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            PiSignageTvSwitch(coordinator, player_id, mapped.get(player_id))
            for player_id in new_ids
        )

    _async_add_controllable_players()
    entry.async_on_unload(
        coordinator.async_add_listener(_async_add_controllable_players)
    )


class PiSignageTvSwitch(PiSignageEntity, SwitchEntity):
    """Turns the TV attached to a screen on and off."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_translation_key = "tv"

    def __init__(
        self,
        coordinator: PiSignageCoordinator,
        player_id: str,
        media_player: str | None,
    ) -> None:
        """Initialise the switch, delegating to *media_player* when given."""
        super().__init__(coordinator, player_id, "tv")
        self._media_player = media_player
        # Holds the just-set value until the source of truth confirms it. The
        # player only reports every minute or so, and without this the switch
        # springs back and looks like the command failed.
        self._optimistic_is_on: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the mapped media player as well as the coordinator."""
        await super().async_added_to_hass()
        if self._media_player is None:
            return

        @callback
        def _async_media_player_changed(
            event: Event[EventStateChangedData],
        ) -> None:
            # The delegated TV reports far sooner than the next poll, so react
            # to it directly rather than waiting a whole cycle.
            self._optimistic_is_on = None
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._media_player], _async_media_player_changed
            )
        )

    @property
    def is_on(self) -> bool | None:
        """Whether the TV is on.

        With a mapped media player that entity is the truth, because piSignage
        knows nothing about a TV it is not driving. Otherwise ``tvStatus`` is
        the player's own view of TV power, which also moves when piSignage's
        schedule switches the screen off — so the switch tracks the TV however
        it was turned off.
        """
        if self._optimistic_is_on is not None:
            if self._polled_is_on() == self._optimistic_is_on:
                self._optimistic_is_on = None
            else:
                return self._optimistic_is_on
        return self._polled_is_on()

    def _polled_is_on(self) -> bool | None:
        if self._media_player is not None:
            state = self.hass.states.get(self._media_player)
            if state is None:
                return None
            return state.state not in _OFF_STATES

        player = self.player
        if player is None:
            return None
        status = player.get("tvStatus")
        return status if isinstance(status, bool) else None

    @property
    def available(self) -> bool:
        """A mapped media player that has gone missing makes this unusable."""
        if not super().available:
            return False
        if self._media_player is None:
            return True
        state = self.hass.states.get(self._media_player)
        return state is not None and state.state != STATE_UNAVAILABLE

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
            if self._media_player is not None:
                await self.hass.services.async_call(
                    MEDIA_PLAYER_DOMAIN,
                    SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: self._media_player},
                    blocking=True,
                )
            else:
                await self.coordinator.client.async_set_tv_power(self._player_id, on)
        except (PiSignageError, HomeAssistantError) as err:
            self._optimistic_is_on = None  # fall back to the real value
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_tv_failed",
                translation_placeholders={
                    "state": "on" if on else "off",
                    "error": str(err),
                },
            ) from err
        else:
            if on and self._media_player is None:
                # Switching off over CEC moves the player to the special TV_OFF
                # playlist, and switching back on does not move it off again, so
                # the screen returns lit but blank until a playlist is deployed.
                _LOGGER.debug(
                    "Switched the TV on for %s; if the screen stays on TV_OFF, "
                    "select its playlist again to bring the content back",
                    self._player_id,
                )
        finally:
            if self._media_player is None:
                await self.coordinator.async_request_refresh()
