"""Connectivity sensor for each piSignage screen."""

from __future__ import annotations

import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import OFFLINE_AFTER_SECONDS, normalise_epoch
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator
from .entity import PiSignageEntity, async_setup_player_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PiSignageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one connectivity sensor per player."""
    async_setup_player_entities(
        entry,
        async_add_entities,
        lambda coordinator, player_id: [PiSignageOnlineSensor(coordinator, player_id)],
    )


class PiSignageOnlineSensor(PiSignageEntity, BinarySensorEntity):
    """Whether a screen has checked in recently."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator: PiSignageCoordinator, player_id: str) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, player_id, "online")

    @property
    def is_on(self) -> bool | None:
        """True when the last check-in is recent enough.

        piSignage has no explicit online flag — liveness is inferred from how
        long ago the player last reported in.
        """
        player = self.player
        if player is None:
            return None

        last_reported = normalise_epoch(player.get("lastReported"))
        if last_reported is None:
            return False
        return (time.time() - last_reported) < OFFLINE_AFTER_SECONDS
