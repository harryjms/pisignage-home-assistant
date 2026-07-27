"""Polling coordinator for the piSignage integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    OFFLINE_AFTER_SECONDS,
    PiSignageAuthError,
    PiSignageClient,
    PiSignageError,
    normalise_epoch,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type PiSignageConfigEntry = ConfigEntry[PiSignageCoordinator]

#: How many poll cycles to keep nudging a screen onto its selected playlist
#: before giving up. A screen only switches once it has finished downloading the
#: content, which can take minutes for large videos, so the window is generous.
#: The budget is only spent on cycles where a nudge is actually attempted — a
#: screen that is offline or still downloading waits without using it up.
PENDING_ASSIGNMENT_MAX_POLLS: Final = 20


def _player_is_online(player: dict[str, Any]) -> bool:
    """Whether *player* has checked in recently enough to act on.

    Mirrors the connectivity binary sensor: trust the explicit ``isConnected``
    flag when present, and fall back to the check-in age for older players that
    do not report it.
    """
    connected = player.get("isConnected")
    if isinstance(connected, bool):
        return connected

    last_reported = normalise_epoch(player.get("lastReported"))
    if last_reported is None:
        return False
    return (time.time() - last_reported) < OFFLINE_AFTER_SECONDS


@dataclass(slots=True)
class PiSignageData:
    """One snapshot of the account, shared by every entity."""

    players: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    playlists: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _PendingAssignment:
    """A screen that has been told to show a playlist but has not switched yet."""

    playlist: str
    polls_remaining: int


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
        # Screens that have been sent a playlist but have not switched to it yet,
        # keyed by player id. The poll loop keeps nudging them until they do.
        self._pending: dict[str, _PendingAssignment] = {}

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

        data = PiSignageData(
            players={
                str(player["_id"]): player for player in players if player.get("_id")
            },
            groups={str(group["_id"]): group for group in groups if group.get("_id")},
            playlists=playlists,
        )

        if self._pending:
            try:
                await self._async_reconcile_pending(data)
            except PiSignageAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err

        return data

    @callback
    def async_track_assignment(self, player_id: str, playlist: str) -> None:
        """Remember that *player_id* should be showing *playlist*.

        A selection deploys the playlist, which makes the player download it, but
        the screen keeps showing the old playlist until the download finishes and
        a fresh deploy flips it over. The poll loop watches this list and does
        that follow-up, so a selection completes on its own instead of needing a
        manual Deploy in the console.
        """
        self._pending[player_id] = _PendingAssignment(
            playlist, PENDING_ASSIGNMENT_MAX_POLLS
        )

    async def _async_reconcile_pending(self, data: PiSignageData) -> None:
        """Nudge any screen that has not yet switched to its selected playlist."""
        for player_id in list(self._pending):
            pending = self._pending[player_id]
            player = data.players.get(player_id)
            if player is None or not _player_is_online(player):
                # Not visible or not reachable this cycle — keep the budget and
                # try again once it comes back.
                continue

            if player.get("currentPlaylist") == pending.playlist:
                del self._pending[player_id]  # the screen caught up
                continue

            if player.get("syncInProgress") is True:
                # Still downloading; a re-deploy now would only disturb it. Wait
                # without spending the budget.
                continue

            pending.polls_remaining -= 1
            if pending.polls_remaining < 0:
                del self._pending[player_id]
                _LOGGER.warning(
                    "Screen '%s' never switched to '%s' after repeated deploys; "
                    "giving up. Check the playlist deployed in the piSignage "
                    "console",
                    player.get("name") or player_id,
                    pending.playlist,
                )
                continue

            group = await self._async_resolve_group(player_id, data)
            if group is None:
                continue

            try:
                await self.client.async_redeploy_playlist(
                    player_id, group, pending.playlist
                )
            except PiSignageAuthError:
                raise
            except PiSignageError as err:
                _LOGGER.debug(
                    "Could not nudge screen '%s' onto '%s'; will retry next cycle: %s",
                    player.get("name") or player_id,
                    pending.playlist,
                    err,
                )

    @staticmethod
    def _group_id_from_players(
        players: dict[str, dict[str, Any]], player_id: str
    ) -> str | None:
        """Resolve which group a player belongs to, from a players snapshot.

        A player with no explicit group still has a private one, referenced by
        ``selfGroupId``. Playlists deploy to that group like any other.
        """
        player = players.get(player_id)
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

    def group_id_for_player(self, player_id: str) -> str | None:
        """Resolve which group a player belongs to, from the latest snapshot."""
        return self._group_id_from_players(self.data.players, player_id)

    async def _async_resolve_group(
        self, player_id: str, data: PiSignageData
    ) -> dict[str, Any] | None:
        """Return the group for *player_id* against *data*, or ``None``.

        Used by the reconcile loop, which works off the snapshot it has just
        fetched rather than the coordinator's published data. Falls back to a
        direct fetch for per-player pseudo-groups missing from the bulk listing,
        and swallows errors because a nudge must never break the poll.
        """
        group_id = self._group_id_from_players(data.players, player_id)
        if not group_id:
            return None
        if (group := data.groups.get(group_id)) is not None:
            return group
        try:
            return await self.client.async_get_group(group_id)
        except PiSignageError:
            return None

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
