"""Read-only sensors for each piSignage screen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import normalise_epoch
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator
from .entity import PiSignageEntity, async_setup_player_entities

PARALLEL_UPDATES = 0


def _last_seen(player: dict[str, Any]) -> datetime | None:
    timestamp = normalise_epoch(player.get("lastReported"))
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


@dataclass(frozen=True, kw_only=True)
class PiSignageSensorDescription(SensorEntityDescription):
    """Describes a piSignage sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[PiSignageSensorDescription, ...] = (
    PiSignageSensorDescription(
        key="current_playlist",
        translation_key="current_playlist",
        value_fn=lambda player: player.get("currentPlaylist") or None,
    ),
    PiSignageSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=_last_seen,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PiSignageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors for every player."""
    async_setup_player_entities(
        entry,
        async_add_entities,
        lambda coordinator, player_id: [
            PiSignageSensor(coordinator, player_id, description)
            for description in SENSORS
        ],
    )


class PiSignageSensor(PiSignageEntity, SensorEntity):
    """A single read-only value from a player."""

    entity_description: PiSignageSensorDescription

    def __init__(
        self,
        coordinator: PiSignageCoordinator,
        player_id: str,
        description: PiSignageSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, player_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """The current value, or None when the player is gone."""
        if (player := self.player) is None:
            return None
        return self.entity_description.value_fn(player)
