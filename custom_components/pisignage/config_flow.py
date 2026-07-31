"""Config flow for the piSignage integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, Final

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    PiSignageAuthError,
    PiSignageClient,
    PiSignageConnectionError,
    PiSignageError,
    build_base_url,
)
from .const import (
    CONF_ACCOUNT,
    CONF_TV_MEDIA_PLAYERS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

#: Prefix for the per-screen TV fields in the options form. The player id is
#: appended, so each screen gets its own key without colliding with the rest.
TV_FIELD_PREFIX: Final = "tv_"

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): TextSelector(),
        vol.Required(CONF_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class PiSignageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the piSignage config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        # Surfaced in the form so the server's own wording reaches the user
        # instead of being replaced by a generic message.
        self._error_detail: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PiSignageOptionsFlow:
        """Return the options flow handler."""
        return PiSignageOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and prove them against the account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url = build_base_url(user_input[CONF_ACCOUNT])
            except ValueError:
                errors[CONF_ACCOUNT] = "invalid_account"
            else:
                # The normalised URL identifies the account, so the same one
                # cannot be added twice under different spellings.
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()

                errors = await self._async_validate(user_input)
                if not errors:
                    return self.async_create_entry(
                        title=user_input[CONF_ACCOUNT], data=user_input
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            description_placeholders={"detail": self._error_detail},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the coordinator reported an auth failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-collect the password for an existing entry."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            errors = await self._async_validate({**reauth_entry.data, **user_input})
            if not errors:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={
                CONF_ACCOUNT: reauth_entry.data.get(CONF_ACCOUNT, ""),
                "detail": self._error_detail,
            },
            errors=errors,
        )

    async def _async_validate(self, data: Mapping[str, Any]) -> dict[str, str]:
        """Try the credentials, returning form errors keyed for the UI."""
        client = PiSignageClient(
            data[CONF_ACCOUNT],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            session=async_get_clientsession(self.hass),
        )
        try:
            await client.async_validate_connection()
        except PiSignageAuthError as err:
            _LOGGER.debug("piSignage rejected the credentials: %s", err)
            self._error_detail = str(err)
            return {"base": "invalid_auth"}
        except PiSignageConnectionError as err:
            # Previously logged nothing at all, which left "check your internet"
            # as the only clue for a problem that was often something else.
            _LOGGER.debug("Could not reach piSignage at %s: %s", client.base_url, err)
            self._error_detail = str(err)
            return {"base": "cannot_connect"}
        except PiSignageError as err:
            # An application-level refusal is not a connectivity problem, and
            # flattening it into one hid the server's own explanation.
            _LOGGER.debug("piSignage refused the setup request: %s", err)
            self._error_detail = str(err)
            return {"base": "api_error"}
        except Exception as err:  # the flow must never leak a traceback to the UI
            _LOGGER.exception("Unexpected error validating piSignage credentials")
            self._error_detail = str(err)
            return {"base": "unknown"}
        return {}


class PiSignageOptionsFlow(OptionsFlow):
    """Handle the piSignage options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user tune the poll interval and map TVs to media players."""
        # Keyed by field label rather than player id: the label is what the form
        # shows, and a bare Mongo id would be meaningless to the user.
        players = self._async_player_fields()

        if user_input is not None:
            # Fold the per-screen fields back into one mapping, dropping any the
            # user cleared so a screen returns to using HDMI-CEC.
            tv_players = {
                player_id: entity
                for field, player_id in players.items()
                if (entity := user_input.get(field))
            }
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_TV_MEDIA_PLAYERS: tv_players,
                }
            )

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL,
                    max=MAX_SCAN_INTERVAL,
                    step=10,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            )
        }
        for field in players:
            schema[vol.Optional(field)] = EntitySelector(
                EntitySelectorConfig(domain="media_player")
            )

        current = dict(self.config_entry.options)
        mapped = current.get(CONF_TV_MEDIA_PLAYERS) or {}
        suggested: dict[str, Any] = {
            CONF_SCAN_INTERVAL: current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        }
        for field, player_id in players.items():
            if entity := mapped.get(player_id):
                suggested[field] = entity

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), suggested
            ),
        )

    @callback
    def _async_player_fields(self) -> dict[str, str]:
        """Form field name to player id, one per screen.

        The field is named after the screen so the form reads sensibly; the id
        is only ever stored. Two screens sharing a name get the id appended so
        neither field is lost.
        """
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None:
            return {}

        names: dict[str, int] = {}
        for player in coordinator.data.players.values():
            name = str(player.get("name") or "")
            names[name] = names.get(name, 0) + 1

        fields: dict[str, str] = {}
        for player_id, player in coordinator.data.players.items():
            name = str(player.get("name") or player_id)
            label = name if names.get(name, 0) == 1 else f"{name} ({player_id})"
            fields[f"{TV_FIELD_PREFIX}{label}"] = player_id
        return fields
