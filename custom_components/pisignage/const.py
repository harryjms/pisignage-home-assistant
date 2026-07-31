"""Constants for the piSignage integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "pisignage"
MANUFACTURER: Final = "piSignage"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONF_ACCOUNT: Final = "account"

#: Maps a player id to a ``media_player`` entity that controls its TV. Screens
#: whose player cannot reach the TV over HDMI-CEC get their TV switch from this
#: instead, so a TV that Home Assistant already controls another way is still
#: usable from the screen's device page.
CONF_TV_MEDIA_PLAYERS: Final = "tv_media_players"

# The hosted service is shared infrastructure and its own docs put the polling
# floor at 30-60s, so 60s is the default and 30s the hard minimum.
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 3600
