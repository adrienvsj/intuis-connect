"""Setup, unload and reload behaviour of the config entry on a real Home Assistant."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intuis_connect.entity.intuis_home import IntuisHome
from custom_components.intuis_connect.intuis_api.api import (
    APIError,
    CannotConnect,
    IntuisAPI,
    InvalidAuth,
)
from custom_components.intuis_connect.utils.const import DOMAIN

HOME_ID = "home_1"

HOMESDATA = {
    "id": HOME_ID,
    "name": "Maison",
    "country": "FR",
    "timezone": "Europe/Paris",
    "coordinates": [2.35, 48.85],
    "rooms": [
        {"id": "room_1", "name": "Salon", "type": "livingroom", "module_ids": ["rad_1"]},
        {"id": "room_2", "name": "Bureau", "type": "office", "module_ids": ["rad_2"]},
    ],
    "schedules": [
        {
            "id": "sched_1",
            "name": "Semaine",
            "type": "therm",
            "selected": True,
            "away_temp": 12,
            "hg_temp": 7,
            "zones": [
                {"id": 0, "name": "Confort", "type": 0, "rooms_temp": [{"room_id": "room_1", "temp": 20}]},
                {"id": 1, "name": "Nuit", "type": 1, "rooms_temp": [{"room_id": "room_1", "temp": 17}]},
            ],
            "timetable": [{"zone_id": 1, "m_offset": 0}, {"zone_id": 0, "m_offset": 420}],
        }
    ],
}

HOMESTATUS = {
    "id": HOME_ID,
    "rooms": [
        {
            "id": "room_1",
            "therm_setpoint_mode": "home",
            "therm_setpoint_temperature": 20,
            "therm_measured_temperature": 19.5,
            "muller_type": "NMH",
        },
        {
            # Pilot-wire radiator: no measured temperature
            "id": "room_2",
            "therm_setpoint_mode": "home",
            "therm_setpoint_fp": "comfort",
            "muller_type": "FP4",
        },
    ],
    "modules": [
        {"id": "gw_1", "type": "NMG"},
        {"id": "rad_1", "type": "NMH", "bridge": "gw_1", "radiator_state": "auto"},
        {"id": "rad_2", "type": "NMH", "bridge": "gw_1"},
    ],
}


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
def mock_api():
    """An API double whose async methods return the fixture payloads."""
    api = MagicMock(spec=IntuisAPI)
    api.home_id = HOME_ID
    api.async_refresh_access_token = AsyncMock()
    api.async_get_homes_data = AsyncMock(return_value=IntuisHome.from_api(HOMESDATA))
    api.async_get_home_status = AsyncMock(return_value=HOMESTATUS)
    api.async_get_config = AsyncMock(return_value={"modules": [{"id": "gw_1"}]})
    api.async_get_energy_measures = AsyncMock(return_value={})
    api.circuit_breaker = MagicMock(is_open=False)
    with patch("custom_components.intuis_connect.IntuisAPI", return_value=api):
        yield api


@pytest.fixture
def no_http(hass):
    """Satisfy the http dependency without starting a web server."""
    hass.config.components.add("http")
    hass.http = MagicMock()


def _entry(hass, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={"username": "user@example.com", "refresh_token": "r", "home_id": HOME_ID, "home_name": "Maison"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_and_unload(hass, mock_api, no_http):
    """All platforms load from realistic payloads and the entry unloads cleanly."""
    entry = _entry(hass)
    with patch("custom_components.intuis_connect.async_generate_services_yaml", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    climates = hass.states.async_entity_ids("climate")
    assert len(climates) == 2
    assert all(hass.states.get(c).state != "unavailable" for c in climates)
    assert hass.services.has_service(DOMAIN, "set_schedule_slot")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("error", [CannotConnect("down"), APIError("500")])
async def test_cloud_errors_retry_setup(hass, mock_api, no_http, error):
    """A cloud outage at startup is retried instead of failing for good."""
    mock_api.async_get_homes_data.side_effect = error
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_refused_token_starts_reauth(hass, mock_api, no_http):
    mock_api.async_refresh_access_token.side_effect = InvalidAuth("refused")
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_only_option_changes_reload(hass, mock_api, no_http):
    """Token updates and the history-import flag must not reload the entry."""
    entry = _entry(hass, import_history=True, import_history_days=0)
    with patch("custom_components.intuis_connect.async_generate_services_yaml", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
            hass.config_entries.async_update_entry(entry, data={**entry.data, "refresh_token": "new"})
            hass.config_entries.async_update_entry(entry, options={**entry.options, "import_history": False})
            await hass.async_block_till_done()
            reload.assert_not_called()

            hass.config_entries.async_update_entry(entry, options={**entry.options, "manual_duration": 60})
            await hass.async_block_till_done()
            reload.assert_awaited_once_with(entry.entry_id)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_schedule_devices_link_to_home(hass, mock_api, no_http):
    """Schedule devices hang under the home device without deprecated parameters."""
    from homeassistant.helpers import device_registry as dr

    entry = _entry(hass)
    with patch("custom_components.intuis_connect.async_generate_services_yaml", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    home = dev_reg.async_get_device_by_identifier((DOMAIN, f"{HOME_ID}_home"), entry.entry_id)
    schedule = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{HOME_ID}_schedule_sched_1"), entry.entry_id
    )
    assert home is not None and schedule is not None
    assert schedule.via_device_id == home.id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
