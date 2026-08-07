"""Tests for the TV power switch."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.pisignage.api import PiSignageError
from custom_components.pisignage.const import CONF_TV_MEDIA_PLAYERS

from .conftest import make_player

TV_ENTITY = "switch.lobby_screen_tv"


async def _switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: TV_ENTITY}, blocking=True
    )


async def test_switch_reports_tv_state(hass: HomeAssistant, init_integration) -> None:
    assert hass.states.get(TV_ENTITY).state == STATE_ON


async def test_switch_reports_tv_state_from_string_booleans(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """Some builds send tvStatus/isCecSupported as strings, not JSON booleans.

    The switch must still appear and read on/off, rather than sitting in the
    unknown state that Home Assistant paints as a disabled toggle.
    """
    mock_client.async_get_players.return_value = [
        make_player(isCecSupported="true", tvStatus="false")
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY).state == STATE_OFF


async def test_no_switch_without_cec_support(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """A TV that cannot be reached over CEC would ignore the command.

    piSignage still reports success for it, so a switch there would silently do
    nothing — worse than not offering one.
    """
    mock_client.async_get_players.return_value = [make_player(isCecSupported=False)]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY) is None


async def test_switch_appears_when_cec_is_reported_later(
    hass: HomeAssistant, mock_config_entry, mock_client, freezer
) -> None:
    """Players probe the TV after start-up, so support can turn up late."""
    mock_client.async_get_players.return_value = [make_player(isCecSupported=False)]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(TV_ENTITY) is None

    mock_client.async_get_players.return_value = [make_player(isCecSupported=True)]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY) is not None


async def test_turn_off_calls_client(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    await _switch(hass, SERVICE_TURN_OFF)

    mock_client.async_set_tv_power.assert_awaited_once()
    player_id, on = mock_client.async_set_tv_power.await_args.args
    assert player_id == "player1"
    assert on is False


async def test_turn_on_calls_client(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    mock_client.async_get_players.return_value = [make_player(tvStatus=False)]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(TV_ENTITY).state == STATE_OFF

    await _switch(hass, SERVICE_TURN_ON)

    _player_id, on = mock_client.async_set_tv_power.await_args.args
    assert on is True


async def test_state_shows_immediately(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    """The player reports about once a minute; without this it springs back."""
    assert hass.states.get(TV_ENTITY).state == STATE_ON

    await _switch(hass, SERVICE_TURN_OFF)

    assert hass.states.get(TV_ENTITY).state == STATE_OFF


async def test_polled_value_takes_over_once_it_agrees(
    hass: HomeAssistant, init_integration, mock_client, freezer
) -> None:
    await _switch(hass, SERVICE_TURN_OFF)
    assert hass.states.get(TV_ENTITY).state == STATE_OFF

    mock_client.async_get_players.return_value = [make_player(tvStatus=False)]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Something else turns the TV back on — the switch must follow reality.
    mock_client.async_get_players.return_value = [make_player(tvStatus=True)]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY).state == STATE_ON


async def test_failure_surfaces_and_leaves_no_false_reading(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    mock_client.async_set_tv_power.side_effect = PiSignageError("no CEC")

    with pytest.raises(HomeAssistantError):
        await _switch(hass, SERVICE_TURN_OFF)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY).state == STATE_ON


async def test_media_player_backed_switch_for_screen_without_cec(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """A mapped media_player gives a non-CEC screen a working TV switch."""
    hass.states.async_set("media_player.lobby_tv", STATE_ON)
    mock_client.async_get_players.return_value = [make_player(isCecSupported=False)]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_TV_MEDIA_PLAYERS: {"player1": "media_player.lobby_tv"}},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY).state == STATE_ON


async def test_media_player_delegation_calls_the_media_player(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """The command must go to the media player, never to piSignage."""
    hass.states.async_set("media_player.lobby_tv", STATE_ON)
    calls = async_mock_service(hass, "media_player", SERVICE_TURN_OFF)

    mock_client.async_get_players.return_value = [make_player(isCecSupported=False)]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_TV_MEDIA_PLAYERS: {"player1": "media_player.lobby_tv"}},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await _switch(hass, SERVICE_TURN_OFF)

    assert len(calls) == 1
    assert calls[0].data[ATTR_ENTITY_ID] == "media_player.lobby_tv"
    mock_client.async_set_tv_power.assert_not_awaited()


async def test_delegated_switch_follows_the_media_player(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """The media player is the truth — piSignage knows nothing about that TV."""
    hass.states.async_set("media_player.lobby_tv", STATE_ON)
    mock_client.async_get_players.return_value = [make_player(isCecSupported=False)]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_TV_MEDIA_PLAYERS: {"player1": "media_player.lobby_tv"}},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("media_player.lobby_tv", STATE_OFF)
    await hass.async_block_till_done()

    assert hass.states.get(TV_ENTITY).state == STATE_OFF


async def test_cec_still_wins_when_no_mapping(
    hass: HomeAssistant, init_integration, mock_client
) -> None:
    """Without a mapping a CEC-capable screen keeps using piSignage."""
    await _switch(hass, SERVICE_TURN_OFF)

    mock_client.async_set_tv_power.assert_awaited_once()
