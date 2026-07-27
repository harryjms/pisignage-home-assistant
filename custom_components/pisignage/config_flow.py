"""Config flow for the piSignage integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

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
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

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
        """Let the user tune the poll interval."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
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
                ),
                self.config_entry.options,
            ),
        )
