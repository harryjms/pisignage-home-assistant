"""Playlist picker for each piSignage screen."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PiSignageError
from .const import DOMAIN
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator
from .entity import PiSignageEntity, async_setup_player_entities

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PiSignageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one playlist selector per player."""
    async_setup_player_entities(
        entry,
        async_add_entities,
        lambda coordinator, player_id: [
            PiSignagePlaylistSelect(coordinator, player_id)
        ],
    )


class PiSignagePlaylistSelect(PiSignageEntity, SelectEntity):
    """Reads and sets the playlist a screen is playing."""

    _attr_translation_key = "playlist"

    def __init__(self, coordinator: PiSignageCoordinator, player_id: str) -> None:
        """Initialise the selector."""
        super().__init__(coordinator, player_id, "playlist")

    @property
    def options(self) -> list[str]:
        """Every playlist on the account.

        The playing playlist is folded in even if it is no longer on the server,
        so the current option is always a valid choice and HA does not warn
        about an out-of-range state.
        """
        options = set(self.coordinator.data.playlists)
        if (current := self._current_playlist()) is not None:
            options.add(current)
        return sorted(options, key=str.casefold)

    @property
    def current_option(self) -> str | None:
        """The playlist currently playing on this screen."""
        return self._current_playlist()

    def _current_playlist(self) -> str | None:
        player = self.player
        if player is None:
            return None
        current = player.get("currentPlaylist")
        return str(current) if current else None

    async def async_select_option(self, option: str) -> None:
        """Make this screen play *option*.

        Fast path: the playlist is already deployed to the screen's group, so a
        single ``setplaylist`` call switches it with no other side effects.

        Slow path: the playlist is not on the group yet, so it is attached and
        deployed first. That makes **every** player in the group re-sync, which
        is why it is logged loudly.
        """
        if option not in self.coordinator.data.playlists:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_playlist",
                translation_placeholders={"playlist": option},
            )

        player_name = (self.player or {}).get("name", self._player_id)

        try:
            group = await self.coordinator.async_get_player_group(self._player_id)
            deployed = await self.coordinator.client.async_activate_playlist(
                self._player_id, group, option
            )
        except PiSignageError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_playlist_failed",
                translation_placeholders={"playlist": option, "error": str(err)},
            ) from err
        else:
            if deployed:
                _LOGGER.warning(
                    "Deployed playlist '%s' to group '%s' so that '%s' could play it. "
                    "Every player in that group has re-synced and may have changed "
                    "what it is showing",
                    option,
                    group.get("name") or group.get("_id"),
                    player_name,
                )
        finally:
            # Re-read rather than assuming the write landed; a deploy can take a
            # while to reach the player.
            await self.coordinator.async_request_refresh()
