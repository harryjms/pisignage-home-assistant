"""The piSignage integration."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PiSignageClient
from .const import CONF_ACCOUNT, PLATFORMS
from .coordinator import PiSignageConfigEntry, PiSignageCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: PiSignageConfigEntry) -> bool:
    """Set up piSignage from a config entry."""
    client = PiSignageClient(
        entry.data[CONF_ACCOUNT],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        session=async_get_clientsession(hass),
    )

    coordinator = PiSignageCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PiSignageConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: PiSignageConfigEntry
) -> None:
    """Reload when the options change, to pick up a new scan interval."""
    await hass.config_entries.async_reload(entry.entry_id)
