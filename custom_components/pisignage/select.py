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
        # Holds the just-selected value until a poll confirms it. A deploy can
        # take a while to reach the screen, and without this the entity snaps
        # back to the old playlist and looks like the change failed.
        self._optimistic_option: str | None = None

    @property
    def options(self) -> list[str]:
        """Every playlist on the account.

        The playing playlist is folded in even if it is no longer on the server,
        so the current option is always a valid choice and HA does not warn
        about an out-of-range state.
        """
        options = set(self.coordinator.data.playlists)
        if (current := self.current_option) is not None:
            options.add(current)
        return sorted(options, key=str.casefold)

    def _current_playlist(self) -> str | None:
        player = self.player
        if player is None:
            return None
        current = player.get("currentPlaylist")
        return str(current) if current else None

    async def async_select_option(self, option: str) -> None:
        """Assign *option* to this screen persistently.

        piSignage assigns playlists to groups, not to individual screens, and
        a group plays its whole eligible set on rotation. So making a screen
        keep showing one playlist means making it that group's only playlist —
        which also removes the group's other playlists.
        """
        if option not in self.coordinator.data.playlists:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_playlist",
                translation_placeholders={"playlist": option},
            )

        # Show the new value straight away instead of waiting for the next
        # poll, otherwise a successful change looks like it did nothing.
        self._optimistic_option = option
        self.async_write_ha_state()

        try:
            group = await self.coordinator.async_get_player_group(self._player_id)
            removed = await self.coordinator.client.async_assign_playlist(
                self._player_id, group, option
            )
        except PiSignageError as err:
            self._optimistic_option = None  # fall back to the polled value
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_playlist_failed",
                translation_placeholders={"playlist": option, "error": str(err)},
            ) from err
        else:
            # The deploy has started the screen downloading; the coordinator
            # keeps nudging it until it actually switches, so a slow download no
            # longer leaves the screen stuck on the old playlist.
            self.coordinator.async_track_assignment(self._player_id, option)
            if removed:
                _LOGGER.warning(
                    "Assigned '%s' to group '%s', removing %s from it. Every "
                    "player in that group now shows '%s'. Re-add the others in "
                    "piSignage if they were scheduled deliberately",
                    option,
                    group.get("name") or group.get("_id"),
                    ", ".join(repr(name) for name in removed),
                    option,
                )
        finally:
            await self.coordinator.async_request_refresh()

    @property
    def current_option(self) -> str | None:
        """The playlist this screen is playing, or is about to.

        The optimistic value stands only until a poll reports the same thing,
        at which point the real reading takes over again.
        """
        polled = self._current_playlist()
        if self._optimistic_option is not None:
            if polled == self._optimistic_option:
                self._optimistic_option = None
            else:
                return self._optimistic_option
        return polled
