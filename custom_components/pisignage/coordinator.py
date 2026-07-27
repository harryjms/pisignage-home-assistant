"""Polling coordinator for the piSignage integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PiSignageAuthError, PiSignageClient, PiSignageError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type PiSignageConfigEntry = ConfigEntry[PiSignageCoordinator]


@dataclass(slots=True)
class PiSignageData:
    """One snapshot of the account, shared by every entity."""

    players: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    playlists: list[str] = field(default_factory=list)


class PiSignageCoordinator(DataUpdateCoordinator[PiSignageData]):
    """Fetch the whole account once per cycle.

    Players, groups and playlists are fetched together because every entity
    needs some slice of them, and the hosted API would rather serve three
    requests a minute than three per screen.
    """

    config_entry: PiSignageConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PiSignageConfigEntry,
        client: PiSignageClient,
    ) -> None:
        """Initialise the coordinator."""
        scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client

    async def _async_update_data(self) -> PiSignageData:
        """Fetch players, groups and playlist names."""
        try:
            players = await self.client.async_get_players()
            groups = await self.client.async_get_groups()
            playlists = await self.client.async_get_playlist_names()
        except PiSignageAuthError as err:
            # Surfaces as a reauth prompt rather than a silent failure loop.
            raise ConfigEntryAuthFailed(str(err)) from err
        except PiSignageError as err:
            raise UpdateFailed(f"Error talking to piSignage: {err}") from err

        return PiSignageData(
            players={
                str(player["_id"]): player for player in players if player.get("_id")
            },
            groups={str(group["_id"]): group for group in groups if group.get("_id")},
            playlists=playlists,
        )

    def group_id_for_player(self, player_id: str) -> str | None:
        """Resolve which group a player belongs to.

        A player with no explicit group still has a private one, referenced by
        ``selfGroupId``. Playlists deploy to that group like any other.
        """
        player = self.data.players.get(player_id)
        if player is None:
            return None

        group = player.get("group")
        group_id: Any = None
        if isinstance(group, dict):
            group_id = group.get("_id")
        elif isinstance(group, str):
            group_id = group

        if not group_id:
            group_id = player.get("selfGroupId")

        return str(group_id) if group_id else None

    async def async_get_player_group(self, player_id: str) -> dict[str, Any]:
        """Return the group object a player belongs to.

        Falls back to fetching the group directly, because the bulk listing does
        not always include every per-player pseudo-group.
        """
        group_id = self.group_id_for_player(player_id)
        if not group_id:
            raise PiSignageError(
                f"Player {player_id} is not attached to any group, so a playlist "
                "cannot be deployed to it"
            )

        if (group := self.data.groups.get(group_id)) is not None:
            return group
        return await self.client.async_get_group(group_id)
