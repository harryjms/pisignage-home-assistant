"""Setup, unload and coordinator error handling."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pisignage.api import PiSignageAuthError, PiSignageError
from custom_components.pisignage.const import DOMAIN

from .conftest import make_player

SELECT_ENTITY = "select.lobby_screen_playlist"


async def test_setup_and_unload(hass: HomeAssistant, init_integration) -> None:
    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(SELECT_ENTITY) is not None

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED


async def test_reload(hass: HomeAssistant, init_integration) -> None:
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(SELECT_ENTITY) is not None


async def test_setup_retries_when_api_is_down(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    mock_client.async_get_players.side_effect = PiSignageError("down")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    mock_client.async_get_players.side_effect = PiSignageAuthError("expired")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_entities_go_unavailable_on_poll_failure(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    assert hass.states.get(SELECT_ENTITY).state == "Promos"

    mock_client.async_get_players.side_effect = PiSignageError("down")
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(SELECT_ENTITY).state == STATE_UNAVAILABLE


async def test_new_player_appears_without_reload(
    hass: HomeAssistant,
    init_integration,
    mock_client,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Players are provisioned outside HA, so a new one must just show up."""
    assert hass.states.get("select.foyer_screen_playlist") is None

    mock_client.async_get_players.return_value = [
        make_player(),
        make_player(_id="player2", name="Foyer Screen", currentPlaylist="NewYearSale"),
    ]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("select.foyer_screen_playlist").state == "NewYearSale"


async def test_device_is_registered(hass: HomeAssistant, init_integration) -> None:
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, "player1")})

    assert device is not None
    assert device.name == "Lobby Screen"
    assert device.sw_version == "5.4.3"
    assert device.serial_number == "400000000108d2e2"
    assert device.configuration_url == "https://myco.pisignage.com"
