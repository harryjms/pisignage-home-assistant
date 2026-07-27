"""Tests for the piSignage config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.pisignage.api import (
    PiSignageAuthError,
    PiSignageConnectionError,
    PiSignageError,
)
from custom_components.pisignage.const import CONF_ACCOUNT, DOMAIN

USER_INPUT = {
    CONF_ACCOUNT: "myco",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
}


async def test_user_flow_creates_entry(hass: HomeAssistant, mock_client) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "myco"
    assert result["data"] == USER_INPUT
    # The normalised URL is the identity, so spellings cannot duplicate.
    assert result["result"].unique_id == "https://myco.pisignage.com/api"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PiSignageAuthError("Incorrect password."), "invalid_auth"),
        (PiSignageConnectionError("down"), "cannot_connect"),
        # An application-level refusal is not a connectivity problem; calling
        # it one sent people to check their network for an API error.
        (PiSignageError("weird"), "api_error"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, mock_client, error: Exception, expected: str
) -> None:
    mock_client.async_validate_connection.side_effect = error

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    # The server's own wording must reach the form, not be replaced by a
    # generic message that sends people to debug the wrong thing.
    assert result["description_placeholders"]["detail"] == str(error)

    # The flow must still be usable once the problem clears.
    mock_client.async_validate_connection.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_rejects_empty_account(
    hass: HomeAssistant, mock_client
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_ACCOUNT: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ACCOUNT: "invalid_account"}


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-secret"
    # The rest of the entry must survive untouched.
    assert mock_config_entry.data[CONF_USERNAME] == "admin"


async def test_reauth_rejects_bad_password(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_client.async_validate_connection.side_effect = PiSignageAuthError("nope")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_PASSWORD] == "secret"


async def test_options_flow_sets_scan_interval(
    hass: HomeAssistant, init_integration
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {CONF_SCAN_INTERVAL: 120}
