"""Tests for the config flow, run through the Home Assistant flow manager."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.intuis_connect.utils.const import (
    CONF_ENERGY_PRICE_MODE,
    CONF_HOME_ID,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
)

HOMES = [{"id": "home_1", "name": "Maison", "timezone": "Europe/Paris"}]


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let the flow manager load the integration from custom_components."""


@pytest.fixture
def mock_login():
    """Make the credential check succeed without network access."""
    api = MagicMock()
    api.refresh_token = "refresh_abc"
    with patch(
        "custom_components.intuis_connect.config_flow.async_validate_api",
        return_value=(HOMES, api),
    ) as validate, patch(
        "custom_components.intuis_connect.async_setup_entry", return_value=True
    ):
        yield validate


async def _walk_to_energy_cost(hass) -> dict:
    """Run the user flow with default answers up to the energy cost form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "User@Example.com", "password": "secret"}
    )
    for step in ("indefinite", "overrides", "energy", "history_import"):
        assert result["type"] is FlowResultType.FORM, result
        assert result["step_id"] == step
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    return result


async def test_user_flow_creates_entry(hass, mock_login):
    """A new install must get through every step, including energy cost."""
    result = await _walk_to_energy_cost(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "energy_cost"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENERGY_PRICE_MODE: "fixed"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == "User@Example.com"
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh_abc"
    assert result["data"][CONF_HOME_ID] == "home_1"
    assert result["options"][CONF_ENERGY_PRICE_MODE] == "fixed"


async def test_flows_do_not_share_options(hass, mock_login):
    """Answers given in one flow must not leak into another flow."""
    first = await _walk_to_energy_cost(hass)
    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    first_handler = hass.config_entries.flow._progress[first["flow_id"]]
    second_handler = hass.config_entries.flow._progress[second["flow_id"]]
    assert first_handler._override_options
    assert second_handler._override_options == {}


async def test_options_flow_keeps_and_clears_price_entity(hass, mock_login):
    """The options flow must accept its own defaults and allow clearing the price entity."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.intuis_connect.utils.const import CONF_ENERGY_PRICE_ENTITY

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_USERNAME: "user@example.com", CONF_REFRESH_TOKEN: "r", CONF_HOME_ID: "home_1"},
        options={CONF_ENERGY_PRICE_ENTITY: "sensor.price"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ("indefinite", "overrides", "energy", "rate_limit"):
        assert result["type"] is FlowResultType.FORM, result
        assert result["step_id"] == step
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["step_id"] == "energy_cost"
    # Submitting without the field (the user cleared it) stores an empty value.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENERGY_PRICE_ENTITY] == ""
