"""Home Assistant integration conventions: errors, on/off, flows, planning view."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intuis_connect import IntuisPlanningView
from custom_components.intuis_connect.intuis_api.api import APIError, InvalidAuth
from custom_components.intuis_connect.utils.const import (
    API_MODE_HOME,
    API_MODE_OFF,
    DOMAIN,
)


async def test_climate_turn_on_follows_schedule(climate_entity_factory, mock_api):
    entity = climate_entity_factory()
    await entity.async_turn_on()
    assert mock_api.async_set_room_state.await_args.args[:2] == ("room_123", API_MODE_HOME)
    await entity.async_turn_off()
    assert mock_api.async_set_room_state.await_args.args[:2] == ("room_123", API_MODE_OFF)
    assert entity.hvac_mode == HVACMode.OFF


async def test_climate_api_error_is_a_home_assistant_error(climate_entity_factory, mock_api):
    """The user sees a readable error instead of a traceback."""
    entity = climate_entity_factory()
    mock_api.async_set_room_state.side_effect = APIError("500")
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_temperature(temperature=20)
    assert err.value.translation_key == "api_error"


async def test_climate_refused_token_starts_reauth(climate_entity_factory, mock_api, mock_coordinator):
    entity = climate_entity_factory()
    mock_coordinator.config_entry = MagicMock()
    mock_api.async_set_room_state.side_effect = InvalidAuth("refused")
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert err.value.translation_key == "auth_failed"
    mock_coordinator.config_entry.async_start_reauth.assert_called_once()


@pytest.fixture
def _flow_ready(recorder_mock, enable_custom_integrations):
    """The flow manager needs the integration and its recorder dependency."""


@pytest.mark.parametrize(
    ("error", "expected"),
    [(APIError("500"), "cannot_connect"), (RuntimeError("boom"), "unknown")],
)
async def test_config_flow_unexpected_errors_are_reported(_flow_ready, hass, error, expected):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.intuis_connect.config_flow.async_validate_api", side_effect=error
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "user@example.com", "password": "x"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_config_flow_aborts_when_already_configured(_flow_ready, hass):
    MockConfigEntry(domain=DOMAIN, unique_id="user@example.com", data={}).add_to_hass(hass)
    api = MagicMock(refresh_token="r")
    homes = [{"id": "home_1", "name": "Maison", "timezone": "Europe/Paris"}]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.intuis_connect.config_flow.async_validate_api",
        return_value=(homes, api),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "User@Example.com", "password": "x"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_planning_view_serves_cached_page(hass, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<html>planning</html>", encoding="utf-8")
    view = IntuisPlanningView(hass, page, "2.2.0")

    response = await view.get(MagicMock(headers={}))
    assert response.status == 200
    assert response.text == "<html>planning</html>"
    assert response.headers["ETag"] == '"2.2.0"'

    page.unlink()  # served from memory after the first read
    assert (await view.get(MagicMock(headers={}))).status == 200
    assert (await view.get(MagicMock(headers={"If-None-Match": '"2.2.0"'}))).status == 304
