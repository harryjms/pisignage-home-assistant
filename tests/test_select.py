"""Tests for the playlist selector and the read-only entities."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pisignage import coordinator as coordinator_module
from custom_components.pisignage.api import PiSignageError

from .conftest import iso_now, make_group, make_player

SELECT_ENTITY = "select.lobby_screen_playlist"
SENSOR_ENTITY = "sensor.lobby_screen_current_playlist"
ONLINE_ENTITY = "binary_sensor.lobby_screen_online"


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SELECT_ENTITY, ATTR_OPTION: option},
        blocking=True,
    )


async def test_select_reports_current_playlist(
    hass: HomeAssistant, init_integration
) -> None:
    state = hass.states.get(SELECT_ENTITY)

    assert state.state == "Promos"
    assert state.attributes[ATTR_OPTIONS] == ["NewYearSale", "Promos"]


async def test_selecting_playlist_calls_client(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    await _select(hass, "NewYearSale")

    mock_client.async_assign_playlist.assert_awaited_once()
    player_id, group, playlist = mock_client.async_assign_playlist.await_args.args
    assert player_id == "player1"
    assert group["_id"] == "group1"
    assert playlist == "NewYearSale"


async def test_selecting_unknown_playlist_is_rejected(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    with pytest.raises(ServiceValidationError):
        await _select(hass, "DoesNotExist")

    mock_client.async_assign_playlist.assert_not_awaited()


async def test_client_error_surfaces_to_the_user(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    mock_client.async_assign_playlist.side_effect = PiSignageError("still syncing")

    with pytest.raises(HomeAssistantError):
        await _select(hass, "NewYearSale")


async def test_removed_playlists_are_warned_about(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping a group's other playlists is destructive and must be named."""
    mock_client.async_assign_playlist.return_value = ["Promos", "Gaydio Gold"]

    await _select(hass, "NewYearSale")

    assert "'Promos'" in caplog.text
    assert "'Gaydio Gold'" in caplog.text
    assert "Stores" in caplog.text


async def test_no_warning_when_nothing_was_removed(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_client.async_assign_playlist.return_value = []

    await _select(hass, "NewYearSale")

    assert "removing" not in caplog.text


async def test_selection_shows_immediately(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    """A deploy takes time to reach the screen.

    Without an optimistic value the entity snaps back to the old playlist and
    a successful change looks like it did nothing.
    """
    assert hass.states.get(SELECT_ENTITY).state == "Promos"

    await _select(hass, "NewYearSale")

    assert hass.states.get(SELECT_ENTITY).state == "NewYearSale"


async def test_polled_value_takes_over_once_it_agrees(
    hass: HomeAssistant, init_integration, mock_client, freezer
) -> None:
    await _select(hass, "NewYearSale")
    assert hass.states.get(SELECT_ENTITY).state == "NewYearSale"

    # The screen catches up, and afterwards the entity tracks reality again.
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="NewYearSale")
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="SomethingElse")
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(SELECT_ENTITY).state == "SomethingElse"


async def test_selection_is_nudged_until_the_screen_switches(
    hass: HomeAssistant, init_integration, mock_client, freezer
) -> None:
    """A screen that only downloaded must be re-deployed until it switches.

    The initial deploy makes the player download the playlist but not switch to
    it; without a follow-up the screen stays on the old playlist until someone
    presses Deploy in the console. The poll loop must do that follow-up.
    """
    await _select(hass, "NewYearSale")

    # The screen has finished syncing but is still showing the old playlist.
    mock_client.async_redeploy_playlist.reset_mock()
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="Promos", syncInProgress=False)
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.async_redeploy_playlist.assert_awaited()
    player_id, _group, playlist = mock_client.async_redeploy_playlist.await_args.args
    assert player_id == "player1"
    assert playlist == "NewYearSale"

    # Once the screen reports the new playlist, the nudging stops for good.
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="NewYearSale")
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.async_redeploy_playlist.reset_mock()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.async_redeploy_playlist.assert_not_awaited()


async def test_no_nudge_while_the_screen_is_still_downloading(
    hass: HomeAssistant, init_integration, mock_client, freezer
) -> None:
    """Re-deploying mid-download would only disturb the sync, so it waits."""
    await _select(hass, "NewYearSale")

    mock_client.async_redeploy_playlist.reset_mock()
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="Promos", syncInProgress=True)
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.async_redeploy_playlist.assert_not_awaited()


async def test_no_nudge_while_the_screen_is_offline(
    hass: HomeAssistant, init_integration, mock_client, freezer
) -> None:
    """An unreachable screen cannot switch, so nudging it is pointless."""
    await _select(hass, "NewYearSale")

    mock_client.async_redeploy_playlist.reset_mock()
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="Promos", isConnected=False)
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.async_redeploy_playlist.assert_not_awaited()


async def test_nudging_gives_up_after_the_budget_runs_out(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    freezer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A screen that never switches must not be re-deployed forever."""
    monkeypatch.setattr(coordinator_module, "PENDING_ASSIGNMENT_MAX_POLLS", 2)

    await _select(hass, "NewYearSale")
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="Promos", syncInProgress=False)
    ]

    # Poll a handful of times; with a budget of two it will give up quickly.
    for _ in range(6):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        if "giving up" in caplog.text:
            break

    assert mock_client.async_redeploy_playlist.await_count >= 1
    assert "giving up" in caplog.text

    # Once it has given up, no further cycle nudges the screen.
    mock_client.async_redeploy_playlist.reset_mock()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_client.async_redeploy_playlist.assert_not_awaited()


async def test_failed_selection_does_not_leave_a_false_reading(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    mock_client.async_assign_playlist.side_effect = PiSignageError("nope")

    with pytest.raises(HomeAssistantError):
        await _select(hass, "NewYearSale")
    await hass.async_block_till_done()

    assert hass.states.get(SELECT_ENTITY).state == "Promos"


async def test_playing_playlist_missing_from_server_stays_selectable(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """A deleted playlist still playing must not put the entity out of range."""
    mock_client.async_get_players.return_value = [
        make_player(currentPlaylist="DeletedPlaylist")
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(SELECT_ENTITY)
    assert state.state == "DeletedPlaylist"
    assert "DeletedPlaylist" in state.attributes[ATTR_OPTIONS]


async def test_current_playlist_sensor(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(SENSOR_ENTITY).state == "Promos"


async def test_online_binary_sensor(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(ONLINE_ENTITY).state == STATE_ON


async def test_disconnected_player_reads_offline(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    mock_client.async_get_players.return_value = [make_player(isConnected=False)]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ONLINE_ENTITY).state == STATE_OFF


async def test_stale_player_reads_offline_without_isconnected(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """Older players omit isConnected, so check-in age is the fallback."""
    player = make_player(lastReported=iso_now(-3600))
    del player["isConnected"]
    mock_client.async_get_players.return_value = [player]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ONLINE_ENTITY).state == STATE_OFF


async def test_recent_player_reads_online_without_isconnected(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    player = make_player(lastReported=iso_now(-30))
    del player["isConnected"]
    mock_client.async_get_players.return_value = [player]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ONLINE_ENTITY).state == STATE_ON


async def test_player_without_group_cannot_deploy(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    mock_client.async_get_players.return_value = [make_player(group=None)]
    mock_client.async_get_groups.return_value = [make_group()]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError):
        await _select(hass, "NewYearSale")
