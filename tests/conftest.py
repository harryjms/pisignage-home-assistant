"""Shared fixtures for the piSignage tests."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pisignage.const import CONF_ACCOUNT, DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Without this HA will not load custom_components in tests."""
    return


def make_player(**overrides: Any) -> dict[str, Any]:
    """A player as the API returns it."""
    player = {
        "_id": "player1",
        "name": "Lobby Screen",
        "currentPlaylist": "Promos",
        "playlistOn": True,
        "group": {"_id": "group1", "name": "Stores"},
        "lastReported": time.time(),
        "version": "3.2.0",
        "platform_version": "bookworm",
        "cpuSerialNumber": "0000000012345678",
    }
    player.update(overrides)
    return player


def make_group(**overrides: Any) -> dict[str, Any]:
    """A group as the API returns it."""
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [{"name": "Promos", "settings": {}}],
        "deployedPlaylists": [{"name": "Promos", "settings": {}}],
    }
    group.update(overrides)
    return group


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A configured piSignage account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="myco",
        data={
            CONF_ACCOUNT: "myco",
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "secret",
        },
        unique_id="https://myco.pisignage.com/api",
    )


@pytest.fixture
def mock_client():
    """Patch the API client everywhere the integration constructs one."""
    with (
        patch(
            "custom_components.pisignage.PiSignageClient", autospec=True
        ) as mock_class,
        patch(
            "custom_components.pisignage.config_flow.PiSignageClient", new=mock_class
        ),
    ):
        client = mock_class.return_value
        client.base_url = "https://myco.pisignage.com/api"
        client.async_validate_connection = AsyncMock(return_value=None)
        client.async_get_players = AsyncMock(return_value=[make_player()])
        client.async_get_groups = AsyncMock(return_value=[make_group()])
        client.async_get_playlist_names = AsyncMock(
            return_value=["NewYearSale", "Promos"]
        )
        client.async_activate_playlist = AsyncMock(return_value=False)
        client.async_get_group = AsyncMock(return_value=make_group())
        yield client


@pytest.fixture
async def init_integration(hass, mock_config_entry, mock_client) -> MockConfigEntry:
    """Set the integration up and return its entry."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
