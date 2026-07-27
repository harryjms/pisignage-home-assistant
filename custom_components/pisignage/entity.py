"""Shared entity plumbing for the piSignage integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator


class PiSignageEntity(CoordinatorEntity[PiSignageCoordinator]):
    """Base entity bound to one piSignage player."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PiSignageCoordinator, player_id: str, key: str
    ) -> None:
        """Initialise the entity for *player_id*."""
        super().__init__(coordinator)
        self._player_id = player_id
        # The player id is stable and not user-editable, unlike the name.
        self._attr_unique_id = f"{player_id}_{key}"

    @property
    def player(self) -> dict[str, Any] | None:
        """The current snapshot of this player, if it still exists."""
        return self.coordinator.data.players.get(self._player_id)

    @property
    def available(self) -> bool:
        """Whether the player is still present in the account."""
        return super().available and self.player is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the player as an HA device.

        Built fresh each time so a renamed player or an updated player build
        shows up without needing a reload.
        """
        player = self.player or {}
        # The web UI lives at the same host as the API, minus the /api suffix.
        console = self.coordinator.client.base_url.removesuffix("/api")
        return DeviceInfo(
            identifiers={(DOMAIN, self._player_id)},
            name=player.get("name") or self._player_id,
            manufacturer=MANUFACTURER,
            model="piSignage Player",
            sw_version=player.get("version"),
            hw_version=player.get("platform_version"),
            serial_number=player.get("cpuSerialNumber"),
            configuration_url=console,
        )


@callback
def async_setup_player_entities(
    entry: PiSignageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[PiSignageCoordinator, str], Iterable[Entity]],
) -> None:
    """Add entities for every player, including ones that appear later.

    piSignage players are provisioned outside Home Assistant, so the platform
    listens to the coordinator rather than assuming the fleet is fixed at setup.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_players() -> None:
        new_ids = set(coordinator.data.players) - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            entity
            for player_id in new_ids
            for entity in factory(coordinator, player_id)
        )

    _async_add_new_players()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_players))
