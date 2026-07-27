"""Tests for the playlist selector and the read-only entities."""

from __future__ import annotations

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

    mock_client.async_activate_playlist.assert_awaited_once()
    player_id, group, playlist = mock_client.async_activate_playlist.await_args.args
    assert player_id == "player1"
    assert group["_id"] == "group1"
    assert playlist == "NewYearSale"


async def test_selecting_unknown_playlist_is_rejected(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    with pytest.raises(ServiceValidationError):
        await _select(hass, "DoesNotExist")

    mock_client.async_activate_playlist.assert_not_awaited()


async def test_client_error_surfaces_to_the_user(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    mock_client.async_activate_playlist.side_effect = PiSignageError("still syncing")

    with pytest.raises(HomeAssistantError):
        await _select(hass, "NewYearSale")


async def test_group_deploy_is_warned_about(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A group-wide re-sync is a real side effect and must not be silent."""
    mock_client.async_activate_playlist.return_value = True

    await _select(hass, "NewYearSale")

    assert "Every player in that group has re-synced" in caplog.text
    assert "Stores" in caplog.text


async def test_no_warning_on_the_fast_path(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_client.async_activate_playlist.return_value = False

    await _select(hass, "NewYearSale")

    assert "has re-synced" not in caplog.text


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
